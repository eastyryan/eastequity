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


# ---------------------------------------------------------------------------
# The four technical layers added in 32fbac9 — measurement wiring.
#
# These signals ship to the brain through CLAUDE.md, which makes claims about
# them ("above it ... tends to act as support", "a name can be a daily uptrend
# while its weekly structure rolls over"). supplier_pullbacks and
# earnings_reaction both read as well-established and both measured as noise, so
# the claims get measured before they get weighted. What is pinned here is the
# WIRING — that the study computes these at the right bar, from the right data,
# with no lookahead. A measurement harness that quietly saw the future would
# manufacture exactly the confident verdict this repo is trying not to ship.
# ---------------------------------------------------------------------------

def _sector_panel(n=420):
    """Three names with distinguishable trends, plus SPY."""
    spy = _frame([100.0 + 0.05 * k for k in range(n)])
    up = _frame([50.0 + 0.10 * k for k in range(n)])
    flat = _frame([60.0 + 0.001 * k for k in range(n)])
    down = _frame([90.0 - 0.03 * k for k in range(n)])
    return {"SPY": spy, "AAA": up, "BBB": flat, "CCC": down}


def test_new_signals_are_populated_on_every_record():
    """A predicate over a key the signal never writes is silently always-False.

    That failure mode reports `n=0` for a lane and reads as "no observations"
    rather than "broken wiring" — the same absent-vs-inconclusive ambiguity the
    context pack fights. Assert the keys exist and at least one is decided.
    """
    panel = _sector_panel()
    tickers = ["AAA", "BBB", "CCC"]
    sectors = {"alpha": ["AAA", "BBB"], "beta": ["CCC"]}
    extras = SS.build_extras(panel, tickers, sectors)
    signal_fn, cross_fn = SS.make_lane_signal(top_n=2)
    rows = SS.run(tickers, 2, 20, signal_fn, cross_fn=cross_fn,
                  panel=panel, extras=extras, min_bars=300)
    assert rows
    for key in ("avwap_status", "avwap_above", "wk_structure", "wk_above_30w",
                "daily_above_200dma", "rs_spy_1m", "rs_sector_1m", "rs_pseudo_1m"):
        assert key in rows[0], f"{key} missing — its lane would report n=0 forever"
    assert any(r["avwap_status"] == "ok" for r in rows), "anchored VWAP never resolved"
    assert any(r["wk_above_30w"] is not None for r in rows), "weekly block never resolved"


def test_weekly_ohlc_bars_are_real_weekly_highs_and_lows():
    """weekly_structure needs highs/lows for higher-highs/higher-lows; without
    them it silently skips weekly_structure, which is the half CLAUDE.md makes a
    claim about. A week's high must be the max across its daily bars."""
    from tools import signal_lanes as SL
    idx = list(pd.bdate_range(start="2015-01-05", periods=15))   # 3 full weeks
    highs = [float(x) for x in range(1, 16)]
    lows = [float(-x) for x in range(1, 16)]
    closes = [float(x) for x in range(1, 16)]
    wc, wh, wl, wp = SL.weekly_ohlc(idx, closes, highs, lows, closes)
    assert wh[0] == 5.0 and wl[0] == -5.0, "week 1 high/low are not the week's extremes"
    assert wh[1] == 10.0 and wl[1] == -10.0
    assert wc[0] == 5.0, "week close must be the last daily close of the week"
    assert wp[0] == 4, "position must be the daily index of the week's last bar"
    assert len(wh) == len(wl) == len(wc) == len(wp)


def test_weekly_bars_used_are_only_the_CLOSED_ones():
    """Using the in-progress week's final close mid-week is lookahead.

    weekly_ohlc emits a week only once the NEXT week has started, so the last
    daily position it reports must always be strictly before the final bar.
    """
    from tools import signal_lanes as SL
    idx = list(pd.bdate_range(start="2015-01-05", periods=13))   # 2 full + partial
    v = [float(x) for x in range(1, 14)]
    _wc, _wh, _wl, wp = SL.weekly_ohlc(idx, v, v, v, v)
    assert wp[-1] < len(idx) - 1, (
        "the in-progress week was emitted — its close is not knowable at bar i")


