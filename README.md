# East Equity Agent

Level-2 agentic swing-trading system. Single Claude brain + deterministic Python
guardrails. **Long-only US equities, swing horizon (3–90 days), AI supply chain /
semiconductors / data center infrastructure universe.** Modeled on the Hermes
Auto-Trader pattern (farzad.money).

## Architecture

```
orchestrator.py (scheduled, deterministic)
  ├─ tools/            Python research tools (macro, scan, SEC, 13F, news, portfolio)
  ├─ Claude (via `claude -p`, rules in CLAUDE.md) → JSON trade proposals
  ├─ validator.py      pure-Python hard-rule enforcement (long-only, swing, sizing)
  ├─ execution/        simulated broker now; Moomoo/IBKR adapters later
  ├─ journal/          append-only JSONL audit trail (proposals/rejected/trades/runs)
  └─ dashboard/        public data files → Next.js on Vercel (Phase 4)
```

## Quick start

```bash
cd ~/east-equity-agent
source .venv/bin/activate
cp .env.example .env          # add FRED_API_KEY
python -m tools.universe_scanner --top 10   # test a tool
python orchestrator.py                       # full dry-run cycle
```

## Safety model

- `autonomy_config.json` — every hard rule lives here; the validator reads it fresh each run
- `trading_mode: dry_run | paper | live` — currently **dry_run** (nothing executes)
- Kill switch: `touch state/KILL_SWITCH` halts all new orders instantly
- Broker readback confirmation required before any trade is journaled as filled
- All rejected proposals logged with machine-readable reasons in `journal/rejected/`

## Build status

- [x] Phase 0 — structure, config, CLAUDE.md, venv
- [x] Phase 1 — research tools (SEC, 13F, macro, scanner, news)
- [x] Phase 2 — orchestrator + validator + journal (skeleton complete)
- [ ] Phase 3 — Moomoo/IBKR execution adapter
- [ ] Phase 4 — Next.js dashboard on Vercel
- [ ] Phase 5 — X posting
- [ ] Phase 6 — scheduling, watchdog, hardening
