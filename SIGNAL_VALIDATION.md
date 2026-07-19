# Signal validation — the unmeasured lanes, measured

**Date:** 2026-07-19
**Harness:** `scripts/signal_study.py` (generalized from `scripts/sepa_study.py`)
**Predicates:** `tools/signal_lanes.py`
**Sample:** 182 names x 12 years, 86,047 observations, 2,355 distinct dates, step = 5 sessions
**Tests:** `tests/test_signal_lanes.py`, `tests/test_signal_study.py` (47 tests)

---

## Summary

This repo had validated exactly one signal. It did so properly and it rejected it. The
SEPA study found that the trend template's +1.15pp naive separation was a
**volatility-selection artifact** — the gate picked higher-ATR names, "names that move
more, not names that move better" — and it vanished once the book's real ATR-width stop
was applied. The VCP layer measured negative and was withheld from the model outright.

Roughly ten other lanes ship to the decision-maker having never been measured. Four are
reconstructible from a price panel. All four were measured. **None of them shows an edge
that survives a volatility control.**

| Lane | Naive 21d excess | Book's rules 21d excess | **ATR-matched 21d excess** | ATR ratio | Verdict |
|---|---|---|---|---|---|
| contrarian_reversal (top 5) | +0.74 | +0.60 | **+0.16** | 1.28 | volatility-selection artifact |
| deep_value_200w (top 5) | +0.76 | +0.88 | **+0.28** | 1.30 | no edge; 2021-dependent |
| supplier_pullback (top 5) | +1.21 | +0.74 | **+0.45** | 1.19 | no edge; label lookahead |
| — same setup, no AI label (ablation) | +0.13 | +0.12 | **+0.07** | 1.15 | the label was the whole result |
| EAR gap_held (price leg) | +0.15 | −0.04 | **−0.07** | 1.36 | no edge; mildly inverted |
| *(context)* top_setups, score top-15 | +0.21 | −0.04 | **−0.16** | 1.43 | negative |

All figures are percentage points of excess return vs the same-day universe, 21 sessions,
entered at the next bar's open. Full tables below.

The single most important column is the third one, and it did not exist in the SEPA
study. See "The control that decides everything."

---

## Method

Everything that made the SEPA result trustworthy is carried over unchanged. The four
non-negotiables, restated because they are the whole reason to believe any number here:

1. **Cross-sectional, same-day.** The metric is the selected group's return minus the
   same-day mean of every name scanned. `data/universe.json` is survivors-only. Absolute
   returns off that pool are meaningless — NVDA is in the universe because it went up.
   Comparing two groups drawn from the same biased pool on the same day cancels most of
   the bias along with market and sector beta. **Never read an absolute column as edge.**
2. **No lookahead.** The signal at bar *t* uses `bars[:t+1]`; the trade enters at bar
   *t+1*'s **open**. `test_run_signal_cannot_see_the_future` corrupts every bar after a
   cut point and asserts the earlier signals are byte-identical.
3. **Scored under the book's actual risk rules.** 2xATR initial stop, 3xATR chandelier
   trail, ratchet-only — what `validator.py` and the safety layer really do. A naive
   fixed-horizon test is what produced the SEPA false positive, so both are always run
   and reported side by side.
4. **Overlapping samples are not independent.** Step-5 sampling with a 21- or 63-session
   horizon means neighbouring observations share most of their window, and this universe
   is one correlated AI/semis theme besides. The effective sample is far below *n*. A
   per-year breakdown is printed and **no p-value is** — the independence assumption
   behind one does not hold.

Two structural changes were forced by pointing this at a full universe:

- **Slicing moved inside the signal call.** `sepa_study.run` stored `closes[:i+1]` and
  three more full-history slices on *every* observation record. At 183 names x ~530
  sample bars that is tens of gigabytes. `signal_fn` now receives whole arrays plus an
  index and slices for itself; a record's memory is O(1) in history length.
