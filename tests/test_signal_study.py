"""Tests for the generalized signal harness.

The harness produces the numbers a lane lives or dies by, so its own correctness is the
load-bearing thing. Three properties matter more than the rest and each has a test that
would fail loudly if broken:

  1. NO LOOKAHEAD — a signal at bar i cannot see bar i+1. Every backtest that has ever
     lied did so here.
  2. THE RISK RULES ARE THE BOOK'S — 2xATR initial stop, 3xATR chandelier, ratchet-only.
     sepa_study.py's entire rejection of SEPA rests on this scoring being right.
  3. THE CROSS-SECTIONAL PASS ACTUALLY RANKS — three of the four lanes measured are
     top-N selections; if cross_fn silently no-ops, every lane reports its raw predicate
     while claiming to report what ships.

No network: every test builds its own panel.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import signal_study as SS  # noqa: E402


def _frame(closes, opens=None, highs=None, lows=None, vols=None, start="2015-01-02"):
    n = len(closes)
    opens = opens if opens is not None else list(closes)
    highs = highs if highs is not None else [c * 1.01 for c in closes]
    lows = lows if lows is not None else [c * 0.99 for c in closes]
    vols = vols if vols is not None else [5_000_000.0] * n
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                         "Close": closes, "Volume": vols}, index=idx)


# --- excursions_atr: the book's real rules ----------------------------------
def test_excursions_atr_exits_at_the_two_atr_initial_stop():
    """Entry is bar i+1's OPEN; the stop sits 2xATR below it and fires on the first LOW
    that breaches. The recorded return is the STOP level, not the bar's close."""
    n = 40
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [99.0] * n
    opens = [100.0] * n
    lows[22] = 50.0                       # deep breach a bar after entry at i=20
    closes[22] = 55.0
    out = SS.excursions_atr(opens, highs, lows, closes, 20, 10)
    ret, mfe, mae, stopped = out
    atr = SS._atr(highs, lows, closes, 20)
    assert stopped is True
    assert ret == pytest.approx((100.0 - 2 * atr) / 100.0 - 1)
    assert ret > -0.5                     # exits AT the stop, not at the 50.0 low
    assert mae < 0


def test_excursions_atr_trail_ratchets_up_and_never_falls():
    """A winner that runs then gives back 3xATR must exit ABOVE its entry — that is the
    whole reason 'let it run' is mechanically safe in this system."""
    n = 40
    closes = [100.0] * 21 + [130.0] * 19
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    opens = [100.0] * n
    lows[35] = 100.5                      # pull back hard, but stay above entry
    closes[35] = 101.0
    ret, mfe, mae, stopped = SS.excursions_atr(opens, highs, lows, closes, 20, 18)
    assert stopped is True
    assert ret > 0, "chandelier trail must have ratcheted above the entry price"


def test_excursions_atr_holds_to_the_close_when_never_stopped():
    n = 40
    closes = [100.0 + k for k in range(n)]
    out = SS.excursions_atr([100.0] * n, [c * 1.001 for c in closes],
                            [c * 0.999 for c in closes], closes, 20, 10)
    ret, mfe, mae, stopped = out
    assert stopped is False
    assert ret == pytest.approx(closes[30] / 100.0 - 1)


def test_excursions_atr_returns_none_past_the_end_of_the_data():
    closes = [100.0] * 30
    assert SS.excursions_atr([100.0] * 30, [101.0] * 30, [99.0] * 30, closes, 25, 10) is None


def test_forward_return_enters_at_the_next_open_not_the_signal_close():
    """Entering at the signal bar's close is the classic way to manufacture a backtest:
    it buys at a price that was only knowable after the session ended."""
    closes = [100.0] * 30
    opens = [100.0] * 30
    opens[21] = 90.0                      # the first tradeable price after a bar-20 signal
    assert SS.forward_return(opens, closes, 20, 5) == pytest.approx(100.0 / 90.0 - 1)


def test_fixed_stop_excursions_truncate_at_minus_eight_percent():
    n = 40
    closes = [100.0] * n
    lows = [99.0] * n
    lows[24] = 80.0
    mfe, mae, ret = SS.excursions([100.0] * n, [101.0] * n, lows, closes, 20, 10)
    assert ret == pytest.approx(-0.08)    # exits at the stop, not the -20% low
    assert mae == pytest.approx(80.0 / 100.0 - 1)


# --- run(): the seam --------------------------------------------------------
def _panel(n=400):
    spy = _frame([100.0 + 0.05 * k for k in range(n)])
    a = _frame([50.0 + 0.10 * k for k in range(n)])
    b = _frame([80.0 - 0.02 * k for k in range(n)])
    return {"SPY": spy, "AAA": a, "BBB": b}


def test_run_passes_full_arrays_and_an_index_not_slices():
    """sepa_study.py carried closes[:i+1] on EVERY record, which is fatal market-wide.
    The signal must receive whole arrays and slice for itself."""
    seen = {}

    def sig(ctx):
        seen["len"] = len(ctx.closes)
        seen["i"] = ctx.i
        return {"v": ctx.closes[ctx.i]}

    rows = SS.run(["AAA"], 1, 50, sig, panel=_panel(), min_bars=300)
    assert rows
    assert seen["len"] > seen["i"] + 1, "signal got a slice, not the full array"
    assert "closes" not in rows[0] and "highs" not in rows[0]


