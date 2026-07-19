"""Pin the extracted lane predicates to the scanner comprehensions they copy.

tools/signal_lanes.py copies rather than imports the three inline lane filters at
universe_scanner.py:817-866, deliberately (a study must be frozen against the predicate
it measured). The cost of copying is drift, and these tests are the price paid for it:
they assert the copies' BOUNDARIES exactly, so if the scanner's thresholds move and
someone re-runs the study, the mismatch surfaces here instead of silently producing
results that describe code no longer in production.

No network. Everything is pure lists in / dicts out, which is the reason the extraction
was worth doing at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import signal_lanes as SL  # noqa: E402


# --- contrarian_reversal (universe_scanner.py:817-826) -----------------------
def _contra_row(**kw):
    row = {"is_full_52w_window": True, "pct_from_52w_high": -25.0,
           "momentum_1m_pct": 5.0}
    row.update(kw)
    return row


def test_contrarian_requires_full_52w_window():
    """A young name's '52-week' high is over a shorter window; the scanner refuses to
    call that a 20% drawdown and so must this."""
    assert SL.contrarian_reversal_pred(_contra_row()) is True
    assert SL.contrarian_reversal_pred(_contra_row(is_full_52w_window=False)) is False


def test_contrarian_drawdown_boundary_is_inclusive_at_minus_20():
    assert SL.contrarian_reversal_pred(_contra_row(pct_from_52w_high=-20.0)) is True
    assert SL.contrarian_reversal_pred(_contra_row(pct_from_52w_high=-19.9)) is False


def test_contrarian_momentum_boundary_is_strict_above_2():
    assert SL.contrarian_reversal_pred(_contra_row(momentum_1m_pct=2.0)) is False
    assert SL.contrarian_reversal_pred(_contra_row(momentum_1m_pct=2.1)) is True


def test_contrarian_none_fields_never_pass():
    assert SL.contrarian_reversal_pred(_contra_row(pct_from_52w_high=None)) is False
    assert SL.contrarian_reversal_pred(_contra_row(momentum_1m_pct=None)) is False
    assert SL.contrarian_reversal_pred(None) is False


# --- deep_value_200w (universe_scanner.py:835-846) ---------------------------
def _dv_row(**kw):
    row = {"liquid": True, "is_full_200w_window": True, "pct_vs_200w_ma": -10.0}
    row.update(kw)
    return row


def test_deep_value_tolerance_is_plus_two_percent_inclusive():
    """'At' the 200-week MA means within +2% ABOVE it (AT_OR_BELOW_TOLERANCE_PCT)."""
    assert SL.deep_value_200w_pred(_dv_row(pct_vs_200w_ma=2.0)) is True
    assert SL.deep_value_200w_pred(_dv_row(pct_vs_200w_ma=2.1)) is False
    assert SL.deep_value_200w_pred(_dv_row(pct_vs_200w_ma=-40.0)) is True


def test_deep_value_requires_liquidity_and_a_real_200w_window():
    assert SL.deep_value_200w_pred(_dv_row(liquid=False)) is False
    assert SL.deep_value_200w_pred(_dv_row(is_full_200w_window=False)) is False


def test_deep_value_screen_gate_fails_open_on_missing_data():
    """_screen_quality_ok passes on missing data — which is EVERY historical bar, so the
    backtested lane is the lane without its quality filter. Pinned so that hole stays
    visible rather than being quietly assumed closed."""
    assert SL.deep_value_200w_pred(_dv_row(), screen_row=None) is True
    assert SL.deep_value_200w_pred(_dv_row(), screen_row={}) is True
    wreck = {"fwd_revenue_growth_pct": -5.0, "revision_direction": "down"}
    assert SL.deep_value_200w_pred(_dv_row(), screen_row=wreck) is False
    ok = {"fwd_revenue_growth_pct": -5.0, "revision_direction": "up"}
    assert SL.deep_value_200w_pred(_dv_row(), screen_row=ok) is True


# --- supplier_pullback (universe_scanner.py:854-866) -------------------------
def _sp_row(**kw):
    row = {"ai_exposure": "ai_supplier", "liquid": True, "is_full_52w_window": True,
           "above_200dma": True, "pct_from_52w_high": -15.0}
    row.update(kw)
    return row


def test_supplier_pullback_band_boundaries_are_inclusive():
    assert SL.supplier_pullback_pred(_sp_row(pct_from_52w_high=-8.0)) is True
    assert SL.supplier_pullback_pred(_sp_row(pct_from_52w_high=-30.0)) is True
    assert SL.supplier_pullback_pred(_sp_row(pct_from_52w_high=-7.9)) is False
    assert SL.supplier_pullback_pred(_sp_row(pct_from_52w_high=-30.1)) is False


def test_supplier_pullback_requires_the_ai_supplier_label_and_the_200dma():
    assert SL.supplier_pullback_pred(_sp_row(ai_exposure="ai_beneficiary")) is False
    assert SL.supplier_pullback_pred(_sp_row(ai_exposure=None)) is False
    assert SL.supplier_pullback_pred(_sp_row(above_200dma=False)) is False
    assert SL.supplier_pullback_pred(_sp_row(above_200dma=None)) is False


def test_ablation_differs_from_shipping_lane_only_by_the_label():
    """The ablation exists to separate the SETUP from the 2026 ai_supplier LABEL, which
    is applied to historical bars and is therefore hindsight. It must be identical in
    every other respect or it is not a controlled comparison."""
    for pfh in (-7.9, -8.0, -15.0, -30.0, -30.1):
        row = _sp_row(pct_from_52w_high=pfh)
        assert SL.supplier_pullback_pred(row) == SL.supplier_pullback_pred_no_label(row)
    unlabelled = _sp_row(ai_exposure="ai_neutral")
    assert SL.supplier_pullback_pred(unlabelled) is False
    assert SL.supplier_pullback_pred_no_label(unlabelled) is True
    assert SL.supplier_pullback_pred_no_label(_sp_row(liquid=False)) is False


# --- lane_features -----------------------------------------------------------
def _ramp(n, start=100.0, step=0.5):
    return [start + step * k for k in range(n)]


def _bars(n=300, start=100.0, step=0.5):
    closes = _ramp(n, start, step)
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    vols = [5_000_000.0] * n
    return closes, highs, lows, vols


def test_lane_features_is_strictly_backward_looking():
    """The whole harness rests on this: a signal at bar i must not see bar i+1. Corrupt
    everything after i and the features must be byte-identical."""
    closes, highs, lows, vols = _bars(300)
    i = 250
    a = SL.lane_features(closes, highs, lows, vols, i, ticker="T")
    for k in range(i + 1, 300):
        closes[k], highs[k], lows[k], vols[k] = 1e6, 1e6, 1e6, 1e12
    b = SL.lane_features(closes, highs, lows, vols, i, ticker="T")
    assert a == b


def test_lane_features_returns_none_below_scanner_min_bars():
    closes, highs, lows, vols = _bars(300)
    assert SL.lane_features(closes, highs, lows, vols, 10) is None
    assert SL.lane_features(closes, highs, lows, vols, SL.MIN_BARS + 5) is not None


def test_lane_features_flags_liquidity_off_median_dollar_volume():
    closes, highs, lows, vols = _bars(300)
    assert SL.lane_features(closes, highs, lows, vols, 250)["liquid"] is True
    thin = [1.0] * 300          # 100 * 1 share = $100/day
    assert SL.lane_features(closes, highs, lows, thin, 250)["liquid"] is False


def test_lane_features_marks_partial_52w_window_and_missing_200dma():
    """Under 252 bars the '52-week' claim is false; under 200 bars above_200dma is None
    (NOT False) — the scanner is explicit that null must never be read as a downtrend."""
    closes, highs, lows, vols = _bars(300)
    short = SL.lane_features(closes[:150], highs[:150], lows[:150], vols[:150], 149)
    assert short["is_full_52w_window"] is False
    assert short["above_200dma"] is None
    full = SL.lane_features(closes, highs, lows, vols, 260)
    assert full["is_full_52w_window"] is True
    assert full["above_200dma"] is True


def test_lane_features_relative_strength_is_name_minus_spy():
    closes, highs, lows, vols = _bars(300)          # steady ramp up
    flat_spy = [100.0] * 300
    row = SL.lane_features(closes, highs, lows, vols, 250,
                           spy_closes=flat_spy, spy_i=250)
    assert row["rel_strength_3m_pct"] == row["momentum_3m_pct"]
    row_nospy = SL.lane_features(closes, highs, lows, vols, 250)
    assert row_nospy["rel_strength_3m_pct"] is None


def test_weekly_closes_returns_week_end_bars_with_daily_positions():
    """The 200-week MA is derived from the daily panel here, not a separate weekly
    download. Positions are returned so a caller can take only CLOSED weeks — using the
    in-progress week's final close mid-week would be lookahead."""
    import datetime as dt
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=k) for k in range(28)]
    closes = [float(k) for k in range(28)]
    wc, wp = SL.weekly_closes(dates, closes)
    assert len(wc) == len(wp) >= 3
    assert all(wc[k] == closes[wp[k]] for k in range(len(wc)))
    assert wp == sorted(wp)
    # each recorded position is the last bar of its ISO week
    for p in wp:
        assert dates[p].isocalendar()[:2] != dates[p + 1].isocalendar()[:2]


