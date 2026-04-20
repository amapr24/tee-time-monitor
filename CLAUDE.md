# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Big picture

This project is **not a long-running service**. It is a cron-driven scrape-and-publish pipeline whose output is a static `index.html` committed into the repo and served by Vercel. The end user only ever sees that page.

Flow on every run:

1. `cron-job.org` fires `POST /api/trigger` on Vercel (or the user clicks "CHECK NOW" on the page, which also hits `/api/trigger`).
2. `api/trigger.js` uses `GITHUB_PAT` to dispatch the `tee-time-monitor.yml` workflow (ref `main`). `api/teetimes.js` is a separate endpoint that proxies the raw `index.html` from GitHub — it is unrelated to triggering.
3. The GitHub Action checks out the repo, installs Playwright + Chromium, restores per-course JSON caches from Actions cache, runs `python tee_time_monitor.py`, saves the caches back, then commits and pushes any change to `index.html`.
4. Vercel serves the updated page; a `<meta refresh>` on the page reloads it every 5 min.

State lives in two places:
- **Per-course cache files** (`cache_<course>.json`) — restored/saved via `actions/cache`. Used to detect *new* slots vs. previous run. The cache key uses `${{ github.run_id }}` on save with `restore-keys: teetime-cache-` so each run writes a fresh entry and reads the latest one (see the comment block in the workflow — do not "simplify" this to a static key).
- **`index.html`** — committed back to `main` on every run. It is both the published UI and the durable record of the last successful scrape; `generate_html()` in `tee_time_monitor.py` rebuilds it entirely from the cache files.

## `tee_time_monitor.py` architecture

Single file, ~1600 lines, organized as:

- `COURSES` list at the top — the config. Each entry has a `type` that routes to one of three scrapers:
  - `cpsgolf` → `scrape_cpsgolf` (Miami Lakes; calendar widget, must click month `›` then day).
  - `chronogolf` → `scrape_chronogolf` (date passed in URL query; `skip_past_dates: True` because Chronogolf silently redirects past dates to today and would poison the cache).
  - `webtrac` → `scrape_webtrac` (Plantation Preserve; plain GET form — must first load the base page to grab `#_csrf_token`, then navigate to the results URL with `begintime=12:00 am` to get all times regardless of the site's default cutoff).
- `check_course` launches **one** browser per course and reuses the context across all target dates (shared cookies/UA make it look like a returning human visitor). Each date is in its own try/except so one failure doesn't abort the rest.
- `main` runs all courses concurrently via `asyncio.gather(..., return_exceptions=True)`, then calls `generate_html()` once at the end.
- Time-window filtering: each course has `tee_time_min`/`tee_time_max`, but the upper bound is overridden per-day by `get_sunset_cutoff` (sunset − 4 hours, via `astral`). Do not hard-code `tee_time_max` as the final filter.
- Dates are always computed in `America/New_York` via `get_upcoming_weekend_dates()` (Fri/Sat/Sun within the next 6 days).
- Notifications: `notify()` currently calls Pushover only — the `send_email` call is commented out. Re-enable by uncommenting in `notify()`, not by calling `send_email` elsewhere.

When adding a new course, add a dict to `COURSES` **and** add its `cache_file` to the `path:` list in *both* the restore and save steps of `.github/workflows/tee-time-monitor.yml` (and `-dev.yml` if applicable), otherwise its cache will be wiped every run and every slot will look "new".

## Running

There is no local entry point and no `requirements.txt` despite what `README.md` claims (the README is aspirational/stale — ignore its "python main.py" / Selenium / BeautifulSoup section; the real stack is Playwright + `requests` + `astral`).

To run the scraper locally (rarely needed — usually debug in Actions):

```bash
pip install playwright requests astral
playwright install chromium
DEBUG_SCRAPE=1 python tee_time_monitor.py    # DEBUG_SCRAPE prints page snippet when no slots parse
```

Required env vars for notifications: `PUSHOVER_USER`, `PUSHOVER_TOKEN`, and optionally `EMAIL_SENDER`/`EMAIL_PASSWORD`/`EMAIL_TO` (Gmail SMTP). Missing creds are logged and skipped, not fatal.

To trigger a real run: push to `main` and manually dispatch **Tee Time Monitor Miami** from the Actions tab, or `curl -X POST https://<vercel-domain>/api/trigger`.

There are no tests, no linter config, and no build step.

## Branches / workflows

- `main` → `tee-time-monitor.yml` — production. Covers all 5 courses and pushes `index.html` to `main`.
- `dev` → `tee-time-monitor-dev.yml` — same script but only 3 courses cached, and pushes to `dev` via `git push origin dev`. Keep the two workflows in sync when changing steps.

Both workflows use `if: always()` on the save-cache and commit steps so a partial scrape failure still persists whatever was gathered — preserve this behavior.

## `archive/`

`monitor_miami_lakes.py`, `monitor_normandy.py`, `monitor_shores.py` are the original per-course scripts that were consolidated into `tee_time_monitor.py`. They are kept for reference only — do not edit them, and do not wire them into the workflow.