def test_run_signal_cannot_see_the_future():
    """THE test. Corrupt every bar after a cut point and the signals dated before it
    must be identical. A harness that fails this produces confident nonsense."""
    def sig(ctx):
        return {"v": round(sum(ctx.closes[max(0, ctx.i - 20):ctx.i + 1]), 6)}

    panel = _panel()
    clean = SS.run(["AAA"], 1, 10, sig, panel=panel, min_bars=300)

    corrupted = {k: v.copy() for k, v in panel.items()}
    cut = 360
    for col in ("Open", "High", "Low", "Close"):
        corrupted["AAA"].iloc[cut:, corrupted["AAA"].columns.get_loc(col)] = 1.0
    dirty = SS.run(["AAA"], 1, 10, sig, panel=corrupted, min_bars=300)

    clean_v = {r["date"]: r["v"] for r in clean if r["date"] < panel["AAA"].index[cut - 21]}
    dirty_v = {r["date"]: r["v"] for r in dirty if r["date"] in clean_v}
    assert clean_v and dirty_v
    assert all(clean_v[d] == dirty_v[d] for d in dirty_v)


def test_run_records_signal_time_atr_for_every_observation():
    """The volatility-selection check is not opt-in. Whatever the signal returns, the
    harness records ATR at signal time, because that is the failure mode that already
    fooled this repo once."""
    rows = SS.run(None, 1, 25, lambda ctx: {"x": 1}, panel=_panel(), min_bars=300)
    assert rows
    assert all(r["sig_atr_pct"] is not None and r["sig_atr_pct"] > 0 for r in rows)


def test_run_skips_bars_where_the_signal_returns_none():
    rows = SS.run(None, 1, 25, lambda ctx: None, panel=_panel(), min_bars=300)
    assert rows == []


def test_run_cross_fn_sees_every_name_on_a_date_and_can_rank_them():
    """The cross-sectional pass is why rank-based conditions are measurable at all —
    RS percentiles, 'top 5 by momentum', the whole lane cascade."""
    def sig(ctx):
        return {"score": ctx.closes[ctx.i], "best": False}

    def cross(date, recs):
        assert len({r["ticker"] for r in recs}) == len(recs)
        top = max(recs, key=lambda r: r["score"])
        top["best"] = True

    rows = SS.run(None, 1, 25, sig, cross_fn=cross, panel=_panel(), min_bars=300)
    by_date = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    assert len(by_date) > 1
    for date, recs in by_date.items():
        assert sum(1 for r in recs if r["best"]) == 1


def test_run_restricts_to_the_requested_tickers():
    rows = SS.run(["AAA"], 1, 25, lambda ctx: {"x": 1}, panel=_panel(), min_bars=300)
    assert {r["ticker"] for r in rows} == {"AAA"}


def test_run_reports_an_error_without_a_benchmark():
    out = SS.run(["AAA"], 1, 25, lambda ctx: {"x": 1},
                 panel={"AAA": _frame([100.0] * 400)}, min_bars=300)
    assert isinstance(out, dict) and "error" in out


# --- summaries ---------------------------------------------------------------
def _rec(date, ticker, sel, fwd21, atr21, atr_pct=0.02):
    return {"date": date, "ticker": ticker, "sel": sel, "sig_atr_pct": atr_pct,
            "fwd": {21: fwd21}, "exc": {21: (0.1, -0.05, fwd21)},
            "atr": {21: (atr21, 0.1, -0.05, False)}}


def test_summarize_measures_excess_against_the_same_day_universe_mean():
    """Absolute returns off a survivors-only universe are meaningless; only the
    same-day cross-sectional difference is readable."""
    rows = [_rec("d1", "A", True, 0.10, 0.10), _rec("d1", "B", False, 0.00, 0.00)]
    out = SS.summarize(rows, "sel", lambda r: r["sel"], horizons=(21,))["horizons"][21]
    assert out["n"] == 1
    assert out["excess_mean_pct"] == pytest.approx(5.0)      # 10% vs a 5% pool mean
    assert out["absolute_mean_pct"] == pytest.approx(10.0)
    assert out["beat_universe_pct"] == 100.0


def test_summarize_atr_rules_reports_stop_rate_and_payoff():
    rows = [{"date": "d1", "ticker": "A", "sel": True, "sig_atr_pct": 0.02,
             "fwd": {21: 0.1}, "exc": {21: (0, 0, 0)},
             "atr": {21: (0.20, 0.25, -0.02, False)}},
            {"date": "d1", "ticker": "B", "sel": True, "sig_atr_pct": 0.02,
             "fwd": {21: -0.1}, "exc": {21: (0, 0, 0)},
             "atr": {21: (-0.10, 0.02, -0.12, True)}}]
    d = SS.summarize_atr_rules(rows, "sel", lambda r: r["sel"], (21,))["horizons"][21]
    assert d["n"] == 2
    assert d["win_rate_pct"] == 50.0
    assert d["stopped_pct"] == 50.0
    assert d["payoff"] == pytest.approx(2.0)                 # +20% win vs -10% loss
    assert d["excess_mean_pct"] == pytest.approx(0.0)        # both names ARE the pool


