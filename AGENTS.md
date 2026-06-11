# AGENTS.md

All project guidance for coding agents lives in [CLAUDE.md](CLAUDE.md) — read that file before making changes.

It is the single source of truth for the pipeline architecture (two-repo fork/upstream topology, cron → Actions → GitHub Pages flow), the `tee_time_monitor.py` scraper layout, how to run and test locally, and the workflow invariants that have bitten us before (commit-before-pull ordering, cache key strategy, generated files that must never be hand-edited).

This file used to carry a diverging copy of that guidance; it was consolidated on 2026-06-11 so the two can't drift apart again.
