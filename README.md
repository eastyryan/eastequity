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
  ├─ Claude (via `claude -p`, rules in CLAUDE.md) → JSON trade proposals
  ├─ validator.py      pure-Python hard-rule enforcement (long-only, swing, sizing)
  ├─ execution/        simulated broker now; Moomoo/IBKR adapters later
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
| 6:00am | `light` | Holdings + watchlist prices; exits only |
| 9am, 10am, 12pm, 2pm | `holdings_watchlist` | **Four fast trading cycles** — mini-scan + deep research on holdings & watchlist only |
| 4:00pm | `full` | Full universe scan + fat-pitch promotion + deep focus set |
| 5:30pm | evening review | News-only commentary (no trading) |
| Sunday 12:00am | `weekly_market` | Multi-sector breadth + discovery sweep; publishes `market_checkin.json`; no trading |

```bash
python orchestrator.py --depth holdings_watchlist   # fast focused cycle
python orchestrator.py --depth full                 # classic deep cycle
python orchestrator.py --weekly-market              # Sunday breadth check-in
python orchestrator.py --gather-only --depth full   # data only
python orchestrator.py --learning-mark              # shadow + post-exit marks, news cache, lesson prune
```

## Quick start

```bash
cd ~/east-equity-agent
source .venv/bin/activate
cp .env.example .env          # add FRED_API_KEY
python -m tools.universe_scanner --top 10   # test a tool
python orchestrator.py --depth holdings_watchlist
```

## Safety model

- `autonomy_config.json` — every hard rule lives here; the validator reads it fresh each run
- `trading_mode: dry_run | paper | live` — currently **paper** (simulated broker)
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
- [ ] Phase 3 — Moomoo/IBKR execution adapter
- [ ] Phase 5 — X posting (draft path exists)
- [ ] Phase 6 — further scheduling/watchdog hardening
