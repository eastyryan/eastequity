# Adopted lessons (auto + weekly pipeline)

Soft lessons only. Hard code changes require owner approval.

### [2026-07-19] soft_lesson — LP-0693555695

** The deterministic trigger check fired on AMD purely because the *price* touched the parsed $500 level, but my published `would_buy_at` was a compound condition — *post-event* confirmation plus a 50-DMA reclaim, explicitly excluding a pre-event dip. The result is a full event-driven run spun up for a setup that was never actually live, five days before its own catalyst. The trigger parser could be materially sharper if it also parsed an *event/date gate* from the trigger text (e.g. "after 7/22-23") and suppressed or down-weighted the alert until that date passes, rather than firing on the pr

*Source run: 20260717-e6c993*

### [2026-07-19] soft_process — LP-2207714842

** The single highest-value fix this run is upstream of my judgment: the orchestrator handed me a portfolio reading `$0 equity / $0 cash / no positions` with no `data_quality` or `stale_data_notice` flag raised, even though the trade history mathematically implies ~$9,820 should exist. A cheap deterministic guard — "if broker equity is ~$0 but closed-trade math implies non-trivial cash, flag `broker_sync_suspect` and set `allows_new_buys: false` with a reason" — would prevent a future run from being invited to trade (this was even a trigger-driven run) against an account it cannot possibly fun

*Source run: 20260717-85be4b*

### [2026-07-19] soft_lesson — LP-1344551419

Daily study (technical_analysis): Stop distance vs. holding horizon: the √t noise band, and why entry location (not stop width) is the binding lever on multi-week swings - A stop has one job: fire on evidence the thesis broke, not on ordinary noise. Kaminski & Lo (SIFR RR 63, 2008; J. Financial Markets 18(C):234-254, 2014) prove that under a random walk, stop-loss rules

*Source run: 20260717-ed4649*

### [2026-07-19] soft_lesson — LP-2555191189

Daily study session produced no machine-readable lesson - prompt/parse needs attention.

*Source run: 20260717-d6c697*

### [2026-07-19] soft_process — LP-8200625986

** For non-focus scan names that surface as genuine top candidates (MU this run — #2 EPS revision, cheapest-in-group, strong-buy), the bundle gives me the numeric row but withholds the chart, filing texts, options-implied move, and deep fundamentals I need to actually underwrite an entry — so a legitimately compelling name gets defaulted to the watchlist partly for lack of data rather than lack of merit. The scanner should promote the top 1–2 non-focus setups (by swing_setup_score or upward-revision rank) into the focus set each run so they receive a chart + deep bundle, letting me deep-resear

*Source run: 20260714-3de446*

### [2026-07-19] soft_process — LP-4566902391

** Two process gaps cost research time today. First, candlestick charts did not render this run — `data/charts/` was empty even though the gather log said "rendering candlestick charts..."; the chart step appears to depend on the same blocked live yfinance feed rather than falling back to the relay bundle the way the rest of the pipeline does, so I judged entry geometry on numeric indicators alone with no visual confirmation of the actual price bars. Second, the watchlist-trigger-alert logic flagged AMD as "price trigger reached" when the price was actually 3.9% away from my stated $555 level 

*Source run: 20260714-35bb96*
