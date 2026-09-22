# Research lanes (what the brain sees)

East Equity gathers a **full archive** and a **slim brain pack** every run.
Grok Bot (scheduled slots on GitHub Actions) reads the slim pack only.

## Depths ↔ lanes

| Depth | Grok slots (ET) | Universe scan | Deep research | Tape / 8-K promote |
|-------|-----------------|---------------|---------------|--------------------|
| `light` | 6:00am | holdings+watch prices only | holdings news only | no |
| `holdings_watchlist` | 8:45am, 12:00pm, 2:00pm | **mini-scan** held+watch only (`full_universe_scan=false`) | held + watch + trigger alerts + tape/8-K promotions | **yes** (`tape_promote_max` 5, `filings_sweep` on) |
| `full` | 10:30am, 3:30pm | full universe | focus set + fat-pitch + tape/8-K + earnings reporters | **yes** |
| `weekly_market` | Sun overnight | full + discovery | holdings + standouts | yes |
| `evening_review` | 5:30pm | full context, no new buys | commentary | yes |

`holdings_watchlist` deliberately skips the ~180-name download. It must **not**
skip tape/8-K promotions or holdings deep-dive — those are the mid-day safety
net for catalysts on non-watch names. See
`tests/test_holdings_watchlist_research.py`.

## Critical lanes (fail loud)

`research_freshness` (top-level, also in the slim pack’s blocking set) reports:

- **price_as_of** — bar session dates from `universe_scan.prices_meta[T].price_as_of`
- **live_overlay** — age/status of `state/live_prices.json` (filled at decision
  time as `price_freshness_live`)
- **critical_lanes** — alive/empty for:
  - `news_and_catalysts`
  - `sec_filings`
  - `earnings_calendar` (committed `data/earnings_calendar.json`)

If any critical lane is empty/dead on a trading depth (`full`,
`holdings_watchlist`, `weekly_market`), gather prints a `!! RESEARCH FRESHNESS`
line, stamps `research_freshness.fail_loud`, and the slim pack promotes that
into `stale_data_notice` (+ a `data_quality` empty flag). Do not open new
positions on absent news/filings/earnings data.

## Other research blocks (by depth)

| Block | light | holdings_watchlist | full |
|-------|-------|--------------------|------|
| `market_news` + `todays_8ks` | no | yes (early, for promote) | yes |
| `sec_filings` / `deep_fundamentals` / `filing_texts` | stub | focus | focus |
| `smart_money_13f` / `ownership_flow` | skip | focus (13F `report_period` surfaced) | focus |
| `insider_activity` / options / partnerships | skip/limited | focus | focus |
| `earnings_week` + `earnings_lanes` | yes (cache read) | yes | yes |
| market radar / discovery | no | no | full / weekly |

## Grok Bot slots (see also `docs/GROK_SCHEDULE.md`)

| Time (ET) | Role | Depth |
|-----------|------|-------|
| 6:00am | light | `light` |
| 8:45am / 12:00pm / 2:00pm | primary trading | `holdings_watchlist` |
| 10:30am / 3:30pm | deep scan | `full` |
| 5:30pm | evening | `evening_review` (+ Fri self-review) |
| 7:00pm | study | `--study` (no trades) |
| 12:00am | news / Sunday weekly | `--news-only` / `weekly_market` |

Auth and runner details live in `docs/GROK_SCHEDULE.md`. This note is only the
research-lane map the brain should expect in context.
