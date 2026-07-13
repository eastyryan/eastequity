"""Tests for fundamental_screen ET timezone gating (_eastern_now,
_us_eastern_offset_hours, REFRESH_HOURS_ET).

The bug: refresh gated on datetime.now().hour ('ET' in the comment) but the
cloud host is UTC, so the 6am/9am ET pre-market windows were missed. These
tests inject known UTC instants and prove the ET hour (and the refresh-window
membership) is correct across DST.

Pure Python, no network - run with:  python3 tests/test_fundamental_et_window.py
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.fundamental_screen import (REFRESH_HOURS_ET, _eastern_now,
                                      _us_eastern_offset_hours)

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def et_hour(y, mo, d, h, mi=0):
    return _eastern_now(datetime(y, mo, d, h, mi, tzinfo=timezone.utc)).hour


def test_offset_dst_rule():
    print("US-Eastern DST offset rule (pure fallback, no tzdata):")
    check("July is EDT (UTC-4)", _us_eastern_offset_hours(date(2026, 7, 13)) == 4)
    check("January is EST (UTC-5)", _us_eastern_offset_hours(date(2026, 1, 12)) == 5)
    check("late-Nov is EST", _us_eastern_offset_hours(date(2026, 11, 30)) == 5)
    check("mid-March is EDT", _us_eastern_offset_hours(date(2026, 3, 20)) == 4)


def test_the_missed_window_now_fires():
    print("the regression: 10:00 UTC IS 6am ET and must hit the window:")
    # OLD code saw datetime.now().hour == 10 (UTC) -> not in {5,6,8,9} -> MISSED.
    h = et_hour(2026, 7, 13, 10, 0)
    check("10:00 UTC (summer) -> 6am ET", h == 6, f"got {h}")
    check("6am ET is in REFRESH_HOURS_ET", h in REFRESH_HOURS_ET)


def test_summer_9am_window():
    print("13:30 UTC summer -> 9:30am ET (in window):")
    h = et_hour(2026, 7, 13, 13, 30)
    check("13:30 UTC (summer) -> 9am ET", h == 9, f"got {h}")
    check("9am ET is in window", h in REFRESH_HOURS_ET)


def test_dst_contrast_same_utc():
    print("same UTC instant gives different ET hour across DST:")
    summer = et_hour(2026, 7, 13, 14, 30)   # EDT: 14:30-4 = 10:30 ET
    winter = et_hour(2026, 1, 12, 14, 30)   # EST: 14:30-5 = 09:30 ET
    check("summer 14:30 UTC -> 10am ET (NOT window)", summer == 10 and summer not in REFRESH_HOURS_ET, f"got {summer}")
    check("winter 14:30 UTC -> 9am ET (in window)", winter == 9 and winter in REFRESH_HOURS_ET, f"got {winter}")


def test_afternoon_never_refreshes():
    print("mid-afternoon ET is outside every pre-market window:")
    h = et_hour(2026, 7, 13, 18, 0)   # 14:00 ET
    check("18:00 UTC -> 2pm ET, not in window", h == 14 and h not in REFRESH_HOURS_ET, f"got {h}")


if __name__ == "__main__":
    for fn in (test_offset_dst_rule, test_the_missed_window_now_fires,
               test_summer_9am_window, test_dst_contrast_same_utc,
               test_afternoon_never_refreshes):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All fundamental-screen ET-window checks passed.")