def test_new_signals_cannot_see_the_future():
    """THE test, applied to the new layers specifically.

    Corrupt every bar after a cut point; every record dated before it must be
    byte-identical. anchored_vwap_block and weekly_structure both treat the LAST
    element of what they receive as "now", so a slicing mistake in signal_fn
    would let them read forward and this is what catches it.
    """
    panel = _sector_panel()
    tickers = ["AAA", "BBB", "CCC"]
    sectors = {"alpha": ["AAA", "BBB"], "beta": ["CCC"]}
    signal_fn, cross_fn = SS.make_lane_signal(top_n=2)

    clean = SS.run(tickers, 2, 20, signal_fn,
                   cross_fn=cross_fn, panel=panel,
                   extras=SS.build_extras(panel, tickers, sectors), min_bars=300)

    corrupted = {k: v.copy() for k, v in panel.items()}
    cut = 380
    for t, df in corrupted.items():
        for col in ("Open", "High", "Low", "Close"):
            df.iloc[cut:, df.columns.get_loc(col)] *= 3.0
        df.iloc[cut:, df.columns.get_loc("Volume")] *= 50.0

    dirty = SS.run(tickers, 2, 20, signal_fn,
                   cross_fn=cross_fn, panel=corrupted,
                   extras=SS.build_extras(corrupted, tickers, sectors), min_bars=300)

    watched = ("avwap_status", "avwap_above", "avwap_pct", "avwap_anchor",
               "wk_structure", "wk_above_30w", "wk_30w_rising", "wk_rsi")
    # Records are dated, not indexed. A signal at a bar before the cut reads only
    # clean bars, so those are the ones that must be identical.
    cut_date = panel["SPY"].index[cut]
    by_key = {(r["ticker"], r["date"]): r for r in dirty}
    compared = 0
    for r in clean:
        d = by_key.get((r["ticker"], r["date"]))
        if d is None or r["date"] >= cut_date:
            continue
        for key in watched:
            assert r[key] == d[key], (
                f"{key} changed for {r['ticker']} on {r['date']} ({r[key]!r} -> "
                f"{d[key]!r}) when only bars after index {cut} were corrupted — "
                f"the signal is reading forward")
        compared += 1
    assert compared > 5, "too few pre-cut records compared to trust this"


def test_sector_and_pseudo_sector_arms_are_scored_on_the_same_pool():
    """The RS comparison is only fair if both arms rank identical observations.

    A name with no sector RS (thin sector) excluded from one arm but not the
    other would make the difference composition rather than signal — the exact
    confound that made supplier_pullbacks look like an edge.
    """
    panel = _sector_panel()
    tickers = ["AAA", "BBB", "CCC"]
    sectors = {"alpha": ["AAA", "BBB", "CCC"]}
    extras = SS.build_extras(panel, tickers, sectors)
    signal_fn, cross_fn = SS.make_lane_signal(top_n=2)
    rows = SS.run(tickers, 2, 20, signal_fn, cross_fn=cross_fn,
                  panel=panel, extras=extras, min_bars=300)
    for r in rows:
        has_real = r["rs_sector_1m"] is not None
        has_pseudo = r["rs_pseudo_1m"] is not None
        assert has_real == has_pseudo, (
            f"{r['ticker']} has real={has_real} pseudo={has_pseudo} — the arms "
            f"are scoring different observations")


