"""Offline tests for tools.correlation (pure Python, no network).

    .venv/bin/python tests/test_correlation.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.correlation import (  # noqa: E402
    _daily_returns, _corr, _beta, _book_returns, analyze_portfolio_risk,
)

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def approx(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def approx_list(xs, ys, tol=1e-9):
    return len(xs) == len(ys) and all(approx(a, b, tol) for a, b in zip(xs, ys))


# Deterministic wiggly "benchmark" return series (30 points, nonzero variance).
BENCH = [0.011, -0.008, 0.004, 0.017, -0.012, 0.006, -0.003, 0.009, -0.015, 0.021,
         0.002, -0.006, 0.013, -0.009, 0.005, 0.016, -0.011, 0.003, 0.008, -0.004,
         0.019, -0.013, 0.007, 0.001, -0.007, 0.012, -0.002, 0.010, -0.016, 0.014]


def test_daily_returns():
    print("daily returns:")
    r = _daily_returns([100.0, 110.0, 121.0])
    check("simple day-over-day returns", approx_list(r, [0.1, 0.1]), str(r))
    r = _daily_returns([100.0, None, "bad", 110.0])
    check("bad points skipped, chain preserved", approx_list(r, [0.1]), str(r))
    check("single close -> no returns", _daily_returns([100.0]) == [])
    check("empty/None -> no returns", _daily_returns(None) == [])


def test_corr():
    print("pearson correlation:")
    check("perfectly correlated pair ~ 1.0", approx(_corr(BENCH, BENCH), 1.0))
    check("anti-correlated pair ~ -1.0",
          approx(_corr(BENCH, [-x for x in BENCH]), -1.0))
    check("scaling does not change corr",
          approx(_corr(BENCH, [3.0 * x for x in BENCH]), 1.0))
    check("short series (9 points) -> None", _corr(BENCH[:9], BENCH[:9]) is None)
    check("exactly 10 points -> computed", approx(_corr(BENCH[:10], BENCH[:10]), 1.0))
    check("zero-variance series -> None", _corr([0.01] * 20, BENCH[:20]) is None)
    check("zero-variance other side -> None", _corr(BENCH[:20], [0.0] * 20) is None)


def test_beta():
    print("beta:")
    b = _beta([2.0 * x for x in BENCH], BENCH)
    check("2x-levered clone beta ~ 2.0", approx(b, 2.0), str(b))
    check("benchmark against itself beta ~ 1.0", approx(_beta(BENCH, BENCH), 1.0))
    check("inverse series beta ~ -1.0",
          approx(_beta([-x for x in BENCH], BENCH), -1.0))
    check("short series -> None", _beta(BENCH[:9], BENCH[:9]) is None)
    check("zero-variance benchmark -> None", _beta(BENCH, [0.005] * 30) is None)


def test_book_returns():
    print("book returns:")
    book = _book_returns({"A": [0.04] * 12, "B": [0.0] * 12},
                         {"A": 750.0, "B": 250.0})
    check("market-value weighting (75/25)", approx_list(book, [0.03] * 12), str(book[:3]))
    # alignment on min length from the END: A's two junk leading points must drop
    book = _book_returns({"A": [9.9, 9.9] + [0.04] * 10, "B": [0.0] * 10},
                         {"A": 500.0, "B": 500.0})
    check("series aligned from the end",
          len(book) == 10 and approx_list(book, [0.02] * 10), str(book[:3]))
    book = _book_returns({"A": [0.04] * 10, "B": [0.5] * 10}, {"A": 100.0})
    check("unweighted ticker excluded", approx_list(book, [0.04] * 10), str(book[:3]))
    check("no usable tickers -> []", _book_returns({}, {}) == [])


def test_shared_left_tail_book():
    print("shared-left-tail book (twin holdings):")
    twin = [1.2 * x for x in BENCH]
    out = analyze_portfolio_risk(
        {"DELL": twin, "HPE": twin, "KGC": [-x for x in BENCH]},
        {"DELL": 1020.0, "HPE": 814.0},
        BENCH, candidates=["kgc", "DELL"], benchmark="spy")
    check("status ok", out["status"] == "ok")
    check("benchmark upper-cased", out["benchmark"] == "SPY")
    check("window_days from benchmark series", out["window_days"] == 30)
    check("pairwise key sorted A|B", set(out["pairwise"]) == {"DELL|HPE"},
          str(out["pairwise"]))
    check("twin pair corr ~ 1.0", out["pairwise"]["DELL|HPE"] == 1.0)
    check("avg pairwise corr 1.0", out["avg_pairwise_holding_corr"] == 1.0)
    check("shared_left_tail true when all pairs > 0.7",
          out["shared_left_tail"] is True)
    check("note is a plain sentence",
          isinstance(out["note"], str) and len(out["note"]) > 10)
    check("portfolio beta of 1.2x book ~ 1.2", out["portfolio_beta"] == 1.2,
          str(out["portfolio_beta"]))
    check("holding beta + corr_to_benchmark",
          out["holdings"]["DELL"]["beta"] == 1.2
          and out["holdings"]["DELL"]["corr_to_benchmark"] == 1.0)
    kgc = out["candidates"].get("KGC")
    check("candidate ticker upper-cased", kgc is not None, str(out["candidates"].keys()))
    check("inverse candidate corr_to_book ~ -1.0", kgc["corr_to_book"] == -1.0)
    check("inverse candidate beta ~ -1.0", kgc["beta"] == -1.0)
    check("inverse candidate flagged diversifier", kgc["diversifier"] is True)
    # candidate already held: book must EXCLUDE it (HPE-only book = same twin series)
    dell = out["candidates"]["DELL"]
    check("held candidate corr_to_book excludes itself",
          dell["corr_to_book"] == 1.0 and dell["diversifier"] is False)


def test_not_shared_left_tail():
    print("anti-correlated book:")
    out = analyze_portfolio_risk(
        {"DELL": [1.2 * x for x in BENCH], "KGC": [-x for x in BENCH]},
        {"DELL": 1000.0, "KGC": 1000.0}, BENCH)
    check("anti-correlated pair ~ -1.0", out["pairwise"]["DELL|KGC"] == -1.0)
    check("shared_left_tail false", out["shared_left_tail"] is False)
    # three holdings, one below-threshold pair kills the flag
    out3 = analyze_portfolio_risk(
        {"A": BENCH, "B": BENCH, "C": [-x for x in BENCH]},
        {"A": 1.0, "B": 1.0, "C": 1.0}, BENCH)
    check("all three pairs reported", len(out3["pairwise"]) == 3)
    check("one uncorrelated pair -> not shared", out3["shared_left_tail"] is False)


def test_unknown_corr_blocks_shared_flag():
    print("unknown pair correlation:")
    out = analyze_portfolio_risk(
        {"A": BENCH, "B": BENCH, "C": [0.001] * 30},  # C: zero variance -> corr None
        {"A": 1.0, "B": 1.0, "C": 1.0}, BENCH)
    check("zero-variance pair reported as None",
          out["pairwise"]["A|C"] is None and out["pairwise"]["B|C"] is None)
    check("None pair -> shared_left_tail false", out["shared_left_tail"] is False)
    check("avg ignores None pairs", out["avg_pairwise_holding_corr"] == 1.0)


def test_single_and_empty_book():
    print("single / empty book:")
    out = analyze_portfolio_risk({"DELL": BENCH}, {"DELL": 1000.0}, BENCH)
    check("single holding: pairwise empty", out["pairwise"] == {})
    check("single holding: shared_left_tail false",
          out["shared_left_tail"] is False)
    check("single holding: beta still computed",
          out["holdings"]["DELL"]["beta"] == 1.0)
    out = analyze_portfolio_risk({}, {}, BENCH)
    check("empty book: still status ok", out["status"] == "ok")
    check("empty book: no holdings/candidates",
          out["holdings"] == {} and out["candidates"] == {})


def test_short_history_holding():
    print("short-history guards in assembled output:")
    out = analyze_portfolio_risk(
        {"DELL": BENCH, "NEW": BENCH[:5]}, {"DELL": 1000.0, "NEW": 500.0}, BENCH)
    check("short-history pair -> None corr", out["pairwise"]["DELL|NEW"] is None)
    check("short-history holding -> None beta",
          out["holdings"]["NEW"]["beta"] is None)


if __name__ == "__main__":
    for fn in (test_daily_returns, test_corr, test_beta, test_book_returns,
               test_shared_left_tail_book, test_not_shared_left_tail,
               test_unknown_corr_blocks_shared_flag, test_single_and_empty_book,
               test_short_history_holding):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All correlation checks passed.")