- **The cross-sectional pass was kept and made central.** Three of the four lanes are
  *top-N selections*, not filters — they rank and take five. A lane measured without its
  rank step is a different signal from the one that ships. `cross_fn` replays the
  scanner's full lane cascade (top-15 by `swing_setup_score` taken off the board first,
  then contrarian, then deep-value, then supplier, each seeing only what the previous
  did not take). Every lane is therefore reported twice: the **raw predicate** (larger
  *n*, the cleaner statistical question) and the **top 5 shipped** (what the brain sees).

### The control that decides everything

`atr_selection_check` reports what the SEPA study observed: the ratio of selected to
unselected ATR. But an observation is not a verdict. **Every lane here selects higher-ATR
names** — ratios run 1.15 to 1.49, with no exceptions — so that check alone cannot
separate them.

`summarize_atr_matched` turns it into a control. Each day's scanned names are split into
ATR quintiles and a selected name is benchmarked **only against names of comparable
volatility on the same day**. If a lane's excess survives, it picks names that move
*better*. If it collapses toward zero, it picked names that move *more*.

Every lane collapsed. Two went negative.

The control is deliberately **conservative**: with quintiles, the bucket straddling the
selection boundary contains both selected and unselected names, so a pure volatility tilt
leaks a small positive residual through it. In the synthetic case where return is *purely*
a function of ATR, the control removes ~85% of a fake edge, not 100%
(`test_atr_matched_excess_collapses_a_pure_volatility_tilt` asserts exactly this, and
says so). **The residual positives in the table above are consistent with zero true edge.**
This understates the problem rather than overstating it, which is the correct direction
for a harness whose job is to reject things.

---

## Results

### 1. Naive fixed-horizon — excess vs same-day universe mean (pp)

| group | h | n | excess | median | beat% |
|---|---|---|---|---|---|
| contrarian (raw pred) | 21 | 5,995 | +0.51 | −0.18 | 48.4 |
| contrarian (raw pred) | 63 | 5,995 | +1.18 | −1.07 | 46.7 |
| contrarian (top5 shipped) | 21 | 2,045 | +0.74 | +0.05 | 50.4 |
| contrarian (top5 shipped) | 63 | 2,045 | +1.23 | −1.54 | 47.2 |
| deep_value (raw pred) | 21 | 7,849 | +0.22 | −0.17 | 49.0 |
| deep_value (top5 shipped) | 21 | 1,919 | +0.76 | +0.22 | 51.0 |
| deep_value (top5 shipped) | 63 | 1,919 | +2.34 | +1.54 | 53.6 |
| supplier_pb (raw pred) | 21 | 6,249 | +1.42 | +0.37 | 51.4 |
| supplier_pb (raw pred) | 63 | 6,249 | +5.31 | +1.13 | 52.8 |
| supplier_pb (top5 shipped) | 21 | 2,397 | +1.21 | +0.20 | 50.8 |
| supplier_pb (top5 shipped) | 63 | 2,397 | +4.65 | +0.29 | 51.4 |
| pullback ABLATION (no label) | 21 | 19,707 | +0.13 | −0.36 | 47.8 |
| pullback ABLATION (no label) | 63 | 19,707 | +0.67 | −1.22 | 46.2 |
| top_setups (score top15) | 21 | 15,811 | +0.21 | −0.08 | 47.4 |
| EAR gap_held | 21 | 12,271 | +0.15 | −0.37 | 47.9 |
| EAR gap_faded | 21 | 1,603 | −0.46 | −1.06 | 44.5 |
| EAR gap_filled | 21 | 5,996 | +0.63 | −0.25 | 48.3 |
| EAR down_gap | 21 | 17,204 | +0.66 | 0.00 | 50.0 |

Note the **means are positive while the medians are negative** almost everywhere. That is
the right-skew of a trend-following payoff, not a broad win: most observations are
slightly negative and a small tail carries the average. Any lane whose case rests on the
mean is resting on that tail.

The supplier lane's +5.31pp at 63 sessions is the largest number on this page. It is also
almost entirely fake — see §4.

