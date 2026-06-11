# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Big picture

This project is **not a long-running service**. It is a cron-driven scrape-and-publish pipeline whose output is a set of generated files (`index.html`, `data.json`, `version.json`) committed into the repo and served by **GitHub Pages**. The end user only ever sees that page (or the app reading `data.json`).

**Two repos are involved.** Production is `amapr24/tee-time-monitor` (upstream): cron-job.org dispatches its workflow and GitHub Pages serves its output. Development happens in the fork `linksgolfpr/tee-time-monitor-cursor`; changes flow upstream via cross-repo PR (`fork:main` → `upstream:production`), and the fork is periodically synced back from upstream. Code you change here does not run in production until that upstream PR is merged.

Flow on every production run:

1. `cron-job.org` (external) fires `POST https://api.github.com/repos/amapr24/tee-time-monitor/actions/workflows/tee-time-monitor.yml/dispatches` with a PAT (~every 10 minutes), triggering the workflow on `production`.
2. The GitHub Action checks out the repo, installs pinned dependencies + Chromium (both cached, keyed on `requirements.txt`), restores per-course JSON caches from Actions cache, runs `python tee_time_monitor.py`, saves the caches back, then commits and pushes any change to the generated files.
3. GitHub Pages serves the updated `index.html`; the page polls `version.json` every 30 s and reloads itself when the `ts` value changes (there is no meta refresh).

The pipeline used to route through Vercel serverless functions (`api/trigger.js` dispatched the workflow, `api/teetimes.js` proxied the HTML, and the root `index.html` was a loader that fetched from `/api/teetimes`). That layer was removed on 2026-04-19 in response to the Vercel incident. Do not reintroduce a `/api/*` dependency on a backend that doesn't exist — GitHub Pages has no server side. If a "trigger now from the browser" feature is ever wanted back, it needs a separate host (Cloudflare Workers is the obvious pick) because a GitHub PAT cannot live in static HTML.

`.nojekyll` at the repo root disables Jekyll processing on Pages.

State lives in three places:
- **Per-course cache files** (`cache_<course>.json`) — restored/saved via `actions/cache`. Used to detect *new* slots vs. previous run. The cache key uses `${{ github.run_id }}` on save with `restore-keys: teetime-cache-` so each run writes a fresh entry and reads the latest one (see the comment block in the workflow — do not "simplify" this to a static key).
- **`index.html` + `version.json`** — committed back on every run. `index.html` is both the published UI and the durable record of the last successful scrape; `generate_html()` rebuilds it entirely from the cache files. `version.json` holds a timestamp the page polls to know when to reload.
- **`data.json`** — committed on every run by `generate_data_json()`; consumed by the Miami Tee Times app. Keep its schema stable unless the app changes too.

## `tee_time_monitor.py` architecture

Single file, organized as:

