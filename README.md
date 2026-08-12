# East Equity Agent

Level-2 agentic swing-trading system. Single Claude brain + deterministic Python
guardrails. **Long-only US equities, swing horizon (3–90 days), AI supply chain /
semiconductors / data center infrastructure universe.** Modeled on the Hermes
Auto-Trader pattern (farzad.money).

## Architecture

```
orchestrator.py        thin entrypoint (re-exports + main)
  ├─ runlib/
  │    core, depths, preflight
  │    context_gather   research bundle
  │    context_tiers    FULL archive + SLIM brain pack
  │    brain_io         ask Claude / risk desk / execute
  │    analytics, publish, reviews
  ├─ tools/            research + learning (shadow, exit, adopt, learning_mark)
  ├─ Grok 4.6 (via `grok -p` on GitHub Actions, rules in CLAUDE.md) → JSON trade proposals
  ├─ validator.py      pure-Python hard-rule enforcement (long-only, swing, sizing)
  ├─ execution/        broker router: Alpaca paper (live) + simulation fallback
  ├─ journal/          append-only JSONL audit trail
  └─ dashboard/        public data → Next.js on Vercel
```

**Context tiers:** every brain wake writes `state/context_full_<run>.json` (full archive)
and `state/context_<run>.json` (slim pack: always keys + focus research + top-N
`learning_pack`). Learning histories stay in archive; the brain sees the compact pack.

## Run depths (speed + breadth)

Full universe deep research is slow — most slots no longer do it.

| Slot (ET) | Depth | What it does |
|-----------|--------|----------------|
| 6:00am wkdy | `light` | Holdings + watchlist prices; exits only |
| 8:45am, 12pm, 2pm wkdy | `holdings_watchlist` | Fast trading cycles — holdings, watchlist, tape/8-K only |
| 10:30am + 3:30pm wkdy | `full` | Full universe scan + fat-pitch promotion (3:30 is pre-bell) |
| 5:30pm wkdy | evening review | News-only commentary (Fri also weekly self-review) |
| 7:00pm wkdy | `--study` | Learning only, no trades |
| 12:00am daily | news / `weekly_market` | Overnight review; Sunday = weekly breadth |
| 11:59pm Sat+Sun | `--news-only` | Weekend news; Sunday also universe review |
| :15 7:15am–7:15pm wkdy | watchdog | Re-runs missed slots; serves watchlist triggers |

### Running it by hand

The **scheduled trader is Grok 4.6 on GitHub Actions** (`grok-cycle.yml`). The Mac
does not need to be awake. See `docs/GROK_SCHEDULE.md`. Auth is your existing
Grok login (`GROK_AUTH_JSON` secret) — no paid API key. Hand-fired local runs
still go through `scripts/manual_run.sh`.

```bash
scripts/manual_run.sh                       # fast focused cycle (holdings_watchlist)
scripts/manual_run.sh --depth full          # classic deep cycle
scripts/manual_run.sh --weekly-market       # Sunday breadth check-in
scripts/manual_run.sh --gather-only         # data only
scripts/manual_run.sh --learning-mark       # shadow + post-exit marks, news cache, lesson prune
```

The wrapper exists because `orchestrator.py`'s default is *"I am a scheduled run"*, so a
hand-typed `python orchestrator.py --depth ...` (no `--manual`) is indistinguishable from
one. That went wrong twice on 2026-07-20: the run journaled `manual: false` and was
counted by the heartbeat as a **completed scheduled slot** — filling in a slot the cloud
had actually missed, so an outage read as healthy — and it claimed the 30-minute
cross-node lease, making the next scheduled cloud run stand down.

Manual runs now **yield but never block**: they abort if the cloud holds the lease, and
they never claim one. Scheduled wins, manual fits in the gaps. See
`runlib/preflight.py:acquire_cross_node_lease` and `tests/test_manual_run_detached.py`.

## Quick start

```bash
cd ~/east-equity-agent
source .venv/bin/activate
cp .env.example .env          # add FRED_API_KEY
python -m tools.universe_scanner --top 10   # test a tool
scripts/manual_run.sh                       # a real run, marked manual
```

## Repo visibility

This repository is **public** (verified `gh repo view` 2026-08-05 — some older
comments claimed otherwise and have been corrected). What that means in practice:

- **World-readable:** the live paper portfolio (`state/portfolio.json`), queued
  order intents *before* the executor picks them up (`state/order_intents.json`),
  the full append-only journal (`journal/`), research bundles (`data/`), and the
  dashboard's committed data. That is by design — the experiment runs in public
  and the dashboard is built from this data.