### 2. The book's real rules — 2xATR stop + 3xATR chandelier trail

| group | h | n | mean | excess | win% | stop% | avgW | avgL | payoff | MFE | MAE |
|---|---|---|---|---|---|---|---|---|---|---|---|
| contrarian (raw) | 21 | 5,995 | 1.99 | +0.41 | 45.6 | 67.4 | 11.60 | −6.06 | 1.91 | 10.52 | −5.86 |
| contrarian (raw) | 63 | 5,995 | 2.89 | +0.46 | 42.9 | 95.0 | 14.82 | −6.07 | 2.44 | 13.67 | −5.99 |
| contrarian (top5) | 21 | 2,045 | 1.88 | +0.60 | 45.5 | 68.1 | 11.09 | −5.80 | 1.91 | 9.77 | −5.68 |
| contrarian (top5) | 63 | 2,045 | 2.81 | +0.89 | 42.3 | 95.7 | 14.48 | −5.75 | 2.52 | 12.85 | −5.81 |
| deep_value (raw) | 21 | 7,849 | 2.97 | +0.40 | 50.2 | 60.5 | 11.19 | −5.34 | 2.10 | 10.21 | −5.07 |
| deep_value (top5) | 21 | 1,919 | 2.39 | +0.88 | 46.9 | 64.6 | 11.43 | −5.60 | 2.04 | 10.02 | −5.34 |
| deep_value (top5) | 63 | 1,919 | 3.54 | +1.25 | 44.1 | 94.4 | 15.20 | −5.67 | 2.68 | 13.59 | −5.52 |
| supplier_pb (raw) | 21 | 6,249 | 2.36 | +0.85 | 48.3 | 64.5 | 10.86 | −5.58 | 1.95 | 10.06 | −5.40 |
| supplier_pb (top5) | 21 | 2,397 | 2.03 | +0.74 | 46.4 | 65.6 | 10.38 | −5.19 | 2.00 | 9.27 | −5.21 |
| supplier_pb (top5) | 63 | 2,397 | 2.64 | +0.72 | 42.1 | 95.0 | 13.29 | −5.11 | 2.60 | 11.96 | −5.40 |
| **pullback ABLATION** | 21 | 19,707 | 1.61 | **+0.12** | 45.9 | 64.2 | 9.27 | −4.89 | 1.89 | 8.30 | −4.84 |
| **pullback ABLATION** | 63 | 19,707 | 2.39 | **+0.19** | 42.0 | 94.3 | 12.45 | −4.90 | 2.54 | 11.12 | −5.01 |
| top_setups (top15) | 21 | 15,811 | 1.61 | −0.04 | 45.1 | 67.4 | 10.77 | −5.92 | 1.82 | 9.90 | −5.85 |
| top_setups (top15) | 63 | 15,811 | 2.42 | −0.15 | 41.0 | 95.8 | 14.37 | −5.87 | 2.45 | 12.95 | −6.03 |
| EAR gap_held | 21 | 12,271 | 1.62 | −0.04 | 46.9 | 60.4 | 9.97 | −5.76 | 1.73 | 9.08 | −5.53 |
| EAR gap_held | 63 | 12,271 | 2.84 | +0.01 | 42.8 | 91.2 | 14.32 | −5.75 | 2.49 | 12.58 | −5.73 |
| EAR gap_faded | 21 | 1,603 | 1.97 | −0.47 | 48.3 | 59.6 | 10.27 | −5.78 | 1.78 | 9.67 | −5.59 |
| **EAR gap_filled** | 21 | 5,996 | 2.50 | **+0.44** | 48.9 | 59.5 | 11.51 | −6.14 | 1.87 | 10.97 | −5.77 |
| **EAR down_gap** | 21 | 17,204 | 2.22 | **+0.46** | 48.3 | 58.6 | 10.84 | −5.84 | 1.86 | 9.81 | −5.60 |
| EAR down_gap | 63 | 17,204 | 3.49 | +0.72 | 44.2 | 90.8 | 15.29 | −5.86 | 2.61 | 13.72 | −5.82 |