- `COURSES` list at the top — the config. Five courses: **Miami Lakes**, **Miami Beach**, **Normandy Shores**, **Plantation Preserve**, **Miami Shores**. Each entry has a `type` that routes to a scraper:
  - `cpsgolf` → `scrape_cpsgolf` (Miami Lakes; calendar widget, must click month `›` then day). The site defaults the Players selector to 4 and does not expose a URL param for it, so the tee sheet returned is 4-person-only. If you ever want to broaden it, click the "Any" pill in the Players row before the day-click (previously prototyped and reverted — see git history).
  - `chronogolf` → two paths: courses with a `chronogolf_club_id` (Miami Shores) skip the browser entirely and hit the club-widget JSON API via plain `requests` (`fetch_chronogolf_club_teetimes`); the rest (Miami Beach, Normandy Shores) use the Playwright scraper `scrape_chronogolf` (date passed in URL query; `skip_past_dates: True` because Chronogolf silently redirects past dates to today and would poison the cache).
  - `webtrac` → `scrape_webtrac` (Plantation Preserve; plain GET form — must first load the base page to grab `#_csrf_token`, then navigate to the results URL with `begintime=12:00 am` to get all times regardless of the site's default cutoff).
- One shared Chromium per run: `main` launches a single browser (none at all if every selected course is API-only) and each course gets its **own browser context** in `check_course` — per-course cookie/UA isolation so each course still looks like a returning human visitor, without five browser processes on a 2-core runner. Each date is in its own try/except so one failure doesn't abort the rest. Blocking `requests` calls (club API, Pushover) run via `asyncio.to_thread` so they can't stall concurrent scrapes.
- `main` runs all courses concurrently via `asyncio.gather(..., return_exceptions=True)`, logs any course-level failures, then calls `generate_html()` and `generate_data_json()` once at the end.
- Time-window filtering: each course has `tee_time_min`/`tee_time_max`, but the upper bound is overridden per-day by `get_sunset_cutoff` (sunset − 4 h 10 m, derived from the `lru_cache`d `get_sunset`). Do not hard-code `tee_time_max` as the final filter. Miami Lakes additionally has `scrape_time_max` (17): the CPS site pre-filters by the URL's `TeeOffTimeMax`, so the scrape requests a wider window than `tee_time_max` and lets the sunset cutoff do the real trimming. Separately, `_slot_time_class` marks slots within 4 h 30 m of sunset as twilight for page styling — the two thresholds are intentionally different.
- Dates come from `get_monitor_dates()`: days within the next 7 whose weekday is in `DEFAULT_SCRAPE_WEEKDAYS` (Fri/Sat/Sun) ∪ `EXTRA_SCRAPE_WEEKDAYS`, computed in `America/New_York`. To monitor an extra day, add it to `EXTRA_SCRAPE_WEEKDAYS` — don't touch the date loop.
- Notifications are **Pushover only** (`send_pushover`); the email layer was removed 2026-06-11. Alerts include booking links — per-date deep links for Chronogolf courses (`chronogolf_book_url`), the course URL otherwise. `cpsgolf_book_url` builds the Miami Lakes page link with an integer `TeeOffTimeMax` hour.
- The generated page has no trigger button. Don't add one back without adding a backend.
- **Do not hand-edit `index.html`, `data.json`, or `version.json`** — they are fully regenerated on every run. Any manual changes will be overwritten on the next workflow execution.

When adding a new course, add a dict to `COURSES` **and** add its `cache_file` to the `path:` list in *both* the restore and save steps of `.github/workflows/tee-time-monitor.yml`, otherwise its cache will be wiped every run and every slot will look "new".

## Running

Dependencies are pinned in `requirements.txt` (runtime) and `requirements-dev.txt` (adds pytest).

To run the scraper locally (rarely needed — usually debug in Actions):

```bash
pip install -r requirements.txt
playwright install chromium
python tee_time_monitor.py                                 # all courses
python tee_time_monitor.py --course "miami beach"          # filter by substring, repeatable
```

A filtered local run deliberately skips `generate_html()`/`generate_data_json()` so a partial scrape can't overwrite the published output with a subset of course data.

Required env vars for notifications: `PUSHOVER_USER`, `PUSHOVER_TOKEN`. Missing creds are logged and skipped, not fatal.

To validate a change with a real scrape: push a branch to the fork and manually dispatch **Tee Time Monitor Miami** from the fork's Actions tab against that branch (generated-output commits will land on that branch — use a throwaway branch, not `main`). Production runs happen only in the upstream repo via cron-job.org.

## Tests

Parser logic is pure Python and tested without a browser. The scrapers' `page.evaluate` blocks only *select* DOM elements and return raw innerText (or table cell arrays); `parse_cpsgolf` / `parse_chronogolf` / `parse_chronogolf_club_api` / `parse_webtrac` do all extraction, so `tests/test_parsers.py` can feed fixtures directly.

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

Tests run automatically on PRs via `.github/workflows/tests.yml`, and on pushes to `main`/`production` except commits that only touch generated output. If you change a site-parsing regex, update the corresponding tests — they encode the exact current behavior (including quirks like "18 HOLES" capturing as "18 HOLE").

Playwright is imported via a guarded `try/except` so the parsers module stays importable in environments where the Chromium binary isn't installed.

`page.goto` is wrapped by `goto_with_retry` (3 attempts, exponential backoff) to ride through transient timeouts without losing the whole day.

## Workflow (`tee-time-monitor.yml`)

- `workflow_dispatch` only — no schedule; the cadence comes from cron-job.org hitting the upstream repo.
- `concurrency: group: tee-time-monitor / cancel-in-progress: false` serializes runs. GitHub keeps at most **one pending run** per group, so extra triggers arriving during a run are dropped by design. `timeout-minutes: 20` caps how long a hung run can block the group.
- The save-cache and commit steps use `if: always()` so a partial scrape failure still persists whatever was gathered — preserve this behavior.
- The commit step runs `git add` → `git commit` → `git pull --rebase` → `git push`, **in that order**. The scraper leaves the working tree dirty, and git refuses to rebase over unstaged changes — moving the pull before the commit broke every automated run on 2026-06-10 (`c7508f1`, reverted by `1c95fee`). Do not reorder.
- pip and Playwright caches are keyed on `hashFiles('requirements.txt')`; a dependency bump invalidates them (one slow cold run, then cached again).
