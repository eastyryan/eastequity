"""Offline tests for tools.volume_analysis (pure Python, no network).

    python3 tests/test_volume_analysis.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.volume_analysis import (  # noqa: E402
    analyze_volume, obv_series, cmf, rvol, updown_volume_ratio, obv_divergence,
)

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def bars(n, price=100.0, vol=1e6):
    """Flat synthetic tape: (opens, highs, lows, closes, volumes)."""
    return ([price] * n, [price * 1.01] * n, [price * 0.99] * n,
            [price] * n, [vol] * n)


def test_primitives():
    print("primitives:")
    obv = obv_series([1, 2, 1, 3], [10, 10, 10, 10])
    check("OBV signs volume by direction", obv == [0, 10, 0, 10], str(obv))
    check("rvol vs trailing avg", rvol([100.0] * 50 + [300.0]) == 3.0)
    r = updown_volume_ratio([1, 2, 1, 2, 1, 2], [0, 10, 5, 10, 5, 10])
    check("up/down ratio 30/10 = 3", r == 3.0, str(r))
    # CMF: all closes at highs -> strongly positive
    n = 25
    c = cmf([2.0] * n, [1.0] * n, [2.0] * n, [1e6] * n)
    check("CMF +1 when closes pin highs", c == 1.0, str(c))


def test_selling_climax():
    print("selling climax:")
    o, h, l, c, v = bars(60)
    # decline into the last bar; final bar: huge range + 4x volume, close mid-high
    for i in range(-6, 0):
        c[i] = 100 - (6 + i + 1) * 3  # falling closes
        o[i] = c[i] + 1.5
        h[i], l[i] = o[i] + 1, c[i] - 1
    l[-1] = c[-6] - 22
    h[-1] = c[-6] - 8
    c[-1] = l[-1] + 0.8 * (h[-1] - l[-1])  # close near top of a huge bar
    v[-1] = 4.5e6
    out = analyze_volume(o, h, l, c, v)
    check("climax reversal-watch detected",
          out["read"] == "selling_climax_reversal_watch", out["read"])


def test_absorption_at_lows():
    print("absorption (effort without result):")
    o, h, l, c, v = bars(60)
    for i in range(-10, 0):  # drift to lows
        c[i] = 96
        o[i] = 96.2
        h[i], l[i] = 96.5, 95.8
    v[-1] = 2.6e6          # heavy volume...
    h[-1], l[-1] = 96.2, 95.9   # ...tiny range
    o[-1] = c[-1] = 96.0
    out = analyze_volume(o, h, l, c, v)
    check("absorption at lows flagged",
          out["read"] == "absorption_at_lows_accumulation", out["read"])


def test_breakout_confirmation():
    print("breakout sponsorship:")
    o, h, l, c, v = bars(60)
    c[-1] = 104.0
    h[-1] = 104.5
    v[-1] = 2.0e6
    out = analyze_volume(o, h, l, c, v)
    check("2x-volume 20d-high breakout confirmed",
          out.get("breakout_volume_confirmed") is True and out["read"] == "confirmed_breakout",
          out["read"])
    v[-1] = 0.9e6
    out2 = analyze_volume(o, h, l, c, v)
    check("weak-volume breakout flagged suspect",
          out2["read"] == "unconfirmed_breakout_suspect", out2["read"])


def test_obv_divergence_bullish():
    print("OBV divergence:")
    closes = [100.0] * 30
    vols = [1e6] * 30
    # prior window: hard flush to 90 on heavy volume, then recovery on volume
    closes[10], vols[10] = 90.0, 3e6
    closes[11], vols[11] = 99.0, 4e6
    # recent: marginal lower low 89.5 on tiny volume (OBV holds higher)
    closes[-2], vols[-2] = 89.5, 2e5
    closes[-1], vols[-1] = 95.0, 2.5e6
    d = obv_divergence(closes, vols)
    check("bullish divergence (lower low, OBV higher low)", d == "bullish", d)


def test_insufficient_data():
    print("guards:")
    out = analyze_volume([1], [1], [1], [1], [1])
    check("short history -> insufficient_data", out["read"] == "insufficient_data")


if __name__ == "__main__":
    for fn in (test_primitives, test_selling_climax, test_absorption_at_lows,
               test_breakout_confirmation, test_obv_divergence_bullish,
               test_insufficient_data):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All volume-analysis checks passed.")