Two things worth stating plainly:

- **Stop-out rates at 63 sessions are 91–96% for every group, including the universe.**
  The chandelier trail essentially always fires eventually on a 3-month hold. That is the
  trail working as designed, not a lane failing — but it means the 63-session column is
  measuring "how far did the trail ratchet before it fired," and differences between
  groups there are differences in trend persistence, heavily tail-driven.
- **Payoff ratios (1.7–2.8) and MFE/MAE ratios are near-identical across every group,
  including the ones with no selection at all.** That is the chandelier's signature, not
  any lane's. It is the same observation the SEPA study made — "a near-identical MFE/MAE
  ratio" — and it recurs here for all four lanes.

### 3. Volatility-selection check

| group | n_sel | n_unsel | ATR%_sel | ATR%_unsel | ratio |
|---|---|---|---|---|---|
| contrarian (raw) | 5,995 | 80,052 | 3.99 | 2.84 | **1.403** |
| contrarian (top5) | 2,045 | 84,002 | 3.71 | 2.90 | **1.279** |
| deep_value (raw) | 7,849 | 78,198 | 3.77 | 2.84 | **1.330** |
| deep_value (top5) | 1,919 | 84,128 | 3.77 | 2.90 | **1.299** |
| supplier_pb (raw) | 6,249 | 79,798 | 3.68 | 2.86 | **1.287** |
| supplier_pb (top5) | 2,397 | 83,650 | 3.45 | 2.91 | **1.188** |
| pullback ABLATION | 19,707 | 66,340 | 3.24 | 2.83 | **1.147** |
| top_setups (top15) | 15,811 | 70,236 | 3.87 | 2.71 | **1.429** |
| EAR gap_held | 12,271 | 73,776 | 3.77 | 2.78 | **1.355** |
| EAR gap_filled | 5,996 | 80,051 | 4.20 | 2.82 | **1.488** |
| EAR down_gap | 17,204 | 68,843 | 3.93 | 2.67 | **1.473** |

**Every lane selects higher-volatility names. There are no exceptions.** This is not a
property of any one lane's logic; it is what "20% off the high," "below the 200-week MA,"
"8–30% pulled back," and "gapped 2%+" have in common. Volatile names are the ones that
get 20% off their highs and the ones that gap.

### 4. ATR-matched excess — the verdict table

Excess under the book's rules vs same-day, **same-volatility-bucket** peers.

| group | h | n | excess | median |
|---|---|---|---|---|
| contrarian (raw) | 21 | 4,959 | **−0.03** | −1.30 |
| contrarian (raw) | 63 | 4,959 | **−0.23** | −1.87 |
| contrarian (top5) | 21 | 2,045 | **+0.16** | −1.52 |
| contrarian (top5) | 63 | 2,045 | **+0.18** | −2.12 |
| deep_value (raw) | 21 | 6,706 | **+0.22** | −0.99 |
| deep_value (raw) | 63 | 6,706 | **+0.06** | −1.66 |
| deep_value (top5) | 21 | 1,919 | **+0.28** | −1.30 |
| deep_value (top5) | 63 | 1,919 | **+0.20** | −1.95 |
| supplier_pb (raw) | 21 | 5,453 | **+0.47** | −1.06 |
| supplier_pb (raw) | 63 | 5,453 | **+0.76** | −1.75 |
| supplier_pb (top5) | 21 | 2,397 | **+0.45** | −1.04 |
| supplier_pb (top5) | 63 | 2,397 | **+0.36** | −1.80 |
| pullback ABLATION | 21 | 17,278 | **+0.07** | −1.13 |
| pullback ABLATION | 63 | 17,278 | **+0.12** | −1.74 |
| top_setups (top15) | 21 | 8,085 | **−0.16** | −1.28 |
| top_setups (top15) | 63 | 8,085 | **−0.34** | −1.99 |
| EAR gap_held | 21 | 10,680 | **−0.07** | −1.15 |
| EAR gap_held | 63 | 10,680 | **−0.12** | −1.90 |
| EAR gap_filled | 21 | 4,978 | **+0.29** | −1.13 |
| EAR down_gap | 21 | 14,856 | **+0.18** | −0.93 |