def test_pseudo_sectors_preserve_group_sizes_and_carry_no_real_membership():
    """The ablation must differ from the real grouping ONLY in its information.

    Same number of groups and same sizes, so any scoring difference is the
    sector labels rather than a different cross-sectional construction.
    """
    sectors = {"a": ["AAA", "BBB", "CCC"], "b": ["DDD", "EEE"], "c": ["FFF"]}
    pseudo = SS.pseudo_sectors(sectors)
    assert set(pseudo) == {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}
    sizes = sorted(
        len([t for t, g in pseudo.items() if g == grp]) for grp in set(pseudo.values()))
    assert sizes == sorted(len(v) for v in sectors.values()), (
        "the ablation changed the shape of the grouping, not just its content")
    assert SS.pseudo_sectors(sectors) == pseudo, "ablation must be deterministic"


def test_indicators_by_ticker_is_not_claimed_to_be_measured():
    """indicators_by_ticker is a PROJECTION, not a signal.

    technicals.build_indicator_map compacts already-computed scanner fields; it
    has no predicate and no forward return of its own. Listing it as a measured
    lane would put a verdict next to a name that was never tested — which is how
    supplier_pullbacks kept its authority for so long.
    """
    labels = [lbl for lbl, _ in SS.NEW_SIGNAL_GROUPS]
    assert not any("indicators_by_ticker" in lbl for lbl in labels)
    assert any("aVWAP" in lbl for lbl in labels)
    assert any("wk " in lbl or "weekly" in lbl for lbl in labels)
    assert any("RS-vs-SECTOR" in lbl for lbl in labels)
    assert any("ablation" in lbl.lower() for lbl in labels), (
        "the sector arm shipped without its ablation control")


# ---------------------------------------------------------------------------
# Round 3 — entry-timing and volume indicators.
#
# These are IMPORTED from universe_scanner / volume_analysis rather than
# reimplemented, so the study scores the code that actually ships. What needs
# pinning is the wiring around them: the warm-up window, the tri-state breakout
# key, the sentinel in updown_vol_ratio, and no-lookahead.
# ---------------------------------------------------------------------------

def test_the_study_imports_the_shipped_indicator_functions_not_copies():
    """A second implementation would measure a lane the scanner does not have.

    This is not hypothetical: signal_lanes carried a stale copy of the
    volume-surge expression after the scanner fixed it, so the top_setups verdict
    was scoring a name's own missing volume history.
    """
    import inspect
    from tools import universe_scanner as US
    from tools import volume_analysis as VA
    src = inspect.getsource(SS.make_lane_signal)
    assert "from tools.universe_scanner import" in src
    assert "from tools.volume_analysis import analyze_volume" in src
    # and the imports resolve to the real things
    assert callable(US._rsi14) and callable(US._macd_state) and callable(US._adx14)
    assert callable(VA.analyze_volume)


def test_warmup_window_does_not_change_the_indicator_readings():
    """THE claim behind WARMUP=400, asserted rather than asserted-in-a-comment.

    _rsi14 / _macd_state / _adx14 all seed from the HEAD of whatever list they
    receive and smooth forward, so a short slice gives a different answer than
    full history. 400 bars is chosen as far past convergence — if that is wrong,
    every indicator arm in the study is measuring a warm-up artifact.
    """
    from tools.universe_scanner import _adx14, _macd_state, _rsi14
    n = 1200
    closes = [50.0 + 10 * ((k % 97) / 97.0) + 0.02 * k for k in range(n)]
    highs = [c * 1.012 for c in closes]
    lows = [c * 0.988 for c in closes]
    i = n - 1

    for warm in (400, 800):
        sl = slice(max(0, i - warm), i + 1)
        assert abs(_rsi14(closes[sl]) - _rsi14(closes[:i + 1])) < 0.05, (
            f"RSI at warm-up {warm} differs from full history")
        assert (_macd_state(closes[sl])["state"]
                == _macd_state(closes[:i + 1])["state"]), (
            f"MACD state at warm-up {warm} differs from full history")
        a_w, _, _ = _adx14(highs[sl], lows[sl], closes[sl])
        a_f, _, _ = _adx14(highs[:i + 1], lows[:i + 1], closes[:i + 1])
        assert abs(a_w - a_f) < 0.5, f"ADX at warm-up {warm} differs from full history"