# --- the same-day lane cascade (universe_scanner.py:815-869) -----------------
def _row(t, score, **kw):
    row = {"ticker": t, "swing_setup_score": score, "is_full_52w_window": True,
           "pct_from_52w_high": None, "momentum_1m_pct": 0.0, "liquid": True,
           "is_full_200w_window": True, "pct_vs_200w_ma": None,
           "above_200dma": True, "ai_exposure": None, "rel_strength_3m_pct": 0.0}
    row.update(kw)
    return row


def test_cascade_excludes_top_setups_from_every_lane():
    """The lanes only ever see what the top-N by swing_setup_score did NOT take. A lane
    measured without that exclusion is scored on a different, easier population."""
    rows = [_row("HOT", 9.0, pct_from_52w_high=-25.0, momentum_1m_pct=5.0),
            _row("COLD", 1.0, pct_from_52w_high=-25.0, momentum_1m_pct=5.0)]
    lanes = SL.select_lanes(rows, top_n=1)
    assert [r["ticker"] for r in lanes["top_setups"]] == ["HOT"]
    assert [r["ticker"] for r in lanes["contrarian_reversal"]] == ["COLD"]


def test_cascade_is_ordered_contrarian_then_value_then_supplier():
    """A name qualifying for two lanes lands in the EARLIER one only — the scanner's
    `lane_taken` accumulation. Ordering is load-bearing, not cosmetic."""
    both = _row("BOTH", 1.0, pct_from_52w_high=-25.0, momentum_1m_pct=5.0,
                pct_vs_200w_ma=-10.0)
    lanes = SL.select_lanes([both], top_n=0)
    assert [r["ticker"] for r in lanes["contrarian_reversal"]] == ["BOTH"]
    assert lanes["deep_value_200w"] == []