Given that the control leaves ~15% of a *purely* synthetic volatility tilt in place, every
number in this table is consistent with zero.

### 5. Stability by year — 21d excess, book's rules

| group | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| contrarian (top5) | −1.35 | +1.07 | +0.36 | +0.55 | +0.56 | +1.72 | +1.51 | −0.61 | +1.51 | +0.82 | +0.85 | **−3.79** |
| deep_value (top5) | – | – | – | −0.15 | +0.79 | −0.84 | **+4.04** | +0.86 | +1.80 | +1.04 | −0.04 | +0.05 |
| supplier_pb (top5) | −0.09 | +0.85 | +0.32 | −1.68 | +1.46 | +0.88 | +0.11 | −1.21 | +0.27 | +0.48 | **+2.89** | **+10.68** |
| pullback ABLATION | −0.65 | +0.14 | +0.13 | −0.73 | +0.33 | +0.39 | +0.43 | +0.04 | −0.21 | −0.02 | +0.32 | +1.21 |
| top_setups (top15) | +0.18 | +0.95 | −0.58 | −0.14 | −0.64 | +0.22 | −0.73 | −0.26 | −0.05 | +0.01 | +0.55 | +0.35 |
| EAR gap_held | −0.79 | −0.19 | −0.38 | −0.36 | +0.29 | +0.56 | −0.40 | −0.67 | +0.47 | −0.45 | +0.06 | +1.56 |

### 6. Regime-dependence — drop the year that carries the result

| lane | window | n | book excess | ATR-matched |
|---|---|---|---|---|
| supplier_pb (top5) | all 12y | 2,397 | +0.74 | +0.45 |
| supplier_pb (top5) | **pre-2025** | 2,096 | **+0.18** | **+0.07** |
| supplier_pb (raw) | all 12y | 6,249 | +0.85 | +0.47 |
| supplier_pb (raw) | **pre-2025** | 4,974 | **+0.40** | **+0.23** |
| deep_value (top5) | all 12y | 1,919 | +0.88 | +0.28 |
| deep_value (top5) | **ex-2021** | 1,715 | **+0.50** | **−0.16** |
| deep_value (raw) | all 12y | 7,849 | +0.40 | +0.22 |
| deep_value (raw) | **ex-2021** | 7,475 | **+0.28** | **+0.07** |

The supplier lane's 2026 cell (+10.68) rests on **70 observations** — 14 sample dates x 5
names in a seven-month partial year. The deep-value lane's entire pooled result is 2021,
the value/reflation rotation. Remove one year from each and both are nothing.

---

## Per-lane verdicts

### contrarian_reversal — VOLATILITY-SELECTION ARTIFACT

`pct_from_52w_high <= -20 and momentum_1m_pct > 2`, top 5 by 1-month momentum.

Naive 21d excess +0.74pp on the shipped top 5, holding at +0.60 under the book's real
rules — the first lane here that does *not* die on contact with the stop, which is more
than SEPA managed. Then it dies on the volatility control: **+0.16pp**, inside the
control's own known leakage. The selection runs 1.28x the universe's ATR, and the naive
number is that ratio, not skill.

Year-to-year it changes sign five times and posts its worst year (−3.79) in the most
recent one. The median is negative at every horizon (−1.52pp at 21d) while the mean is
positive: this is a small tail carrying a losing majority.

**Recommendation: KEEP shipping it, as a declared lens with a validation note.**
Not because it has edge — it does not — but because its documented job in `CLAUDE.md` is
funnel diversification ("the momentum funnel never surfaces these"), and it measures
approximately *zero*, not negative. That is the trend-template case, not the VCP case.
The precedent set in `sepa_trend.py` fits exactly: keep the structure label, attach a
`VALIDATION_NOTE` saying no measured edge, and forbid sizing or confidence off it.
Language in `CLAUDE.md` implying these names are opportunities the funnel *misses* should
be softened to reflect that they are simply names it does not *show*.