def test_breakout_key_is_tri_state_and_predicates_respect_it():
    """volume_analysis only SETS breakout_volume_confirmed when a breakout
    printed, so absent != unconfirmed. A predicate using truthiness instead of
    `is False` would score every non-breakout bar as a failed breakout."""
    groups = dict(SS.INDICATOR_GROUPS)
    conf = groups["breakout CONFIRMED (>=1.5x vol)"]
    unconf = groups["breakout UNCONFIRMED (<1.5x vol)"]
    assert conf({"vol_breakout_confirmed": True}) is True
    assert conf({"vol_breakout_confirmed": False}) is False
    assert unconf({"vol_breakout_confirmed": False}) is True
    assert unconf({"vol_breakout_confirmed": True}) is False
    # THE BUG THIS PREVENTS: no breakout at all must match NEITHER arm.
    assert conf({}) is False and unconf({}) is False
    assert conf({"vol_breakout_confirmed": None}) is False
    assert unconf({"vol_breakout_confirmed": None}) is False


def test_updown_volume_sentinel_is_excluded_from_its_predicate():
    """updown_volume_ratio returns 99.0 for "no down-volume in the window".

    That is an ABSENCE marker, not a ratio of 99. Counting it as an extreme
    reading would let missing data render as the strongest possible signal —
    the same rule the volume-surge divide-by-one bug broke.
    """
    pred = dict(SS.INDICATOR_GROUPS)["updown_vol_ratio > 1 (excl. sentinel)"]
    assert pred({"updown_vol_ratio_25d": 1.4}) is True
    assert pred({"updown_vol_ratio_25d": 0.8}) is False
    assert pred({"updown_vol_ratio_25d": 99.0}) is False, (
        "the no-down-volume sentinel was scored as a real ratio")
    assert pred({"updown_vol_ratio_25d": None}) is False


def test_short_history_trend_flags_resolve_to_none_not_false():
    """`above_50dma` must be None under 50 bars, never False.

    The scanner's inline pandas version computes `last > close.rolling(50).mean()`,
    which on short history is `last > NaN` -> False. That reports "not above its
    50-DMA" for a name that HAS no 50-DMA, and absent data must not resolve to a
    substantive reading.

    Tested on the primitive, not through run(): the study's min_bars floor is 260,
    so this branch is unreachable from a real study and a test routed through
    run() would pass without ever evaluating it — a guard that cannot fire.
    """
    from tools import signal_lanes as SL
    short = [100.0 + k for k in range(30)]
    assert SL._sma(short, 50) is None, "the pure primitive should decline, not NaN"

    # The mapping the study applies on top of it.
    sma50, sma200, last = SL._sma(short, 50), SL._sma(short, 200), short[-1]
    above_50 = (last > sma50) if sma50 is not None else None
    trend = (sma50 > sma200) if (sma50 is not None and sma200 is not None) else None
    assert above_50 is None and trend is None

    # Contrast: the pandas expression the scanner still uses inline.
    import math
    assert (last > float("nan")) is False, (
        "this is the shape being avoided — NaN comparison yields a confident False")
    assert math.isnan(float("nan"))


def test_trend_flags_are_decided_on_real_history():
    """The other half: with enough bars the flags must actually resolve, or the
    None-safety above would be silently suppressing every reading."""
    panel = _sector_panel()
    extras = SS.build_extras(panel, ["AAA"], {"alpha": ["AAA"]})
    signal_fn, cross_fn = SS.make_lane_signal(top_n=2)
    rows = SS.run(["AAA"], 2, 20, signal_fn, cross_fn=cross_fn,
                  panel=panel, extras=extras, min_bars=300)
    assert rows
    assert all(r["above_50dma"] in (True, False) for r in rows)
    assert any(r["trend_up_50_over_200"] is True for r in rows)