def test_cascade_truncates_each_lane_to_five():
    rows = [_row("T%d" % k, 1.0, pct_from_52w_high=-25.0, momentum_1m_pct=float(k))
            for k in range(3, 15)]
    lanes = SL.select_lanes(rows, top_n=0)
    picked = lanes["contrarian_reversal"]
    assert len(picked) == 5
    # ranked by 1-month momentum, descending
    assert [r["momentum_1m_pct"] for r in picked] == [14.0, 13.0, 12.0, 11.0, 10.0]


def test_cascade_supplier_ranks_by_relative_strength_desc():
    rows = [_row("A", 1.0, ai_exposure="ai_supplier", pct_from_52w_high=-15.0,
                 rel_strength_3m_pct=1.0),
            _row("B", 1.0, ai_exposure="ai_supplier", pct_from_52w_high=-15.0,
                 rel_strength_3m_pct=9.0),
            _row("C", 1.0, ai_exposure="ai_supplier", pct_from_52w_high=-15.0,
                 rel_strength_3m_pct=None)]
    picked = SL.select_lanes(rows, top_n=0)["supplier_pullback"]
    assert [r["ticker"] for r in picked] == ["B", "A", "C"]   # None sorts last


def test_deep_value_sort_demotes_ai_at_risk_names():
    """A business frontier AI can replicate is the value-TRAP case, so it ranks last no
    matter how far below the 200-week MA it sits."""
    rows = [_row("TRAP", 1.0, pct_vs_200w_ma=-40.0, ai_exposure="ai_at_risk"),
            _row("GOOD", 1.0, pct_vs_200w_ma=-5.0, ai_exposure="ai_neutral")]
    picked = SL.select_lanes(rows, top_n=0)["deep_value_200w"]
    assert [r["ticker"] for r in picked] == ["GOOD", "TRAP"]


