"""Tests for the P2 helper layer: throttle backoff, short interest, earnings
surprises, sector exposure. Pure Python, no network:

    python3 tests/test_p2_helpers.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.net import _retry_after_seconds
from tools.news_catalysts import _beat_streak, _surprise_pct
from tools.universe_scanner import _short_interest_from_info

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_retry_after():
    print("_retry_after_seconds:")
    check("numeric Retry-After honored", _retry_after_seconds("7", 0) == 7.0)
    check("Retry-After capped at 30", _retry_after_seconds("300", 0) == 30.0)
    check("missing header -> exponential", _retry_after_seconds(None, 0) == 2.0)
    check("attempt 2 -> 8s", _retry_after_seconds(None, 2) == 8.0)
    check("exponential capped", _retry_after_seconds(None, 10) == 30.0)
    check("garbage header -> exponential", _retry_after_seconds("soon", 1) == 4.0)


def test_short_interest():
    print("_short_interest_from_info:")
    si = _short_interest_from_info({
        "sharesShort": 11_000_000, "sharesShortPriorMonth": 10_000_000,
        "shortPercentOfFloat": 0.043, "shortRatio": 2.7})
    check("pct of float scaled", si["short_pct_float"] == 4.3, str(si))
    check("month-over-month rising (+10%)", si["month_over_month"] == "rising")
    check("days to cover mapped", si["days_to_cover"] == 2.7)
    si = _short_interest_from_info({"sharesShort": 10_100_000,
                                    "sharesShortPriorMonth": 10_000_000})
    check("+1% counts as flat", si["month_over_month"] == "flat", str(si))
    check("empty info -> None", _short_interest_from_info({}) is None)
    check("non-dict -> None", _short_interest_from_info(None) is None)


def test_earnings_surprises():
    print("_beat_streak / _surprise_pct:")
    check("streak counts from most recent", _beat_streak([-2.0, 3.1, 0.5, 6.0]) == 3)
    check("recent miss -> 0", _beat_streak([5.0, 5.0, -1.0]) == 0)
    check("None breaks streak", _beat_streak([4.0, None, 2.0]) == 1)
    check("empty -> 0", _beat_streak([]) == 0)
    check("fraction scaled to percent", _surprise_pct(0.053) == 5.3)
    check("already-percent passthrough", _surprise_pct(12.4) == 12.4)
    check("NaN -> None", _surprise_pct(float("nan")) is None)
    check("non-number -> None", _surprise_pct("n/a") is None)


def test_sector_exposure():
    print("orchestrator._sector_exposure:")
    import orchestrator
    smap = orchestrator._sector_map()
    ticker = next(iter(smap))
    portfolio = {"total_equity_usd": 10000,
                 "positions": [{"ticker": ticker, "market_value_usd": 2500},
                               {"ticker": "ZZZQ", "market_value_usd": 500}]}
    exp = orchestrator._sector_exposure(portfolio)
    top = exp[0]
    check("largest sector first", top["value_usd"] == 2500, str(exp))
    check("pct of equity computed", top["pct_of_equity"] == 25.0, str(exp))
    check("unknown ticker bucketed as unmapped",
          any(e["sector"] == "unmapped" and e["value_usd"] == 500 for e in exp), str(exp))
    check("empty book -> empty list", orchestrator._sector_exposure({"positions": []}) == [])


def test_prices_seam_shape():
    print("tools.prices.get_closes input handling (no network paths):")
    from tools.prices import get_closes
    check("empty list -> {}", get_closes([]) == {})
    check("falsy tickers dropped -> {}", get_closes(["", None]) == {})


if __name__ == "__main__":
    for fn in (test_retry_after, test_short_interest, test_earnings_surprises,
               test_sector_exposure, test_prices_seam_shape):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All P2 helper checks passed.")