### deep_value_200w — NO EDGE, AND WHAT LOOKS LIKE EDGE IS 2021

`liquid and is_full_200w_window and pct_vs_200w_ma <= 2.0 and _screen_quality_ok(...)`, top 5.

The best-looking lane on naive numbers at 63 sessions (+2.34pp, 53.6% beat rate, positive
median) — and the one whose result is most concentrated in a single regime. ATR-matched:
**+0.28pp at 21d, +0.20 at 63d**. Drop 2021 and the shipped top-5 goes **negative**
(−0.16pp ATR-matched). The pooled result is the 2021 value rotation with eight years of
noise attached.

Two measurement caveats, both of which make this *more* favourable than reality:

- `_screen_quality_ok` reads a live estimate cache with no history. It fails open on
  missing data, i.e. on every historical bar, so what was measured is the lane **without**
  its value-trap filter. That filter's absence should hurt the lane, and the lane still
  did not clear.
- Only 9 of 12 years produce observations at all — the 200-week window needs ~1,000 prior
  sessions, so nothing before 2018 qualifies.

**Recommendation: KEEP shipping it, as a lens, with the 2021-dependence stated.** The
`CLAUDE.md` block already does the right thing by calling it "a lens, not a buy signal"
and demanding the brain verify the business independently. That framing is now *measured*
rather than merely prudent, and should say so. What must not survive is any implication
that proximity to the 200-week MA is itself predictive. It is not, in this universe, in
any year except 2021.

### supplier_pullback — NO EDGE; THE MEASURED EFFECT IS THE LABEL, NOT THE SETUP

`ai_exposure == "ai_supplier" and liquid and is_full_52w_window and above_200dma and -30 <= pct_from_52w_high <= -8`, top 5 by 3-month RS.

This lane posts the biggest naive number in the study (+5.31pp at 63d, raw predicate) and
it is the clearest false positive. The ablation settles it.

`supplier_pullback_pred_no_label` is the identical predicate with the `ai_exposure` test
removed — same liquidity, same 52-week window, same 200-DMA, same −30/−8 band. It is a
control, not a lane. Result:

| | book excess 21d | ATR-matched 21d |
|---|---|---|
| with the `ai_supplier` label | +0.85 | +0.47 |
| **without it (setup only)** | **+0.12** | **+0.07** |

**The setup contributes nothing. The entire measured effect is the label.** And the label
is `data/ai_exposure.json`, a **2026 classification applied to 2015 bars** — a name marked
`ai_supplier` today was not knowably one in 2018. It encodes, with perfect hindsight,
which companies the AI buildout went on to enrich. That is lookahead, and the by-year row
confirms the mechanism: the lane is flat-to-negative through 2024 and then prints +2.89
(2025) and +10.68 (2026, n=70) exactly when the AI supply chain ran. Pre-2025, ATR-matched
excess is **+0.07pp**.

**Recommendation: KEEP shipping it, but WITHHOLD the ranking claim, and correct the
framing.** The lane is a legitimate *attention router* — it points the brain at AI
suppliers on pullbacks, which is a defensible thing to want given the mandate. It is not
a setup with measured edge, and the "extended leader coming back in" pattern specifically
measured as nothing once the label is removed. The 3-month-RS ordering has no measured
basis and should not be presented as a quality ranking. `CLAUDE.md` already requires
entries here to "need the reclaim/base confirmation the momentum lanes demand" — that
requirement is now the *only* thing carrying this lane, and the lane itself should be
documented as contributing zero.

### earnings_reaction / post_earnings_drift_candidate — NO EDGE ON THE MEASURABLE HALF; NEEDS MORE DATA ON THE OTHER

