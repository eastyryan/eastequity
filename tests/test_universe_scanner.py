"""Offline unit tests for the pure-Python helpers in tools.universe_scanner
(session-window returns, trailing highs, relative strength). No network, no
pandas/numpy - the module imports those lazily inside scan_universe only.

    python3 tests/test_universe_scanner.py

These guard exactly the class of bug being fixed: off-by-one on session windows,
the full-fetch-window masquerading as "6m", and a partial high mislabeled "52w".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.universe_scanner import (  # noqa: E402
    _return_over_sessions, _trailing_high, _rel_strength, _median_dollar_volume,
    _ma_tail, _screen_quality_ok, WEEKS_200, AT_OR_BELOW_TOLERANCE_PCT,
    SESS_1M, SESS_3M, SESS_6M, SESS_52W, MIN_BARS, SESS_ADV, MIN_ADV_USD,
)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_constants():
    print("session constants:")
    check("1m/3m/6m/52w are 21/63/126/252",
          (SESS_1M, SESS_3M, SESS_6M, SESS_52W) == (21, 63, 126, 252))
    check("MIN_BARS admits 3m momentum", MIN_BARS == SESS_3M + 1)


def test_return_over_sessions():
    print("return_over_sessions (off-by-one is the bug we are fixing):")
    closes = [100 * (1.01 ** i) for i in range(22)]  # 22 bars, +1%/session
    r = _return_over_sessions(closes, 21)
    # base bar sits exactly `sessions` bars before the last bar -> closes[-(21+1)]
    check("21-session return uses base closes[-22]",
          r is not None and abs(r - (closes[-1] / closes[-22] - 1)) < 1e-12, f"got {r}")
    check("needs sessions+1 bars (21 bars -> None)",
          _return_over_sessions(closes[:21], 21) is None)
    check("exactly sessions+1 bars is enough",
          _return_over_sessions(closes[:22], 21) is not None)
    # The original defect: 6m on short history must NOT silently span the whole window.
    check("6m on 22 bars -> None (not the full window)",
          _return_over_sessions(closes, SESS_6M) is None)
    check("zero base -> None", _return_over_sessions([0, 1, 2], 2) is None)
    check("empty -> None", _return_over_sessions([], 5) is None)


def test_trailing_high():
    print("trailing_high (true trailing-252 vs partial window):")
    highs = list(range(1, 301))  # 300 ascending bars
    hi, full = _trailing_high(highs, SESS_52W)
    check("full window, peak in range", hi == 300 and full is True, f"got {hi},{full}")
    # Peak placed OUTSIDE the trailing 252 window must be excluded.
    highs2 = [999] + list(range(1, 260))  # 260 bars; 999 is 260 bars back
    hi2, full2 = _trailing_high(highs2, SESS_52W)
    check("older peak excluded from trailing 252", hi2 == 259 and full2 is True,
          f"got {hi2},{full2}")
    hi3, full3 = _trailing_high([5, 3, 9, 2], SESS_52W)
    check("short history flagged partial", hi3 == 9 and full3 is False, f"got {hi3},{full3}")
    check("empty -> (None, False)", _trailing_high([], 252) == (None, False))


def test_rel_strength():
    print("rel_strength (name minus benchmark):")
    check("outperformance positive", abs(_rel_strength(0.10, 0.04) - 0.06) < 1e-12)
    check("underperformance negative", _rel_strength(-0.02, 0.03) < 0)
    check("missing benchmark -> None", _rel_strength(0.10, None) is None)
    check("missing name -> None", _rel_strength(None, 0.04) is None)


def test_median_dollar_volume():
    print("median_dollar_volume (liquidity, spike-robust):")
    # 20 sessions at $100 x 1,000,000 sh = $100M/day -> liquid.
    closes = [100.0] * 25
    vols = [1_000_000] * 25
    adv = _median_dollar_volume(closes, vols)
    check("steady $100M ADV", adv is not None and abs(adv - 100_000_000) < 1, f"got {adv}")
    check("above MIN_ADV_USD -> liquid", adv >= MIN_ADV_USD)
    # A single huge spike must NOT drag the MEDIAN (mean would blow up).
    vols_spiked = [1_000_000] * 24 + [500_000_000]
    adv_s = _median_dollar_volume(closes, vols_spiked)
    check("median ignores one spike", adv_s is not None and abs(adv_s - 100_000_000) < 1, f"got {adv_s}")
    # Only the trailing SESS_ADV sessions count.
    closes2 = [1.0] * 100
    vols2 = [1] * 80 + [10_000_000] * 20  # last 20 are the liquid ones
    adv2 = _median_dollar_volume(closes2, vols2)
    check("uses trailing window only", adv2 is not None and adv2 == 10_000_000, f"got {adv2}")
    # Illiquid microcap: $1 x 5000 sh = $5k/day -> below threshold.
    adv3 = _median_dollar_volume([1.0] * 25, [5000] * 25)
    check("microcap below MIN_ADV_USD", adv3 is not None and adv3 < MIN_ADV_USD, f"got {adv3}")
    check("empty -> None", _median_dollar_volume([], []) is None)
    check("SESS_ADV/threshold sane", SESS_ADV == 20 and MIN_ADV_USD == 20_000_000)


def test_ma_tail_200w():
    print("200-week MA helper (_ma_tail):")
    # 250 weeks of closes rising 100 -> 349; MA over last 200 = mean(150..349) = 249.5
    closes = [100.0 + i for i in range(250)]
    ma, full = _ma_tail(closes, WEEKS_200)
    check("full-window MA over trailing 200 only",
          ma is not None and abs(ma - 249.5) < 1e-9 and full is True, f"got {ma},{full}")
    # 120 weeks (young IPO): mean over what exists, flagged partial
    ma2, full2 = _ma_tail(closes[:120], WEEKS_200)
    check("partial history flagged (no fake '200-week' claim)",
          ma2 is not None and full2 is False, f"got {ma2},{full2}")
    check("empty -> (None, False)", _ma_tail([], WEEKS_200) == (None, False))
    check("None values skipped", _ma_tail([None, 10.0, 20.0], 2)[0] == 15.0)
    check("at-or-below tolerance is +2%", AT_OR_BELOW_TOLERANCE_PCT == 2.0)


def test_screen_quality_gate():
    print("deep-value quality gate (_screen_quality_ok):")
    check("shrinking + falling estimates -> EXCLUDED (value trap)",
          _screen_quality_ok({"fwd_revenue_growth_pct": -8.2, "revision_direction": "down"}) is False)
    check("shrinking but revisions up -> passes (turn possible)",
          _screen_quality_ok({"fwd_revenue_growth_pct": -3.0, "revision_direction": "up"}) is True)
    check("growing + falling revisions -> passes (brain vets)",
          _screen_quality_ok({"fwd_revenue_growth_pct": 12.0, "revision_direction": "down"}) is True)
    check("missing data passes (brain vets)", _screen_quality_ok(None) is True)
    check("empty dict passes", _screen_quality_ok({}) is True)


if __name__ == "__main__":
    for fn in (test_constants, test_return_over_sessions, test_trailing_high,
               test_rel_strength, test_median_dollar_volume,
               test_ma_tail_200w, test_screen_quality_gate):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All universe-scanner helper checks passed.")
