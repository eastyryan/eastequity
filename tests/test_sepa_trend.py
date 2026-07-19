"""Offline tests for tools/sepa_trend.py — Minervini trend template + VCP.

Everything here is pure: synthetic bar series in, dicts out. No network, no file
IO, no yfinance. The point of the suite is to pin the two behaviours that matter
most for a live book: (a) the 8-condition gate is HARD (one violation = False),
and (b) "not enough history" is never silently reported as a pass.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.sepa_trend as st


# --------------------------------------------------------------------------- #
# Helpers — synthetic series
# --------------------------------------------------------------------------- #
def uptrend(n=300, start=50.0, step=0.30):
    """Steady advance: every MA stacks correctly and the 200-DMA slopes up."""
    return [start + step * i for i in range(n)]


def downtrend(n=300, start=140.0, step=0.30):
    return [start - step * i for i in range(n)]


def bars_from_closes(closes, spread=0.01):
    """(highs, lows) bracketing each close by `spread`."""
    return ([c * (1 + spread) for c in closes], [c * (1 - spread) for c in closes])


def vcp_series(base=100.0, pre=60):
    """A textbook VCP: three contractions of decreasing depth (20% -> 12% -> 6%)
    on decreasing volume, ending tight just under the pivot."""
    closes, vols = [], []
    # Prior advance into the consolidation.
    for i in range(pre):
        closes.append(base * 0.6 + (base * 0.4) * i / pre)
        vols.append(1_000_000)

    pivot = base
    for depth, vol, legs in ((0.20, 900_000, 10), (0.12, 600_000, 8), (0.06, 300_000, 6)):
        low = pivot * (1 - depth)
        for j in range(legs):                      # down into the low
            closes.append(pivot - (pivot - low) * (j + 1) / legs)
            vols.append(vol)
        for j in range(legs):                      # back up toward the pivot
            closes.append(low + (pivot - low) * (j + 1) / legs * 0.99)
            vols.append(vol)
    return closes, vols


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_sma_at_requires_full_window():
    closes = list(range(1, 101))          # 100 bars
    assert st._sma_at(closes, 50) == pytest.approx(sum(range(51, 101)) / 50)
    assert st._sma_at(closes, 200) is None            # never a partial average
    assert st._sma_at(closes, 50, offset=60) is None  # window runs off the start


def test_sma_at_offset_looks_back():
    closes = list(range(1, 301))
    now = st._sma_at(closes, 200)
    prior = st._sma_at(closes, 200, offset=21)
    assert now > prior                                 # rising series -> rising MA


def test_trailing_low_flags_partial_window():
    lows = [10.0] * 100
    val, full = st._trailing_low(lows, 252)
    assert val == 10.0 and full is False               # 100 bars is not 52 weeks
    val, full = st._trailing_low([5.0] * 300, 252)
    assert val == 5.0 and full is True


def test_percentile_rank():
    pop = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert st.percentile_rank(10, pop) == 100.0
    assert st.percentile_rank(5, pop) == 50.0
    assert st.percentile_rank(None, pop) is None
    assert st.percentile_rank(5, []) is None


def test_strictly_decreasing():
    assert st._strictly_decreasing([20.0, 12.0, 6.0]) is True
    assert st._strictly_decreasing([6.0, 12.0, 20.0]) is False
    assert st._strictly_decreasing([10.0]) is False    # one point is not a trend


# --------------------------------------------------------------------------- #
# Trend template
# --------------------------------------------------------------------------- #
def test_clean_uptrend_passes_all_eight():
    closes = uptrend()
    highs, lows = bars_from_closes(closes)
    out = st.trend_template(closes, highs, lows, rs_percentile=92.0)
    assert out["template_pass"] is True
    assert out["status"] == "ok"
    assert out["passed"] == 8 and out["failed"] == 0 and out["unknown"] == 0


def test_downtrend_fails_hard():
    closes = downtrend()
    highs, lows = bars_from_closes(closes)
    out = st.trend_template(closes, highs, lows, rs_percentile=10.0)
    assert out["template_pass"] is False
    assert "price_above_150_200" in out["failed_conditions"]
    assert "ma200_trending_up" in out["failed_conditions"]


def test_single_violation_fails_the_gate():
    """A name 40% off its high has stage-2 MA structure but is NOT buyable —
    the gate is AND, not a score."""
    closes = uptrend(n=300)
    closes = closes + [closes[-1] * 0.60] * 5         # sharp break from the high
    highs, lows = bars_from_closes(closes)
    out = st.trend_template(closes, highs, lows, rs_percentile=90.0)
    assert out["template_pass"] is False
    assert "near_52w_high" in out["failed_conditions"]


def test_rs_below_threshold_fails():
    closes = uptrend()
    highs, lows = bars_from_closes(closes)
    out = st.trend_template(closes, highs, lows, rs_percentile=55.0)
    assert out["template_pass"] is False
    assert out["failed_conditions"] == ["rs_percentile_ok"]


def test_rs_exactly_at_threshold_fails():
    """'> 70th percentile' is strict — 70.0 is not a pass."""
    closes = uptrend()
    highs, lows = bars_from_closes(closes)
    out = st.trend_template(closes, highs, lows, rs_percentile=70.0)
    assert out["conditions"]["rs_percentile_ok"] is False


def test_short_history_is_unverified_not_passed():
    """The regression that matters: a 90-bar name must never look buyable."""
    closes = uptrend(n=90)
    highs, lows = bars_from_closes(closes)
    out = st.trend_template(closes, highs, lows, rs_percentile=95.0)
    assert out["template_pass"] is None                # NOT True, NOT False
    assert out["status"] == "insufficient_history"
    assert out["conditions"]["price_above_150_200"] is None
    assert out["unknown"] > 0


def test_pending_rs_when_percentile_missing():
    closes = uptrend()
    highs, lows = bars_from_closes(closes)
    out = st.trend_template(closes, highs, lows, rs_percentile=None)
    assert out["template_pass"] is None
    assert out["status"] == "pending_rs"
    assert out["passed"] == 7


def test_empty_series_degrades():
    out = st.trend_template([], [], [])
    assert out["template_pass"] is None
    assert out["status"] == "insufficient_history"


# --------------------------------------------------------------------------- #
# VCP
# --------------------------------------------------------------------------- #
def test_swing_points_alternate():
    closes, _ = vcp_series()
    highs, lows = bars_from_closes(closes)
    pts = st.swing_points(highs, lows)
    kinds = [k for _, _, k in pts]
    assert len(pts) >= 4
    assert all(a != b for a, b in zip(kinds, kinds[1:]))   # strictly alternating


def test_swing_points_ignore_noise():
    """Sub-threshold wiggle is not a swing."""
    closes = [100.0 + (0.2 if i % 2 else -0.2) for i in range(60)]
    highs, lows = bars_from_closes(closes, spread=0.001)
    assert st.swing_points(highs, lows, threshold_pct=3.0) == []


def test_textbook_vcp_detected():
    closes, vols = vcp_series()
    highs, lows = bars_from_closes(closes)
    out = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    assert out["status"] == "ok"
    assert out["vcp_detected"] is True
    assert out["contraction_count"] >= st.VCP_MIN_CONTRACTIONS
    assert out["depths_contracting"] is True
    assert out["depths_pct"] == sorted(out["depths_pct"], reverse=True)
    assert out["pivot"] is not None


def test_expanding_pullbacks_are_not_vcp():
    """Depths widening = a name breaking down, the opposite of a contraction."""
    closes, vols = [], []
    pivot = 100.0
    for i in range(60):
        closes.append(60 + 40 * i / 60)
        vols.append(1_000_000)
    for depth, vol in ((0.06, 300_000), (0.12, 600_000), (0.20, 900_000)):
        low = pivot * (1 - depth)
        for j in range(8):
            closes.append(pivot - (pivot - low) * (j + 1) / 8)
            vols.append(vol)
        for j in range(8):
            closes.append(low + (pivot - low) * (j + 1) / 8 * 0.99)
            vols.append(vol)
    highs, lows = bars_from_closes(closes)
    out = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    assert out["vcp_detected"] is False
    assert out["depths_contracting"] is False


def test_tightening_run_counts_only_the_trailing_base():
    # Noisy prefix, then a clean 3-leg tightening into the pivot.
    assert st._tightening_run([9.0, 30.0, 5.0, 20.0, 12.0, 6.0]) == 3
    assert st._tightening_run([20.0, 12.0, 6.0]) == 3
    assert st._tightening_run([6.0, 12.0, 20.0]) == 0      # expanding: no base
    assert st._tightening_run([10.0]) == 0
    # Capped at VCP_MAX_BASE_LEGS even when more legs keep tightening.
    assert st._tightening_run([40.0, 30.0, 20.0, 12.0, 6.0]) == st.VCP_MAX_BASE_LEGS


def test_vcp_survives_a_noisy_prefix():
    """The regression that motivated the trailing-run design: a real leader prints
    many swings before it bases. Requiring EVERY leg to contract meant live names
    (NVDA showed 11 legs) could never qualify."""
    closes, vols = [], []
    # Choppy advance with erratic swing depths.
    for i, depth in enumerate((0.10, 0.25, 0.08, 0.30)):
        top = 70.0 + i * 3
        for j in range(6):
            closes.append(top - top * depth * (j + 1) / 6)
            vols.append(1_500_000)
        for j in range(6):
            closes.append(top * (1 - depth) + top * depth * (j + 1) / 6)
            vols.append(1_400_000)
    # ...then a textbook 3-leg base.
    tail_closes, tail_vols = vcp_series(base=100.0, pre=10)
    closes += tail_closes
    vols += tail_vols

    highs, lows = bars_from_closes(closes)
    out = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    assert out["contraction_count"] > out["base_leg_count"]   # prefix legs excluded
    assert out["base_leg_count"] >= st.VCP_MIN_CONTRACTIONS
    assert out["vcp_detected"] is True
    assert out["base_depths_pct"] == sorted(out["base_depths_pct"], reverse=True)


def test_pivot_comes_from_the_base_not_an_older_high():
    """An older, higher swing is overhead supply — using it as the pivot would put
    the buy zone far above the actual breakout level."""
    closes, vols = [], []
    for j in range(14):                       # early spike to ~150, then a deep break
        closes.append(150.0 - 60.0 * j / 14)
        vols.append(2_000_000)
    tail_closes, tail_vols = vcp_series(base=100.0, pre=20)
    closes += tail_closes
    vols += tail_vols

    highs, lows = bars_from_closes(closes)
    out = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    assert out["pivot"] is not None
    assert out["pivot"] < 130.0               # the base high, not the 150 spike


def test_partial_bar_does_not_manufacture_a_dryup():
    """The dangerous direction: a still-forming bar carries a fraction of a day's
    volume, so including it fakes the volume dry-up that CONFIRMS a VCP. The flag
    must exclude it — otherwise every intraday slot invents bullish confirmation."""
    closes, _ = vcp_series()
    # Volume already tapering mildly into the pivot — ~0.87 ratio, short of the 0.80
    # dry-up bar. This is the realistic case: a partial bar does not conjure a dry-up
    # out of flat volume (one bar diluted across a 5-bar window only reaches ~0.82),
    # but it readily tips a name that was ALREADY close, which is most bases.
    vols = [1_000_000] * (len(closes) - 5) + [860_000] * 5
    closes = closes + [max(closes) * 1.01]
    vols = vols + [100_000]                   # 9:35am: a tenth of a session
    highs, lows = bars_from_closes(closes)

    naive = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    guarded = st.detect_vcp(highs, lows, closes, vols, window=len(closes),
                            last_bar_partial=True)

    assert naive["volume_dryup"] is True       # the bug: invented confirmation
    assert guarded["volume_dryup"] is False    # the fix
    assert guarded["last_bar_partial"] is True
    assert guarded["volume_bars_used"] == naive["volume_bars_used"] - 1


def test_partial_bar_always_biases_dryup_toward_firing():
    """Even when it does not flip the flag, a partial bar drags the ratio toward the
    bullish read — never away. Pins the DIRECTION of the distortion, which is the
    part that holds regardless of where the threshold sits."""
    closes, _ = vcp_series()
    vols = [1_000_000] * len(closes)
    closes = closes + [max(closes) * 1.01]
    vols = vols + [80_000]
    highs, lows = bars_from_closes(closes)

    naive = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    guarded = st.detect_vcp(highs, lows, closes, vols, window=len(closes),
                            last_bar_partial=True)
    assert naive["dryup_ratio"] < guarded["dryup_ratio"]
    assert guarded["dryup_ratio"] == pytest.approx(1.0, abs=0.02)  # truth: no dry-up


def test_partial_bar_suppresses_breakout_volume_claim():
    """A running intraday total against a full-day average is a clock reading, not
    a volume multiple — report None rather than a confidently wrong number."""
    closes, vols = vcp_series()
    closes = closes + [max(closes) * 1.02]
    vols = vols + [120_000]
    highs, lows = bars_from_closes(closes)

    out = st.detect_vcp(highs, lows, closes, vols, window=len(closes),
                        last_bar_partial=True)
    assert out["breakout_volume_mult"] is None
    assert out["breakout_volume_ok"] is None
    assert out["in_buy_zone"] is True          # PRICE is still live and usable


def test_partial_flag_threads_through_evaluate_sepa():
    closes, vols = vcp_series()
    closes = closes + [max(closes) * 1.01]
    vols = vols + [50_000]
    highs, lows = bars_from_closes(closes)
    out = st.evaluate_sepa(closes, highs, lows, vols, rs_percentile=95.0,
                           last_bar_partial=True)
    assert out["vcp"]["last_bar_partial"] is True
    assert out["vcp"]["breakout_volume_ok"] is None


def test_vcp_short_history_degrades():
    closes = [100.0] * 20
    highs, lows = bars_from_closes(closes)
    out = st.detect_vcp(highs, lows, closes, [1] * 20)
    assert out["status"] == "insufficient_history"
    assert out["vcp_detected"] is False


def test_buy_zone_and_breakout_volume():
    closes, vols = vcp_series()
    pivot = max(closes)
    closes = closes + [pivot * 1.02]                  # breakout into the buy zone
    vols = vols + [3_000_000]                         # on heavy volume
    highs, lows = bars_from_closes(closes)
    out = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    assert out["in_buy_zone"] is True
    assert out["breakout_volume_ok"] is True
    # The pivot is the consolidation's intraday HIGH (~1% above the max close), so a
    # close 2% over the max close sits ~1% over the pivot. Pinning this stops anyone
    # "fixing" the pivot into a closing price, which would misplace every buy zone.
    assert out["pivot"] > max(closes[:-1])
    assert out["pct_to_pivot"] == pytest.approx(1.0, abs=0.4)


def test_extended_past_buy_zone():
    closes, vols = vcp_series()
    pivot = max(closes)
    closes = closes + [pivot * 1.25]                  # +25% past the pivot: chasing
    vols = vols + [2_000_000]
    highs, lows = bars_from_closes(closes)
    out = st.detect_vcp(highs, lows, closes, vols, window=len(closes))
    assert out["in_buy_zone"] is False


# --------------------------------------------------------------------------- #
# evaluate_sepa + attach_rs_percentile
# --------------------------------------------------------------------------- #
def test_evaluate_sepa_never_raises_on_garbage():
    for bad in ([], [None, None], [float("nan")]):
        out = st.evaluate_sepa(bad, bad, bad, bad)
        assert "trend_template" in out and "vcp" in out
        assert out["trend_template"]["template_pass"] is not True


def test_headline_reads():
    closes = uptrend()
    highs, lows = bars_from_closes(closes)
    out = st.evaluate_sepa(closes, highs, lows, [1_000_000] * len(closes),
                           rs_percentile=95.0)
    assert out["read"] == "stage2_structure"

    down = downtrend()
    dh, dl = bars_from_closes(down)
    out = st.evaluate_sepa(down, dh, dl, [1_000_000] * len(down), rs_percentile=5.0)
    assert out["read"] == "not_stage2"


def test_vcp_is_withheld_from_the_brain_facing_block():
    """The VCP layer measured NEGATIVE excess at every horizon and under every risk
    rule tested. It must not reach a trading decision, however rigorous it looks."""
    closes, vols = vcp_series()
    highs, lows = bars_from_closes(closes)
    full = st.evaluate_sepa(closes, highs, lows, vols, rs_percentile=95.0)
    compact = st.compact_sepa(full)

    assert "vcp" in full                      # still computed for research
    assert "vcp" not in compact               # but never shipped to the brain
    assert "validation" in compact
    assert "do NOT size" in compact["validation"]
    # The headline must not encode the VCP either.
    assert "vcp" not in (compact["read"] or "")


def test_attach_rs_percentile_fills_condition_eight():
    closes = uptrend()
    highs, lows = bars_from_closes(closes)
    def fresh():
        """A separate SEPA block per row — attach_rs_percentile mutates in place, so
        a shared dict would make these assertions meaningless."""
        return st.evaluate_sepa(closes, highs, lows, [1_000_000] * len(closes))

    # Population is [40, 0, 1, ... 8]: STRONG ranks 100th, LAGGARD (0.0) ranks 10th.
    rows = [{"ticker": "STRONG", "rel_strength_3m_pct": 40.0, "sepa": fresh()}]
    rows += [{"ticker": f"W{i}", "rel_strength_3m_pct": float(i), "sepa": fresh()}
             for i in range(1, 9)]
    rows += [{"ticker": "LAGGARD", "rel_strength_3m_pct": 0.0, "sepa": fresh()}]

    st.attach_rs_percentile(rows)

    top = rows[0]["sepa"]["trend_template"]
    assert top["rs_percentile"] == 100.0
    # The percentile is only interpretable next to the population it ranked against.
    assert top["rs_population_n"] == 10
    assert top["conditions"]["rs_percentile_ok"] is True
    assert top["template_pass"] is True
    assert top["status"] == "ok"

    bottom = rows[-1]["sepa"]["trend_template"]
    assert bottom["rs_percentile"] == 10.0
    assert bottom["conditions"]["rs_percentile_ok"] is False
    assert bottom["template_pass"] is False           # identical chart, weak RS -> gated out


def test_attach_rs_percentile_tolerates_missing_blocks():
    rows = [{"ticker": "A", "rel_strength_3m_pct": 5.0},          # no sepa block
            {"ticker": "B", "rel_strength_3m_pct": 9.0, "sepa": {}},
            {"ticker": "C", "rel_strength_3m_pct": 1.0, "sepa": {"trend_template": None}}]
    assert st.attach_rs_percentile(rows) is rows                  # no raise

    assert st.attach_rs_percentile([]) == []
    assert st.attach_rs_percentile([{"ticker": "X"}])             # no population


def test_short_history_stays_unverified_after_rs_pass():
    """A short-history name must still be unverified once RS is known — filling
    condition 8 must not paper over an unknown 200-DMA."""
    closes = uptrend(n=90)
    highs, lows = bars_from_closes(closes)
    rows = [{"ticker": "IPO", "rel_strength_3m_pct": 50.0,
             "sepa": st.evaluate_sepa(closes, highs, lows, [1_000_000] * 90)},
            {"ticker": "OTHER", "rel_strength_3m_pct": 1.0,
             "sepa": st.evaluate_sepa(closes, highs, lows, [1_000_000] * 90)}]
    st.attach_rs_percentile(rows)
    tt = rows[0]["sepa"]["trend_template"]
    assert tt["conditions"]["rs_percentile_ok"] is True
    assert tt["template_pass"] is None                            # still unverified
    assert rows[0]["sepa"]["read"] == "unverified_short_history"