The shipping flag requires `reaction in ("gap_held", "rs_positive") AND revisions == "up"`.

**The revisions leg cannot be backtested.** `revision_direction` is a point-in-time
analyst-estimate field; no free source publishes the as-of revision state for an arbitrary
past date. Since the flag *requires* it, the lane as it ships cannot be measured. That is
not a gap I can close and it should not be papered over.

What *can* be measured is the price leg — the gap-and-hold reaction, which is the half
Brandt, Kishore, Santa-Clara & Venkatachalam (2008) is cited for in the scanner's own
evidence note. It measures nothing, and the ordering is wrong:

| reaction | book excess 21d | ATR-matched 21d | the lane treats it as |
|---|---|---|---|
| **gap_held** | −0.04 | **−0.07** | **the tradeable footprint** |
| gap_faded | −0.47 | −0.27 | a failure |
| gap_filled | +0.44 | **+0.29** | "a failed move" |
| down_gap | +0.46 | **+0.18** | "a negative reaction" |

`gap_held` — the one read the lane flags — is the **worst** of the four. The two reads the
lane explicitly labels failures both score *better* than it. The differences are all
inside the control's noise, so the honest statement is not "the lane is backwards"; it is
**the reaction classification has no measured discriminating power at all, in either
direction.** The claim in `CLAUDE.md` that "a filled gap or negative reaction is a failed
print, not a dip to buy" is not supported by 12 years of this universe.

Caveat that cuts in the lane's favour: without real earnings dates, `gap_held` here means
"an unfilled, held +2% gap in the last 10 sessions" — earnings gaps plus every other kind.
The true earnings-window subset may behave differently. Reconstructing it needs
point-in-time earnings dates, which yfinance provides only patchily and only for recent
years.

**Recommendation: WITHHOLD the reaction half; NEEDS MORE DATA on the revisions half.**
This is the VCP case, not the trend-template case. The lane ships a specific, confident,
research-cited claim — "an up-gap on the print that does NOT fill is the tradeable
footprint" — and the price evidence does not support it. A signal that rigorous-looking
with that record invites the brain to act on it, which is precisely the reasoning that
withheld VCP. Concretely: stop setting `post_earnings_drift_candidate` on the gap read
alone, and stop telling the brain that `gap_filled` / `down_gap` are disqualifying. The
revisions leg (Gleason & Lee, strongest under thin coverage) remains the theoretically
best-supported part and is untested — it should be **forward-tested**, not trusted.

### Context finding: the scanner's own primary ranking measures negative

Not in scope and not mine to change, but it fell out of the same run and is too material
to omit. `swing_setup_score`'s top 15 — the main ranking that drives the whole scan, and
the population the three lanes are defined as *excluding* — scores **−0.04pp (21d) and
−0.15pp (63d)** under the book's rules, and **−0.16 / −0.34 ATR-matched**, on n=15,811.
It also has the highest volatility ratio of any group measured (1.43).

This has a mechanical implication for everything above: the lanes are defined against a
baseline that is itself slightly negative, which *flatters* their relative numbers. It
deserves its own study by whoever owns `universe_scanner.py`.

---

## What could not be measured, and why

**Not backtestable from a price panel. Do not attempt with this harness.**

- **`ownership_flow`** — a composite of 13F, volume-tape sponsorship, and options heat.
  No point-in-time as-of reconstruction exists for the institutional legs.
- **`insider_activity` / cluster buying** — Forms 3/4/5 are retrievable historically, but
  the *classification* (discretionary vs 10b5-1, entity vs officer) depends on footnote
  parsing of the filing as it stood, and the 120-day rolling window is an as-of construct.
- **`smart_money_13f`** — quarter-over-quarter change across tracked funds. 13F is filed
  45 days after quarter end; the as-of-date a signal would have been visible is not the
  quarter date, and the tracked-fund list is a present-day choice.
- **`options_signals` / IV rank / term structure / skew** — historical option chains are
  not available free, and `iv_rank` is defined against this system's own stored history,
  which begins when the system began.
