# Swing Style Evolution Log

INTERNAL engineering record of outside trading styles studied, what was adopted /
hybridized / rejected, and how each experiment performed. Adopted elements are
absorbed into the framework (CLAUDE.md + code) WITHOUT attribution in the brain's
public reasoning - the trade memo explains the trade, not its influences; this file
is where provenance lives. Experiments are graded in the weekly self-review
(improvement notes tagged [style-log]). Code-enforced rules always outrank style
preferences; owner policy outranks everything.

---

## 2026-07-13 — Study: @investingluc (Luc) swing philosophy

Source: owner-provided summary of Luc's public philosophy, technicals, and
"Swing Stride" process. Evaluated element-by-element against the existing
framework. Verdicts: **ADOPTED** (new, integrated), **ALREADY CORE** (we do this;
sharpened language at most), **HYBRIDIZED** (took the useful part), **REJECTED**
(conflicts with owner policy or system design — with reason).

### Philosophy & mindset

| Element | Verdict | Notes |
|---|---|---|
| "Less is more" — minimal toolset, overcomplication destroys edge | HYBRIDIZED | Our DATA pipeline stays deep (fresh XBRL, insiders, 13F, options, partnerships — depth is our edge and it feeds one brain, not a dashboard of conflicting indicators). But DECISION discipline adopts it: a thesis must stand on 2-3 pillars stated plainly; if it needs ten indicators to justify, it is not a fat pitch. |
| Patience/selectivity: flat for weeks is smart; 90% of gains from 10% of positions; never force trades | ALREADY CORE, SHARPENED | "Proposing nothing is a good outcome" predates this study. Adopted Luc's framing: the FAT PITCH standard — days/weeks of nothing is the strategy working, not failing. Added explicitly to CLAUDE.md. |
| ~70% effort on business/story/catalyst research; conviction internally owned | ALREADY CORE | The entire gather bundle exists for this. No change. |
| Long bias, ride trends, ~90% join strength / ~10% bottoms | ALREADY CORE | Momentum funnel is ~90% of surfaced names; contrarian + deep-value-200W lanes are the ~10%. Ratio matches by construction. |
| Mental toughness / manage fear-greed-ego | ALREADY CORE (STRUCTURAL) | We do this better than any human can promise to: the validator, risk desk, exit guard, and calibration tracking ARE the discipline — they cannot have a bad day. |
| Environment awareness: only press in favorable markets; watch leadership rolling over | **ADOPTED (code)** | New `benchmark_trend` in the scanner (SPY vs its 50/200-DMA) + existing macro regime/VIX/HY-spread. CLAUDE.md now gates aggressiveness on it: hostile tape → smaller size, higher bar, more cash. Leadership check: sector_relative_strength already tracks rotation. |
| "Investing" mindset: hold until the reason disappears or price reflects it | ALREADY CORE | Re-underwrite every cycle + cash test + "target is a milestone, not a tripwire." |

### Technicals & setups

| Element | Verdict | Notes |
|---|---|---|
| Minimal indicators: price, volume, volume profile, 50/200 SMA | HYBRIDIZED | We keep the funnel's richer feature set (it screens 107 names; a human eyeballs 5). The brain's chart READS (candlestick PNGs) already center on price/volume/MA structure. Volume profile: not adopted — needs intraday data we don't ingest; the 20d-high/low + base-tightness reads cover the use case at swing timeframe. |
| Timeframes 4H/Daily/Weekly | HYBRIDIZED | Daily + weekly (200W MA) adopted/present. 4H REJECTED: intraday-adjacent, conflicts with the 3-90d swing mandate and the run cadence. |
| "Ready to move": off lows, relative strength, positive news reaction, aggressive dip buying | ADOPTED (language) | All measurable pieces already exist (rel_strength vs SPY, pullback metrics, pct_change_1d, volume_surge). CLAUDE.md now names the composite as the "ready-to-move" check on entries. |
| Coiling/tightening bases, volume-backed breakouts, MA reactions | ALREADY CORE | Chart PNGs + pullback/volume metrics; brain instructed to read bases visually. |
| Heavy volume + tight candles = accumulation/absorption | NOTED, NOT CODED | Real pattern; brain can see it on the chart PNGs. Not adding another scanner flag — "less is more" applies to our own feature creep too. Revisit if the brain repeatedly misses it. |
| Asymmetric R/R: 30-50%+ upside, 1:4+ risk/reward | **REJECTED as hard rule, ADOPTED as preference** | Owner policy (July 2026, set twice) is explicit: target upside ≥10%, RR floor 1.0 — compounding 10-15% swings is the mandate, not lottery hunting. Validator floors UNCHANGED. Soft adoption: when a genuine 1:3+ fat pitch appears, it deserves priority and full size over a marginal 1:1.2. |

