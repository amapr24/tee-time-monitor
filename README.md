# Tee Time Monitor

Automated monitor for weekend tee time availability at five Miami-area golf courses. Runs on a schedule via GitHub Actions and publishes results as a static page served by GitHub Pages.

## How it works

1. `cron-job.org` fires a `workflow_dispatch` trigger against the GitHub API on a schedule.
2. The **Tee Time Monitor Miami** workflow (`tee-time-monitor.yml`) runs `tee_time_monitor.py`, which scrapes all five courses concurrently using Playwright (headless Chromium).
3. Per-course JSON caches (`cache_<course>.json`) are restored from Actions cache before the run and saved back after, so only *newly appeared* slots trigger Pushover notifications.
4. `generate_html()` rebuilds `index.html` from the cache files and the workflow commits it back to `main`.
5. GitHub Pages serves `index.html`; a `<meta refresh content="300">` reloads it every 5 minutes.

## Courses monitored

| Course | Booking platform |
|---|---|
| Miami Lakes | cpsgolf |
| Miami Beach | Chronogolf |
| Normandy Shores | Chronogolf |
| Plantation Preserve | WebTrac |
| Miami Shores | Chronogolf |

## Stack

- **Python 3.12** — single script `tee_time_monitor.py`
- **Playwright** (async, headless Chromium) — scraping
- **requests** — Pushover notifications
- **astral** — sunset calculation for upper tee-time cutoff

## Running locally

```bash
pip install playwright requests astral
playwright install chromium
DEBUG_SCRAPE=1 python tee_time_monitor.py
```

`DEBUG_SCRAPE=1` prints a page snippet when no slots are parsed, useful for diagnosing scraper breakage.

Required env vars for notifications (missing vars are logged and skipped, not fatal):

```
PUSHOVER_USER
PUSHOVER_TOKEN
EMAIL_SENDER   # optional Gmail SMTP
EMAIL_PASSWORD
EMAIL_TO
```

## Branches

- `main` — production; workflow pushes `index.html` to `main` (served by Pages)
- `dev` — development; uses `tee-time-monitor-dev.yml`, pushes to `dev`
