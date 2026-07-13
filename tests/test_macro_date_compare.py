"""Tests for the date-based ~3-months-ago comparison in tools.macro_regime.

Pure Python, no network and NO pandas (the helpers under test are module-level
and pandas-free). Run with:

    python3 tests/test_macro_date_compare.py

The bug these guard against: a FIXED index offset (iloc[-64]) compared the latest
value against ~64 OBSERVATIONS ago, which for MONTHLY (UNRATE, CPIAUCSL) and WEEKLY
(NFCI) series is years ago, not three months — corrupting direction/three_months_ago.
The fix picks the observation closest to ~90 days before the latest observation's
date, so it is correct for daily, weekly, and monthly series alike.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.macro_regime import (  # noqa: E402
    _cpi_yoy,
    _infer_frequency,
    _prior_index_by_date,
    _to_date,
)

FAILURES = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  [{detail}]" if not cond and detail else ""))
    if not cond:
        FAILURES.append(label)


def _month_dates(n, start=(2019, 1, 1)):
    """n consecutive first-of-month dates."""
    y, m, d = start
    out = []
    for _ in range(n):
        out.append(date(y, m, d))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _daily_dates(n, end=date(2026, 6, 1)):
    """n consecutive calendar-daily dates ending at `end` (ascending)."""
    return [end - timedelta(days=(n - 1 - i)) for i in range(n)]


def _weekly_dates(n, end=date(2026, 6, 5)):
    """n consecutive weekly (7-day) dates ending at `end` (ascending)."""
    return [end - timedelta(days=7 * (n - 1 - i)) for i in range(n)]


def test_monthly_picks_three_months_not_years():
    print("monthly series (UNRATE/CPI cadence):")
    dates = _month_dates(72)              # 6 years of monthly obs
    idx = _prior_index_by_date(dates, days_back=90)
    latest = len(dates) - 1
    check("picks the observation ~3 months back (index latest-3)", idx == latest - 3,
          f"idx={idx} latest={latest}")
    check("does NOT pick the fixed -64-observation offset (~5.3 years ago)",
          idx != latest - 64, f"idx={idx}")
    gap_days = (dates[latest] - dates[idx]).days
    check("chosen point is 80-100 days before latest", 80 <= gap_days <= 100, f"gap={gap_days}d")


def test_weekly_picks_thirteen_weeks_not_sixtyfour():
    print("weekly series (NFCI cadence):")
    dates = _weekly_dates(120)
    idx = _prior_index_by_date(dates, days_back=90)
    latest = len(dates) - 1
    gap_days = (dates[latest] - dates[idx]).days
    check("chosen point is ~13 weeks (85-95 days) back", 85 <= gap_days <= 95, f"gap={gap_days}d")
    check("does NOT pick 64 weeks ago", idx != latest - 64, f"idx={idx}")


def test_daily_is_exactly_ninety_days():
    print("daily series (DGS10/VIXCLS cadence):")
    dates = _daily_dates(400)
    idx = _prior_index_by_date(dates, days_back=90)
    latest = len(dates) - 1
    check("calendar-daily picks exactly 90 days back", dates[idx] == dates[latest] - timedelta(days=90),
          f"{dates[idx]}")


def test_short_series_falls_back_to_earliest():
    print("series shorter than 3 months:")
    dates = _daily_dates(30)   # only 30 days of history
    idx = _prior_index_by_date(dates, days_back=90)
    check("falls back to the earliest observation (index 0)", idx == 0, f"idx={idx}")


def test_frequency_inference():
    print("native frequency inference:")
    check("daily", _infer_frequency(_daily_dates(60)) == "daily")
    check("weekly", _infer_frequency(_weekly_dates(60)) == "weekly")
    check("monthly", _infer_frequency(_month_dates(60)) == "monthly")


def test_cpi_yoy_is_date_anchored():
    print("CPI year-over-year (date-anchored):")
    dates = _month_dates(30)
    vals = [100.0 + i for i in range(30)]   # linear index level
    prior_i = _prior_index_by_date(dates, days_back=90)
    latest_yoy, prior_yoy, ok = _cpi_yoy(dates, vals, prior_i)
    check("reports ok with >=12 months of history", ok is True)
    n = len(vals)
    exp_latest = round((vals[n - 1] / vals[n - 13] - 1) * 100, 2)
    check("latest YoY uses the value ~12 months (by date) earlier",
          round(latest_yoy, 2) == exp_latest, f"{round(latest_yoy,2)} vs {exp_latest}")
    exp_prior = round((vals[prior_i] / vals[prior_i - 12] - 1) * 100, 2)
    check("3-months-ago YoY also anchors 12 months before THAT point",
          round(prior_yoy, 2) == exp_prior, f"{round(prior_yoy,2)} vs {exp_prior}")


def test_cpi_yoy_insufficient_history():
    print("CPI YoY with <12 months history:")
    dates = _month_dates(6)
    vals = [100.0 + i for i in range(6)]
    prior_i = _prior_index_by_date(dates, days_back=90)
    _, _, ok = _cpi_yoy(dates, vals, prior_i)
    check("declines (ok=False) rather than fabricating a YoY", ok is False)


def test_to_date_accepts_strings_and_dates():
    print("_to_date coercion:")
    check("ISO string", _to_date("2026-03-15") == date(2026, 3, 15))
    check("date passthrough", _to_date(date(2026, 3, 15)) == date(2026, 3, 15))


if __name__ == "__main__":
    for fn in (test_monthly_picks_three_months_not_years,
               test_weekly_picks_thirteen_weeks_not_sixtyfour,
               test_daily_is_exactly_ninety_days,
               test_short_series_falls_back_to_earliest,
               test_frequency_inference,
               test_cpi_yoy_is_date_anchored,
               test_cpi_yoy_insufficient_history,
               test_to_date_accepts_strings_and_dates):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All macro date-comparison checks passed.")