### The "Swing Stride" process

| Stride | Verdict | Notes |
|---|---|---|
| 1. Research/theme | ALREADY CORE | The bundle. |
| 2. Technical confirmation on higher timeframes | ALREADY CORE | Daily structure + 200W context + chart reads. |
| 3. Entry scale-in (enter on strength, add on pullbacks in uptrend) | **ADOPT-IN-PRINCIPLE, BLOCKED BY DESIGN — ENGINEERING ITEM** | The broker/validator deliberately block adds ("already_holding_ticker") and closed-trade accounting pairs single entries. Proper scale-in needs: broker position-merging (avg-cost math), validator add rules (per-name total cap, add only above cost in uptrend), and pairing updates. Logged as the next style experiment; until then the brain may size initial entries at 50-70% of cap and put the add level in the watchlist (would_buy_at) so the intent is public. |
| 4. Risk stride: pre-planned exits, hard stops zero exceptions, time stops | ALREADY CORE + **ADOPTED (code)** | Hard stops pre-planned and machine-enforced (exit_guard, closing basis) since day one. NEW: stall detection — `position_stop_cushion` now flags positions ≥14 days old that have gone nowhere (±3%) as `stalled`; the brain must justify the opportunity cost or rotate (Luc's time-stop, softened to a forced decision rather than a forced exit — the horizon force-close remains the hard time stop). |
| 5. Management: hold through volatility, extend for exceptional stories, trail/partial on runners | ALREADY CORE (bounded) | Thesis-intact holding + cash test exist. "Extend timeframes" is bounded by the 90d hard ceiling (owner mandate) — an exceptional story gets re-proposed as a NEW swing with fresh thesis, not silently extended. Partials/trailing: not supported by broker (single exit); noted with scale-in as one engineering package. |
| 6. Portfolio/environment: selective, size up with conviction, press only in support | ALREADY CORE + ADOPTED | Conviction tier (earned, gated) + new benchmark_trend gate. |

### His examples, through our rules
- ORCL @140 / stop 135 / target 200+: passes our validator geometry (RR ~8.7, stop 3.6%... would FAIL the volatility stop floor if ATR-noise > 3.6% — our floor would force the honest wider stop). Good case study of our machinery improving his idea.
- SOFI CEO-buying + weekly chart: our insider cluster-buy detector + 200W context produce this setup natively.
- $PURR break $9: **REJECTED CLASS** — sub-$1B micro-cap breakout; blocked by the $1B floor (owner rule, 2026-07-13) and off-universe anyway.

### Experiments now live (grade in weekly self-review)
1. `stalled` flag on holdings → does forcing the opportunity-cost decision improve rotation? (metric: avg days-held on eventual losers/flats)
2. `benchmark_trend` gating → fewer new BUYs opened into SPY-below-200DMA tape; compare entry quality across regimes once samples exist.
3. Fat-pitch preference language → watch whether stated RR on proposals drifts up without forcing (calibration section tracks confidence honesty).

---
(Brain: append future style studies below with the same verdict table format. Tag
related improvement-journal notes with [style-log].)
