"""Tests for the entry-timing indicators in tools/universe_scanner.py
(_rsi14, _macd_state, _adx14, _gap_events). Pure Python, no network:

    python3 tests/test_indicators.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.universe_scanner import _adx14, _gap_events, _macd_state, _rsi14

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_rsi():
    print("RSI(14):")
    up = [100 + i for i in range(40)]
    check("monotonic rise -> 100", _rsi14(up) == 100.0, f"got {_rsi14(up)}")
    alt = [100 + (1 if i % 2 else 0) for i in range(40)]  # +1/-1 alternating
    r = _rsi14(alt)
    check("balanced chop ~= 50", r is not None and 40 <= r <= 60, f"got {r}")
    down = [200 - i for i in range(40)]
    r = _rsi14(down)
    check("monotonic fall -> ~0", r is not None and r < 5, f"got {r}")
    check("too-short history -> None", _rsi14([1, 2, 3]) is None)


def test_macd():
    print("MACD(12/26/9) state:")
    check("too-short -> None", _macd_state([100.0] * 30) is None)
    up = [100 * 1.01 ** i for i in range(60)]
    m = _macd_state(up)
    check("steady uptrend -> positive hist", m and m["hist"] > 0, str(m))
    check("state above zero family", m and m["state"] in ("above_zero", "bull_cross_recent"))
    fresh_turn = [100.0] * 60 + [101.0, 103.0, 106.0]
    m = _macd_state(fresh_turn)
    check("flat-then-3-day-surge -> bull_cross_recent",
          m and m["state"] == "bull_cross_recent", str(m))
    check("hist rising on the turn", m and m["hist_direction"] == "rising", str(m))
    rollover = [100 + i * 0.5 for i in range(60)] + [128, 124, 120]
    m = _macd_state(rollover)
    check("sharp rollover flags falling hist", m and m["hist_direction"] == "falling", str(m))


def test_adx():
    print("ADX(14) + DI:")
    n = 60
    trend_h = [101 + i for i in range(n)]
    trend_l = [99 + i for i in range(n)]
    trend_c = [100 + i for i in range(n)]
    adx_t, dip, din = _adx14(trend_h, trend_l, trend_c)
    check("strong trend -> high ADX", adx_t is not None and adx_t > 50, f"got {adx_t}")
    check("DI+ dominates in an uptrend", dip is not None and dip > din,
          f"di+={dip} di-={din}")
    chop_c = [100 + (1 if i % 2 else 0) for i in range(n)]
    chop_h = [c + 0.5 for c in chop_c]
    chop_l = [c - 0.5 for c in chop_c]
    adx_c, _, _ = _adx14(chop_h, chop_l, chop_c)
    check("chop scores below trend", adx_c is not None and adx_c < adx_t,
          f"chop {adx_c} vs trend {adx_t}")
    check("too-short -> Nones", _adx14([1] * 10, [1] * 10, [1] * 10) == (None, None, None))


def _opens_tracking(closes, gap_opens):
    """Realistic opens: each session opens at the prior close, except the
    explicit gap days in {index: open_price}."""
    opens = [closes[0]] + [closes[i - 1] for i in range(1, len(closes))]
    for i, o in gap_opens.items():
        opens[i] = o
    return opens


def test_gaps():
    print("gap detection (20 sessions, >=2%):")
    closes = [100.0] * 25 + [103.5, 104.0, 104.0, 104.0, 104.0]
    opens = _opens_tracking(closes, {25: 103.0})  # +3% up gap that never fills
    g = _gap_events(opens, closes)
    check("one gap found", g and g["count_20d"] == 1, str(g))
    check("direction up, unfilled",
          g and g["last"]["direction"] == "up" and not g["last"]["filled"], str(g))
    check("sessions_ago correct", g and g["last"]["sessions_ago"] == 4, str(g))

    closes2 = [100.0] * 20 + [97.5, 98.0, 101.0] + [100.0] * 7
    opens2 = _opens_tracking(closes2, {20: 97.0})  # -3% down gap, closed back above at i=22
    g = _gap_events(opens2, closes2)
    check("down gap filled",
          g and g["last"]["direction"] == "down" and g["last"]["filled"], str(g))

    check("sub-2% moves ignored",
          _gap_events([100.0, 101.5], [100.0, 101.5])["count_20d"] == 0)
    check("too-short -> None", _gap_events([100.0], [100.0]) is None)


if __name__ == "__main__":
    for fn in (test_rsi, test_macd, test_adx, test_gaps):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All indicator checks passed.")
