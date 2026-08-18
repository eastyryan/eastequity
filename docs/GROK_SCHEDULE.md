# East Equity — Grok scheduled trader

The scheduled brain is **Grok 4.6 on GitHub Actions**. The Mac does not need to
be awake. Claude is not on the path: not the brain, not the risk desk, not study.

Auth is the Grok CLI in CI. **You do not buy an API key.** Paste the same
`~/.grok/auth.json` login you already use (SuperGrok / grok.com session) into
the `GROK_AUTH_JSON` repo secret. That is the Claude-CCR pattern: subscription
quota, no console.x.ai invoice. `XAI_API_KEY` is optional fallback only.

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
Gate: `scripts/cloud_slot.sh` (75-minute primary window after each slot — matches `slot_depth_from_hhmm` tolerance; min inter-slot gap is 90 min so GitHub cron delay cannot steal the next slot). Any weekday fire outside a primary window during ~06:20–19:20 ET falls through to **watchdog recovery** (not only when the clock minute is `:15`). Crons are EDT (UTC-4) through 2026-11-01; `slot_already_landed.py` refuses a second landing of the same primary slot the same ET day.

## Required secret (no paid API key)

From any Mac where you have already run `grok login`:

```bash
gh secret set GROK_AUTH_JSON --repo eastyryan/eastequity < ~/.grok/auth.json
```

That file is your Grok subscription session. Sessions last on the order of
weeks; if slots start failing with an auth error, run `grok login` again and
re-set the secret. Do not commit `auth.json` to the repo.

Already present: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`.

### Keep auth from dying (required for unattended trading)

OIDC access tokens expire in hours and **rotate on every successful agent
call**. Actions cache keeps the rotated file between jobs; that alone is not
enough — when the cache goes stale the secret still holds the *old* refresh
token and every slot dies with `Not signed in`.

Wire secret auto-refresh so each successful cycle writes the rotated
`auth.json` back into `GROK_AUTH_JSON`:

1. Create a classic PAT with the `repo` scope (or a fine-grained PAT with
   Secrets: Read and write on this repo only).
2. Store it (never commit it):

```bash
gh secret set GH_PAT_SECRETS --repo eastyryan/eastequity
```

3. Confirm the next Grok cycle log ends with
   `GROK_AUTH_JSON secret refreshed from runner session`
   instead of `GH_PAT_SECRETS not set`.

Manual recovery if auth still fails:

```bash
grok login
gh secret set GROK_AUTH_JSON --repo eastyryan/eastequity < ~/.grok/auth.json
```

Optional paid fallback: `XAI_API_KEY` (metered). The workflow uses it only
when OIDC is completely unavailable.
  
Optional: `FRED_API_KEY`, `XAI_API_KEY` (only if you later want metered API).

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