- **`partnerships`** — 8-K item 1.01 filings are retrievable, but the lane's value is in
  the *judgment* layer (quantified vs slideware, who needs whom), which is not a
  computable field.
- **`analyst_estimates` / `revision_direction` / `fwd_pe_est` / `fundamental_screen`** —
  point-in-time estimate snapshots. This is what blocks the EAR lane's revisions leg, and
  it is also why `_screen_quality_ok` was a no-op in the deep-value test.

These can only be **forward-tested**.

**The forward-test corpus exists but is far too short.** `state/` holds **176 archived
point-in-time bundles** (125 slim + 51 full), spanning **2026-07-09 to 2026-07-19 — 11
calendar days**. That is the right seed: each bundle is a genuine as-of snapshot of
exactly the fields above, captured before the outcome was known, which is the only way
these lanes will ever be measurable. But 11 days does not support a 5-day forward return,
let alone 21 or 63. At the current ~16 bundles/day, a 63-day forward window on the
*earliest* bundle can first be scored around **October 2026**, and a sample large enough
to survive the independence problems described in §4 of the method is a 2027 question.

Recommendation: keep archiving, add nothing that depends on these lanes being right, and
revisit when the corpus supports a 21-day window with more than a few dozen independent
observations.

---

## Survivorship bias — how it biases these results

`data/universe.json` is 182 names **curated today**, partly *because* they already worked.
There is no point-in-time universe history (git goes back days). This biases the study in
three distinct ways, and only the first is neutralised:

1. **Absolute returns are inflated, badly.** Every "mean" column in §2 is positive
   (+1.6% to +4.4% per 21 sessions) partly because the pool is winners. This is why the
   study reports **excess** vs the same-day pool and why `test_summarize_...` pins that
   behaviour. Read no absolute column as edge. This part *is* handled.

2. **Cross-sectional comparison does NOT cancel it when a lane's selection correlates
   with the reason a name survived.** This is the real problem and it bites hardest on
   the lanes that buy weakness. `contrarian_reversal` and `deep_value_200w` select names
   that are 20%+ off their highs or below their 200-week MA — i.e. names in serious
   trouble. In a real universe, some fraction of those never come back; they get acquired
   at a discount, delist, or grind to irrelevance and are dropped. **Every such name is
   absent here by construction.** The measured contrarian and deep-value results are
   therefore drawn exclusively from drawdowns that *resolved upward enough for the name
   to still be in the universe in 2026*. The true numbers are worse — plausibly much
   worse — and I cannot bound by how much. That both lanes measured ~zero *even with this
   tailwind* is the strongest statement in this document.

3. **`supplier_pullback` carries a second, independent lookahead** on top of survivorship:
   the `ai_exposure` label itself (see §4 verdict). Survivorship selects the names;
   the label then selects, with hindsight, the subset of them that won the AI buildout.

The asymmetry is what makes the conclusions safe: survivorship pushes every lane's
measured performance **up**, and every lane still measured at zero. A "no edge" finding
under a favourable bias is robust. A "real edge" finding under it would not have been —
which is worth remembering if a future study here comes back positive.

---

## Reproducing

```bash
source .venv/bin/activate
python scripts/signal_study.py --step 5 --years 12 \
    --cache /tmp/panel_12y.pkl --json-out /tmp/lanes.json
python -m pytest tests/test_signal_lanes.py tests/test_signal_study.py -q
```

The panel is ~183 symbols x 12 years from yfinance, chunked and slept for rate limits;
`--cache` makes re-runs free. Full run is ~30 seconds once cached.

`tools/signal_lanes.py` **copies** the three lane predicates rather than importing them,
deliberately — a study must be frozen against the predicate it measured, or next month's
results describe code that no longer exists. `tests/test_signal_lanes.py` pins the copied
boundaries. If `universe_scanner.py`'s thresholds move, those tests are where the drift
should surface; re-run this study before trusting these numbers against a changed scanner.
