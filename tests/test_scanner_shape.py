"""Shape + pure-function tests for the 2026-08-05 scan_universe decomposition
(tools/universe_scanner.py).

WHY THIS FILE EXISTS: scan_universe was ~800 inline lines; the extraction moved
each stage into a module-level function with an explicit signature. These tests
pin (a) the pure per-ticker computations that became independently testable
(_volume_surge, _in_uptrend, _swing_setup_score — the score is checked against
a verbatim copy of the PRE-extraction inline arithmetic, same term order, so
float accumulation must match exactly), (b) the fixture-testability of
_compute_ticker_row and _select_lanes without any network, and (c) the public
seams other modules import (scan_universe signature unchanged).

Pure Python + pandas only. No network, no state/ writes.

    python3 tests/test_scanner_shape.py
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.universe_scanner import (  # noqa: E402
    MIN_BARS,
    _compute_ticker_row,
    _in_uptrend,
    _select_lanes,
    _swing_setup_score,
    _volume_surge,
    _weekly_leaders,
    scan_universe,
)
import tools.universe_scanner as US  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# --------------------------------------------------------------------------- #
# _volume_surge — the divide-by-one guard as a pure function
# --------------------------------------------------------------------------- #
def test_volume_surge():
    print("_volume_surge (divide-by-one guard):")
    check("absent baseline -> None (never a fake surge)",
          _volume_surge(2_000_000.0, None) is None)
    check("zero baseline -> None", _volume_surge(2_000_000.0, 0.0) is None)
    check("negative baseline -> None", _volume_surge(2_000_000.0, -1.0) is None)
    nan = float("nan")
    check("NaN baseline -> None", _volume_surge(2_000_000.0, nan) is None)
    check("NaN recent -> None", _volume_surge(nan, 1_000_000.0) is None)
    check("real ratio computes", _volume_surge(3_000_000.0, 1_500_000.0) == 2.0)
    check("sub-1 ratio computes (volume drying up)",
          _volume_surge(500_000.0, 1_000_000.0) == 0.5)


# --------------------------------------------------------------------------- #
# _in_uptrend — 200-DMA gate with graceful fallbacks
# --------------------------------------------------------------------------- #
def test_in_uptrend():
    print("_in_uptrend (trend gate fallbacks):")
    check("true 200-DMA stack", _in_uptrend(110, 105, 102, 100) is True)
    check("200-DMA present and violated -> False (no fallback)",
          _in_uptrend(110, 105, 102, 106) is False)
    check("no 200-DMA: falls back to 100-DMA",
          _in_uptrend(110, 105, 100, None) is True)
    check("no 100/200: 50-DMA only", _in_uptrend(110, 105, None, None) is True)
    check("below 50-DMA fails everywhere",
          _in_uptrend(100, 105, None, None) is False)


# --------------------------------------------------------------------------- #
# _swing_setup_score — must reproduce the pre-extraction inline block exactly
# --------------------------------------------------------------------------- #
def _pre_extraction_score(in_uptrend, last, sma20, mom_1m, mom_3m,
                          pullback_20d, vol_surge, rel_1m):
    """VERBATIM copy of the inline block scan_universe carried before
    2026-08-05 (same expressions, same order). The extracted function must
    match it bit-for-bit — float accumulation is order-sensitive."""
    score = 0.0
    score += 2.0 if in_uptrend else 0.0
    score += 1.0 if last > sma20 else 0.0
    score += max(min(mom_3m * 5, 2.0), -1.0)
    score += max(min(mom_1m * 5, 1.0), -1.0)
    score += 1.0 if -0.10 <= pullback_20d <= -0.02 else 0.0
    score += 0.5 if (vol_surge is not None and vol_surge > 1.3) else 0.0
    if rel_1m is not None:
        score += max(min(rel_1m * 4, 0.4), -0.2)
    return score


def test_swing_setup_score():
    print("_swing_setup_score (vs pre-extraction inline arithmetic):")
    grid = []
    for in_up in (True, False):
        for mom_1m in (-0.31, -0.04, 0.0, 0.07, 0.55):
            for mom_3m in (-0.5, -0.02, 0.11, 0.9):
                for pullback in (-0.15, -0.07, -0.01, 0.0):
                    for vs in (None, 0.8, 1.31, 4.2):
                        for rel in (None, -0.3, 0.02, 0.6):
                            grid.append((in_up, 101.7, 100.2, mom_1m, mom_3m,
                                         pullback, vs, rel))
    mismatches = [
        args for args in grid
        if _swing_setup_score(*args) != _pre_extraction_score(*args)
    ]
    check(f"bit-identical over {len(grid)} combinations", not mismatches,
          f"first mismatch args={mismatches[:1]}")
    # Spot values a reviewer can verify by hand.
    check("full house: 2+1+2+1+1+0.5+0.4 = 7.9",
          _swing_setup_score(True, 110, 100, 0.5, 0.9, -0.05, 2.0, 0.5) == 7.9)
    check("vol_surge None adds nothing",
          _swing_setup_score(True, 110, 100, 0.5, 0.9, -0.05, None, 0.5) == 7.4)
    check("rel_1m None adds nothing (not the -0.2 floor)",
          _swing_setup_score(True, 110, 100, 0.5, 0.9, -0.05, None, None) == 7.0)
    check("momentum floors clamp at -1 each",
          _swing_setup_score(False, 90, 100, -0.9, -0.9, 0.0, None, None) == -2.0)


# --------------------------------------------------------------------------- #
# _compute_ticker_row — fixture-testable per-ticker stage (offline, pandas)
# --------------------------------------------------------------------------- #
def _frame(n):
    import pandas as pd
    idx = pd.bdate_range(end="2026-07-17", periods=n)
    closes = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes],
         "Low": [c * 0.99 for c in closes], "Close": closes,
         "Volume": [1_000_000.0] * n}, index=idx)


def test_compute_ticker_row():
    print("_compute_ticker_row (fixture frame, no network):")
    df = _frame(300)
    row = _compute_ticker_row(
        "AAA", df, sector="semis", spy_1m=0.01, spy_3m=0.03,
        screen_row={"eps_estimate_current_yr": 5.0},
        ai_info={"exposure": "ai_supplier", "reason": "sells the buildout"},
        w200_entry=(88.5, True, -12.3),
        weekly_block={"weekly_structure": "uptrend"})
    check("ticker/sector plumbed", row["ticker"] == "AAA" and row["sector"] == "semis")
    check("price_as_of is the last bar's session",
          row["price_as_of"] == "2026-07-17", str(row.get("price_as_of")))
    last = float(df["Close"].iloc[-1])
    check("last_close rounded from the frame", row["last_close"] == round(last, 2))
    check("fwd_pe_est = last / screen EPS estimate",
          row["fwd_pe_est"] == round(last / 5.0, 2), str(row["fwd_pe_est"]))
    check("w200_entry plumbed (wma_200w / pct / full-window)",
          row["wma_200w"] == 88.5 and row["pct_vs_200w_ma"] == -12.3
          and row["is_full_200w_window"] is True)
    check("ai exposure label plumbed", row["ai_exposure"] == "ai_supplier")
    check("weekly block passes through untouched",
          row["weekly"] == {"weekly_structure": "uptrend"})
    check("300 bars -> full 52w window and a real 200-DMA trend bool",
          row["is_full_52w_window"] is True and row["above_200dma"] is True)
    check("flat volume -> surge ~1, never a fake spike",
          row["volume_surge_5d_vs_3m"] is not None
          and 0.9 <= row["volume_surge_5d_vs_3m"] <= 1.1)
    check("price_basis disclosure survives",
          row["price_basis"] == "split_dividend_adjusted")
    # Sparse-context variant: every optional input absent.
    bare = _compute_ticker_row(
        "BBB", _frame(MIN_BARS), sector="unmapped", spy_1m=None, spy_3m=None,
        screen_row=None, ai_info=None, w200_entry=None, weekly_block=None)
    check("minimal history: long-window fields None, not fabricated",
          bare["momentum_6m_pct"] is None and bare["above_200dma"] is None
          and bare["wma_200w"] is None and bare["is_full_200w_window"] is False)
    check("no screen row -> no fwd_pe_est", bare["fwd_pe_est"] is None)
    check("no SPY read -> rel strength None",
          bare["rel_strength_1m_pct"] is None)
    # The raising contract: caller counts a bad frame under no_data.
    try:
        import pandas as pd
        _compute_ticker_row("CCC", pd.DataFrame({"Close": [1.0] * 100}),
                            sector="x", spy_1m=None, spy_3m=None,
                            screen_row=None, ai_info=None,
                            w200_entry=None, weekly_block=None)
        raised = False
    except Exception:
        raised = True
    check("bad frame raises (caller counts no_data)", raised)


# --------------------------------------------------------------------------- #
# _select_lanes — lane cascade over plain dict rows (pure)
# --------------------------------------------------------------------------- #
def _lane_row(ticker, **kw):
    base = {
        "ticker": ticker, "swing_setup_score": 1.0, "momentum_1m_pct": 0.0,
        "is_full_52w_window": True, "pct_from_52w_high": -5.0,
        "liquid": True, "is_full_200w_window": True, "pct_vs_200w_ma": 50.0,
        "ai_exposure": None, "above_200dma": True, "rel_strength_3m_pct": 0.0,
    }
    base.update(kw)
    return base


def test_select_lanes():
    print("_select_lanes (cascade + in-place stamps):")
    rows = [
        _lane_row("T1", swing_setup_score=9.0),
        _lane_row("T2", swing_setup_score=8.0,
                  pct_from_52w_high=-30.0, momentum_1m_pct=9.0),  # in top_n
        _lane_row("CON", swing_setup_score=3.0,
                  pct_from_52w_high=-25.0, momentum_1m_pct=5.0),
        _lane_row("DV", swing_setup_score=2.0, pct_vs_200w_ma=-10.0),
        _lane_row("TRAP", swing_setup_score=1.5, pct_vs_200w_ma=-20.0),
        _lane_row("SUP", swing_setup_score=1.0, ai_exposure="ai_supplier",
                  pct_from_52w_high=-15.0),
    ]
    screen = {"TRAP": {"fwd_revenue_growth_pct": -5.0,
                       "revision_direction": "down"}}
    contrarian, deep_value, suppliers = _select_lanes(rows, 2, screen)
    check("contrarian picks CON only (T2 is already in top_n)",
          [r["ticker"] for r in contrarian] == ["CON"],
          str([r["ticker"] for r in contrarian]))
    check("contrarian stamped in place",
          rows[2].get("lane") == "contrarian_reversal")
    check("deep value picks DV; shrinking+cut TRAP is screened out",
          [r["ticker"] for r in deep_value] == ["DV"],
          str([r["ticker"] for r in deep_value]))
    check("supplier lane picks SUP with the lane_validation disclosure",
          [r["ticker"] for r in suppliers] == ["SUP"]
          and "ATTENTION ROUTER" in suppliers[0].get("lane_validation", ""))
    lanes = {r["ticker"] for r in contrarian + deep_value + suppliers}
    check("lanes are mutually exclusive with top_n and each other",
          lanes == {"CON", "DV", "SUP"}, str(lanes))


def test_weekly_leaders():
    print("_weekly_leaders (slim per-sector top 3):")
    by_sector = {"semis": [
        _lane_row("A", swing_setup_score=5.0, last_close=10.0),
        _lane_row("B", swing_setup_score=9.0, last_close=11.0),
        _lane_row("C", swing_setup_score=7.0, last_close=12.0),
        _lane_row("D", swing_setup_score=1.0, last_close=13.0),
    ]}
    leaders = _weekly_leaders(by_sector)
    got = [r["ticker"] for r in leaders["semis"]]
    check("top 3 by score, ordered", got == ["B", "C", "A"], str(got))
    check("slim field set only (no lane internals)",
          set(leaders["semis"][0]) == {"ticker", "last_close", "momentum_1m_pct",
                                       "rel_strength_1m_pct", "pct_from_52w_high",
                                       "swing_setup_score", "liquid"},
          str(set(leaders["semis"][0])))


# --------------------------------------------------------------------------- #
# public seams — signature and stage inventory survive the extraction
# --------------------------------------------------------------------------- #
def test_seams():
    print("public seams:")
    params = inspect.signature(scan_universe).parameters
    check("scan_universe signature unchanged",
          list(params) == ["top_n", "tickers", "enrich", "weekly"]
          and params["top_n"].default == 15
          and params["tickers"].default is None
          and params["enrich"].default is True
          and params["weekly"].default is False)
    for fn in ("_resolve_scan_tickers", "_scan_now_et", "_benchmark_context",
               "_weekly_context", "_load_screen_rows", "_compute_ticker_row",
               "_select_lanes", "_enrich_focus_rows", "_attach_sector_comps",
               "_sector_breadth_context", "_assemble_scan_output",
               "_apply_scan_status", "_weekly_leaders"):
        check(f"stage {fn} is module-level and callable",
              callable(getattr(US, fn, None)))


if __name__ == "__main__":
    test_volume_surge()
    test_in_uptrend()
    test_swing_setup_score()
    test_compute_ticker_row()
    test_select_lanes()
    test_weekly_leaders()
    test_seams()
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURE(S): {FAILURES}'}")
    sys.exit(1 if FAILURES else 0)