def test_atr_selection_check_detects_a_higher_volatility_selection():
    """The SEPA trap, made mechanical: a gate that picks names which move MORE rather
    than BETTER shows up here as a ratio above 1."""
    rows = [_rec("d1", "A", True, 0.0, 0.0, atr_pct=0.06),
            _rec("d1", "B", False, 0.0, 0.0, atr_pct=0.02)]
    d = SS.atr_selection_check(rows, "sel", lambda r: r["sel"])
    assert d["ratio"] == pytest.approx(3.0)
    assert d["atr_sel_pct"] == pytest.approx(6.0)


def test_atr_matched_excess_collapses_a_pure_volatility_tilt():
    """A lane whose only 'edge' is that it picks high-ATR names must very largely
    disappear against same-day, same-ATR-bucket peers. This is the control that turns
    the ATR observation into a verdict.

    It does NOT go to exactly zero, and the test asserts the honest thing rather than a
    flattering one: with quintiles, the bucket that STRADDLES the selection boundary
    holds both selected and unselected names, so a pure volatility tilt leaks a small
    positive residual through it. The control is therefore conservative — it understates
    how much of a lane's apparent edge is volatility — which is the safe direction for a
    harness whose job is to reject things.
    """
    rows = []
    for k in range(40):
        atr = 0.01 + 0.001 * k          # continuous, no ties
        ret = 4.0 * atr                 # return is PURELY a function of volatility
        rows.append({"date": "d1", "ticker": "T%d" % k, "sel": k >= 20,
                     "sig_atr_pct": atr,
                     "fwd": {21: ret}, "exc": {21: (0, 0, ret)},
                     "atr": {21: (ret, 0.1, -0.05, False)}})
    plain = SS.summarize_atr_rules(rows, "s", lambda r: r["sel"], (21,))
    matched = SS.summarize_atr_matched(rows, "s", lambda r: r["sel"], (21,))
    plain_x = plain["horizons"][21]["excess_mean_pct"]
    matched_x = matched["horizons"][21]["excess_mean_pct"]
    assert plain_x > 3.0                              # looks like a large edge
    assert 0 <= matched_x < 0.2 * plain_x             # ...and mostly is not one


def test_atr_matched_preserves_a_real_within_bucket_edge():
    """The control must not simply erase everything — a lane that outperforms peers of
    the SAME volatility still scores positive."""
    rows = []
    for k in range(40):
        sel = k % 2 == 0
        rows.append({"date": "d1", "ticker": "T%d" % k, "sel": sel,
                     "sig_atr_pct": 0.01 + 0.001 * k,
                     "fwd": {21: 0.0}, "exc": {21: (0, 0, 0)},
                     "atr": {21: (0.10 if sel else 0.0, 0.1, -0.05, False)}})
    d = SS.summarize_atr_matched(rows, "s", lambda r: r["sel"], (21,))["horizons"][21]
    assert d["excess_mean_pct"] == pytest.approx(5.0, abs=0.5)


# --- the lane signal wiring --------------------------------------------------
def test_make_lane_signal_emits_both_raw_predicate_and_shipped_selection():
    """Every lane is reported twice on purpose: the raw filter (larger n, the cleaner
    statistical question) and the top-5 the brain is actually shown."""
    signal_fn, cross_fn = SS.make_lane_signal(top_n=1)
    panel = _panel(400)
    extras = {t: {"ai_exposure": "ai_supplier"} for t in ("AAA", "BBB")}
    rows = SS.run(None, 1, 25, signal_fn, cross_fn=cross_fn, panel=panel,
                  extras=extras, min_bars=300)
    assert rows
    for key in ("pred_contrarian_reversal", "sel_contrarian_reversal",
                "pred_deep_value_200w", "sel_deep_value_200w",
                "pred_supplier_pullback", "sel_supplier_pullback",
                "pred_pullback_no_label", "sel_top_setups", "ear_reaction"):
        assert key in rows[0], key
    # the intermediate scan row is dropped after ranking: it is the memory blowup
    assert "row" not in rows[0]


def test_lane_selection_never_exceeds_five_names_per_date():
    signal_fn, cross_fn = SS.make_lane_signal(top_n=0)
    panel = _panel(400)
    extras = {t: {"ai_exposure": "ai_supplier"} for t in ("AAA", "BBB")}
    rows = SS.run(None, 1, 25, signal_fn, cross_fn=cross_fn, panel=panel,
                  extras=extras, min_bars=300)
    by_date = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    for recs in by_date.values():
        for lane in ("contrarian_reversal", "deep_value_200w", "supplier_pullback"):
            assert sum(1 for r in recs if r["sel_" + lane]) <= 5