# --- the earnings-reaction price leg ----------------------------------------
def _gap_bars(n, gap_at, gap_open, gap_close, after_closes):
    """Flat 100 baseline with ONE engineered gap, then quiet bars.

    Post-gap opens are pinned to the previous close on purpose: leaving them at the
    baseline manufactures a SECOND gap the next session, which _gap_events would report
    as `last` and silently invert the test.
    """
    closes = [100.0] * n
    opens = [100.0] * n
    closes[gap_at], opens[gap_at] = gap_close, gap_open
    for k, c in enumerate(after_closes, start=gap_at + 1):
        opens[k] = closes[k - 1]
        closes[k] = c
    for k in range(gap_at + 1 + len(after_closes), n):
        opens[k] = closes[k - 1]
        closes[k] = closes[k - 1]
    return opens, closes


def test_gap_hold_reaction_classifies_a_held_up_gap():
    opens, closes = _gap_bars(30, 25, 106.0, 107.0, [107.0] * 4)
    reaction, read = SL.gap_hold_reaction(opens, closes, 29)
    assert reaction == "gap_held"
    assert read.endswith("_revisions_unknown")    # revisions are unmeasurable, ever


def test_gap_hold_reaction_detects_a_faded_gap():
    """+7% open giving nearly all of it back by its own close is a faded reaction
    dressed up as a hold — retained_pct < 0.5 (the UNH 2026-07-17 case)."""
    opens, closes = _gap_bars(30, 25, 107.0, 100.5, [100.4] * 4)
    assert SL.gap_hold_reaction(opens, closes, 29)[0] == "gap_faded"


def test_gap_hold_reaction_detects_a_filled_gap():
    # drifts back through the pre-gap close of 100 without printing a second gap
    opens, closes = _gap_bars(30, 25, 106.0, 107.0, [105.0, 103.0, 101.0, 99.0])
    assert SL.gap_hold_reaction(opens, closes, 29)[0] == "gap_filled"


def test_gap_hold_reaction_detects_a_down_gap():
    opens, closes = _gap_bars(30, 25, 94.0, 93.0, [93.0] * 4)
    assert SL.gap_hold_reaction(opens, closes, 29)[0] == "down_gap"


def test_gap_hold_reaction_is_backward_looking():
    opens, closes = _gap_bars(40, 25, 106.0, 107.0, [107.0] * 4)
    before = SL.gap_hold_reaction(opens, closes, 29)
    assert before[0] == "gap_held"
    for k in range(30, 40):                       # future collapse
        opens[k], closes[k] = 1.0, 1.0
    assert SL.gap_hold_reaction(opens, closes, 29) == before


def test_earnings_reaction_flag_needs_revisions_up():
    """The SHIPPING flag requires revision_direction == 'up', a point-in-time analyst
    field with no history. This pins WHY the lane cannot be backtested as it ships."""
    gap = {"last": {"sessions_ago": 2, "direction": "up", "filled": False,
                    "retained_pct": 0.9}}
    assert SL.earnings_reaction_read(3, gap, 5.0, "up") == (True, "gap_held_revisions_up")
    flag, read = SL.earnings_reaction_read(3, gap, 5.0, None)
    assert flag is False and read == "gap_held_revisions_unknown"
