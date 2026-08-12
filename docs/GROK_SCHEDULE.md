# East Equity — Grok scheduled trader

The scheduled brain is **Grok 4.6 on GitHub Actions**. The Mac does not need to
be awake. Claude is not on the path: not the brain, not the risk desk, not study.

Auth is the Grok CLI in CI (`XAI_API_KEY` repo secret). That is the only way a
headless runner can call Grok with the laptop off. It is not a Python SDK
wrapper around the API; it is `grok -p`, same CLI as local.

## Clock (America/New_York)

| Time (ET) | Role | How it runs |
|-----------|------|-------------|
| 6:00 AM wkdy | light | `orchestrator.py --auto-depth` |
| 8:45 AM wkdy | holdings_watchlist | same |
| 10:30 AM wkdy | full | same |
| 12:00 PM wkdy | holdings_watchlist | same |
| 2:00 PM wkdy | holdings_watchlist | same |
| 3:30 PM wkdy | full (pre-bell) | same |
| 5:30 PM wkdy | evening_review (+ Fri self-review) | `--auto-depth` then `--self-review` |
| 7:00 PM wkdy | study | `--study` |
| 12:00 AM daily | news / Sunday weekly_market | `--news-only` / `--weekly-market` |
| 11:59 PM Sat+Sun | weekend news | `--news-only` (+ Sun `--universe-review`) |
| :15 7:15 AM–7:15 PM wkdy | watchdog | `find_missed_slot.py` then a pinned-depth rerun |

Workflow: `.github/workflows/grok-cycle.yml`  
Gate: `scripts/cloud_slot.sh` (20-minute window after each slot so GitHub cron delay cannot steal the next slot)

## Required secret

Repo **Settings → Secrets and variables → Actions → New repository secret**:

- `XAI_API_KEY` — create at https://console.x.ai  
- Already present: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`  
- Optional: `FRED_API_KEY` (gather degrades without it)

Until `XAI_API_KEY` is set, every cycle job fails in the first step and no slot runs.

## Hand-fire from Actions

Actions → **Grok trading cycle** → Run workflow → pick a role (`brain`, `evening`, `study`, `watchdog`, or `auto`).

## Local

`scripts/manual_run.sh` is still the only supported hand-fired path. It now wakes
Grok, not Claude. Local launchd traders (`com.eastequity.grok-slots`,
`com.eastequity.grok-watchdog`) stay unloaded so a laptop that happens to be
open cannot double-trade against Actions.

## Risk desk

Brain = `grok-4.6`. Desk = `grok-4.5` (a different model, same as the old
Opus/Sonnet split). Desk can only veto or haircut.