def test_indicator_signals_cannot_see_the_future():
    """No-lookahead, applied to the imported indicator functions.

    Every one of them treats the last element it receives as "now", so the
    slice ending at ctx.i is the entire guard. Volume is corrupted too, since
    the whole volume block keys off relative volume.
    """
    panel = _sector_panel()
    tickers = ["AAA", "BBB", "CCC"]
    sectors = {"alpha": ["AAA", "BBB"], "beta": ["CCC"]}
    signal_fn, cross_fn = SS.make_lane_signal(top_n=2)

    clean = SS.run(tickers, 2, 20, signal_fn, cross_fn=cross_fn, panel=panel,
                   extras=SS.build_extras(panel, tickers, sectors), min_bars=300)

    corrupted = {k: v.copy() for k, v in panel.items()}
    cut = 380
    for df in corrupted.values():
        for col in ("Open", "High", "Low", "Close"):
            df.iloc[cut:, df.columns.get_loc(col)] *= 3.0
        df.iloc[cut:, df.columns.get_loc("Volume")] *= 50.0

    dirty = SS.run(tickers, 2, 20, signal_fn, cross_fn=cross_fn, panel=corrupted,
                   extras=SS.build_extras(corrupted, tickers, sectors), min_bars=300)

    watched = ("rsi_14", "macd_state", "adx_14", "di_bull", "above_50dma",
               "trend_up_50_over_200", "vol_read", "vol_pocket_pivot",
               "vol_no_supply", "vol_breakout_confirmed", "obv_divergence",
               "cmf_20", "updown_vol_ratio_25d", "gap_filled", "gap_retained_pct")
    cut_date = panel["SPY"].index[cut]
    by_key = {(r["ticker"], r["date"]): r for r in dirty}
    compared = 0
    for r in clean:
        d = by_key.get((r["ticker"], r["date"]))
        if d is None or r["date"] >= cut_date:
            continue
        for key in watched:
            assert r[key] == d[key], (
                f"{key} changed for {r['ticker']} on {r['date']} ({r[key]!r} -> "
                f"{d[key]!r}) when only bars after index {cut} were corrupted — "
                f"the indicator is reading forward")
        compared += 1
    assert compared > 5, "too few pre-cut records compared to trust this"


def test_volume_surge_no_longer_divides_by_one_share():
    """REGRESSION: signal_lanes kept the divide-by-one expression the scanner
    fixed, and swing_setup_score (+0.5 for vol_surge > 1.3) is what the study's
    top_setups lane RANKS on — so an absent volume baseline promoted a name up
    the ranking on the strength of its own missing data.

    Absence must resolve to None, never to a substantive reading.
    """
    from tools import signal_lanes as SL
    n = 300
    closes = [100.0 + 0.05 * k for k in range(n)]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    # The exact bug shape: no baseline history, then real volume prints. The old
    # max(..., 1) floor made the denominator ONE SHARE, so this returned ~5,000,000.
    vols = [0.0] * (n - 5) + [5_000_000.0] * 5
    row = SL.lane_features(closes, highs, lows, vols, n - 1)
    assert row is not None
    assert row["volume_surge_5d_vs_3m"] is None, (
        f"an absent volume baseline produced a surge of "
        f"{row['volume_surge_5d_vs_3m']:,.0f} — the max(...,1) floor is back, and "
        f"vol_surge > 1.3 scores +0.5 on swing_setup_score, so a data hole is "
        f"again promoting a name up the ranking on the strength of its absence")

    vols_ok = [1_000_000.0] * (n - 5) + [2_000_000.0] * 5
    row_ok = SL.lane_features(closes, highs, lows, vols_ok, n - 1)
    assert 1.9 < row_ok["volume_surge_5d_vs_3m"] < 2.1, (
        "the fix broke the ordinary case")