- **No credentials live in the repo.** Keys stay in local `.env` / GitHub Actions
  secrets / Vercel env vars, and `tests/test_secrets_hygiene.py` pins this in CI:
  it fails the suite if a secrets file or value-shaped secret lands in the tree.
- **Making it private is a product decision, not a hygiene fix** — nothing here
  is secret, and flipping visibility would need re-checking every unauthenticated
  read path first: the dashboard's GitHub reads (`dashboard/app/api/live-prices/
  route.ts` already sends a token so it survives either way, but audit for other
  raw reads), Vercel's repo access for builds, and the free-tier Actions minutes
  that public repos get.

## Running the tests

CI runs the suite on **Python 3.11** (`tests.yml`); the local system python is
3.9, which is why a dedicated venv exists. Run it like this:

```bash
EE_BROKER=simulation .venv311/bin/python -m pytest tests/ -q
```

Two things to know before running it:

- **Never run two suites concurrently** (or a suite while a trading run is
  active): `tests/conftest.py` swaps the LIVE `state/portfolio.json` aside for
  the duration of the run and restores it afterwards. Two overlapping runs can
  restore the wrong ledger over the real one.
- **One known local-only failure:** `test_claude_md_paths_exist` can fail
  locally against a stale local fixture; it passes in CI, which is the
  authoritative environment.

## Safety model

- `autonomy_config.json` — every hard rule lives here; the validator reads it fresh each run
- `trading_mode: dry_run | paper | live` — currently **paper** on a real Alpaca paper account (`mode.broker: alpaca_paper`; `simulation` is the offline fallback)
- Kill switch: `touch state/KILL_SWITCH` halts all new orders instantly
- Broker readback confirmation required before any trade is journaled as filled
- Calendar-day BUY cap + batch cap on `max_new_positions_per_day`
- **Risk desk required for BUYs** (adversarial kill checklist; rejects if CLI missing)
- **Thesis invalidators** + **demand_driver** required on every BUY (theme concentration ~35%)
- All rejected proposals logged with machine-readable reasons in `journal/rejected/`
- Exit autopsies → `journal/exit_autopsies/`; watchlist opportunity-cost feedback in context

## Reasoning / research (paper learning phase)

Context bundle includes:
- `reasoning_process` — process checklist, watchlist feedback, exit lessons, themes, freshness
- `stack_cards` — layer / customers / substitutes / differential per focus name
- `financial_checklists` — model-specific financial lines (capex-growth, semi, SaaS, bank, …)
- `concept_memory` — durable per-ticker understanding that compounds
- `universe_scan.prices_meta` — per-ticker `price_as_of` (bar session date)
- Process gates — watchlist `status` (drop|hold|buy); full-run `rejected_ideas` (≥2)

### Self-improvement (five loops)

| System | Storage | What it does |
|--------|---------|----------------|
| **Shadow portfolio** | `data/shadow_portfolio.json` | Tracks skips/rejects; marks 30/60/90d; regret_miss vs good_skip |
| **Exit grades** | `journal/exit_autopsies/` + `data/binding_exit_lessons.json` | Deterministic process_win/fail at every close |
| **Post-exit runners** | `data/post_exit_runners.json` | 15/30/60d leftover gains; headlines cached on mark days for WHY |
| **Concept memory** | `data/concept_memory/{TICKER}.json` | What they do + lessons from research/exits |
| **Adopt pipeline** | `data/adopted_lessons.md` + `learning_proposals.json` | Weekly: improvement notes → soft standing lessons; prune/cap with `superseded_by` |
| **Calibration gate** | `learning_controls` in config | After 15 closes: confidence caps + losing-bucket exceptions (validator) |
| **Learning mark job** | `orchestrator.py --learning-mark` | Dedicated mark pass (shadows + runners + news cache + lesson prune) |

Weekly self-review runs the adopt pipeline and re-marks shadows. See CLAUDE.md Learning Protocol.

## Build status

- [x] Phase 0 — structure, config, CLAUDE.md, venv
- [x] Phase 1 — research tools (SEC, 13F, macro, scanner, news)
- [x] Phase 2 — orchestrator + validator + journal
- [x] Tiered run depths + weekly market check-in
- [x] Dashboard (Next.js) — live
- [x] Phase 3 — real-broker execution (Alpaca paper; IBKR later for live — Moomoo Canada has no OpenAPI)
- [ ] Phase 5 — X posting (draft path exists)
- [x] Phase 6 — Grok scheduled trader (launchd + grok CLI + Automations pulse)
