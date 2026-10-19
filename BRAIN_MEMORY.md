# East Equity Agent — Brain Memory

The living memory of the trading brain. Read fully at the start of every run,
update at the end of every run with what was learned. The journal/ directory
holds the raw episodic record (86 runs, 73 brain calls, 10 trades, 9 exit
autopsies, 77 improvements, 46 rejections as of 2026-10-16); this file is the
distilled semantic memory: what the brain believes, and why.

Brain: Muse (from 2026-10-18). Built by Grok. Deterministic Python enforces
safety, validates proposals, and executes. The brain's job is research quality
and thesis quality.

## Book state (updated 2026-10-16)

- Equity ~$990.84, roughly half in cash. Long-only, $1,000 paper book.
- Open: COST +8.0%, CVX +3.4%, NOW +3.8%, PSX flat, NTRA -1.3%.
- Closed: 5 trades, 0 wins, -$27.91 total. Four were 0.50-0.60 probes (-$18.66);
  four closed inside 0-7 days (-$24.09), nearly all stopped at about -1R.
  Latest closes: NTAP stopped (-$5.32), OKTA thesis exit (-$3.82).

## Standing rules (from the system's own measured history)

1. **Stops need room.** No new BUY unless the stop sits at least 1.5 ATR below
   the live entry. If the level cannot give that room, wait for a better price
   instead of buying a tight stop. (Adopted 2026-10-16 after a run of -1R stops.)
2. **Validator floors are minimums, not targets.** A setup that clears them
   honestly is tradeable; do not hold out for the best idea of the quarter.
   But never force a trade: a fabricated setup teaches the loops garbage.
3. **Indicators describe; they do not veto.** MACD crosses, ADX levels, RSI
   bands, volume confirmation legs were all measured over 12 years and carry
   no discriminating power as entry signals. Use them for geometry, timing,
   and description. The one mild exception: `no_supply_pullback` (+0.22/+0.25pp).
   Wilder's ADX and RSI are lagging descriptions, not gates.
4. **An unwind is a size haircut, not a trading halt.** Momentum-health
   "unwind" halves new-BUY size via code; it must never become a reason to
   propose nothing. Cite rejections on their own merits.
5. **No intraday print is a swing entry.** Overnight-gap auction buys and
   opening-range breakouts are the same rule: the swing thesis needs a
   completed session structure, not an intraday print.
6. **Theme risk is not sector risk.** Cap is on demand_driver (~35% of equity).
   Left-tail correlation with the AI stack and the AI factor-stack cap are one
   concentration rule, not two.
7. **Cite lessons every run.** Whenever a KB- or LP- lesson shapes a buy, skip,
   or wait, write its id in the proposal/rejected_idea/watchlist item and add a
   `lessons_applied` row. The scorecard can only grade cited lessons.
8. **Risk desk is local-deterministic until the LLM desk returns.** Geometry,
   stop-vs-noise, RR, and a full validator dry run decide; catalysts are NOT
   web-verified in this mode, so be stricter on sourced claims.

## Calibration

- Closed-trade count gates everything: calibration is anecdote under 5,
  caution at 5-14, binding at 15+. Conviction tier unlocks at 15, shadow book
  binds at 8. Objective of this phase: completed cycles and honest calibration
  data, not P&L maximization.
- Track record and exit lessons feed every run via the context bundle
  (track_record.breakdowns). Study them before proposing; do not repeat
  documented mistakes.

## Changelog

- 2026-10-18: Brain memory created (Muse brain v1). Seeded from the 2026-10-16
  weekly self-review and knowledge consolidation. Brain switched from Grok Bot
  to Muse; Grok Bot schedule paused by Easton.
