"""Consolidation regression: ONE copy of the ET calendar-day helpers.

    python3 tests/test_validator_audit_et_time.py

Regression context (2026-08-05 audit): three divergent copies were live at
once — runlib/core.py's et_now/et_date/to_et_date; runlib/analytics.py, which
IMPORTED those names at :12 and then silently SHADOWED all three with local
redefinitions at :929-958; and validator.py's private _et_now/_et_today/
_to_et_date with a subtly different fallback chain. They disagreed on whether
a naive timestamp is UTC or machine-local, and on whether the no-tzdata
fallback converts to UTC before the fixed -4h shift. tools/et_time.py is now
the single source; this file pins the delegation and the chosen (safest)
fallback semantics so a fourth copy cannot quietly reappear.

Standalone: no conftest, no network, no state/ access at all.
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import et_time
import runlib.core as core
import runlib.analytics as analytics
import validator

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_single_source_everywhere():
    print("every module binds the SAME function objects (no shadowing left):")
    check("core.et_now is et_time.et_now", core.et_now is et_time.et_now)
    check("core.et_date is et_time.et_date", core.et_date is et_time.et_date)
    check("core.to_et_date is et_time.to_et_date_str",
          core.to_et_date is et_time.to_et_date_str)
    check("analytics no longer shadows core",
          analytics.et_now is core.et_now
          and analytics.et_date is core.et_date
          and analytics.to_et_date is core.to_et_date)
    check("validator._et_now delegates", validator._et_now is et_time.et_now)
    check("validator._et_today delegates", validator._et_today is et_time.et_today)
    check("validator._to_et_date delegates",
          validator._to_et_date is et_time.to_et_date)


def test_dst_is_real_when_zoneinfo_present():
    print("with zoneinfo, EST (winter, UTC-5) and EDT (summer, UTC-4) both correct:")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
    except Exception:
        print("  [SKIP] no tzdata on this machine — fallback tests below still run")
        return
    # 04:30Z in January is 23:30 EST the PRIOR day; a fixed -4h would say 00:30 same day.
    check("winter crosses midnight on the EST offset",
          et_time.to_et_date("2026-01-15T04:30:00Z") == date(2026, 1, 14))
    check("summer stays same-day on the EDT offset",
          et_time.to_et_date("2026-07-15T04:30:00Z") == date(2026, 7, 15))


def test_naive_timestamps_are_utc():
    print("naive timestamps are treated as UTC (the 2-of-3 majority behavior,")
    print("and what the UTC cloud runner always did — never machine-local):")
    check("naive == explicit-UTC",
          et_time.to_et_date("2026-08-05T01:30:00")
          == et_time.to_et_date("2026-08-05T01:30:00+00:00")
          == date(2026, 8, 4))


def test_strict_contract_matches_old_validator():
    print("strict variant (validator's old contract): None on garbage, date on good:")
    check("garbage -> None", et_time.to_et_date("not-a-timestamp") is None)
    check("None -> None", et_time.to_et_date(None) is None)
    check("empty -> None", et_time.to_et_date("  ") is None)
    check("good -> date object",
          isinstance(et_time.to_et_date("2026-08-05T14:00:00Z"), date))


def test_string_contract_matches_old_runlib():
    print("string variant (runlib's old contract): ISO date, [:10] salvage:")
    check("good -> ISO string",
          core.to_et_date("2026-08-05T14:00:00Z") == "2026-08-05")
    check("garbage -> first 10 chars (slot reports group on these)",
          core.to_et_date("garbage-not-a-ts") == "garbage-no")
    check("falsy -> None", core.to_et_date(None) is None
          and core.to_et_date("") is None)
    check("str and date variants agree on parseable input",
          core.to_et_date("2026-08-05T01:30:00Z")
          == et_time.to_et_date("2026-08-05T01:30:00Z").isoformat())


def test_fallback_equivalence_without_tzdata():
    print("no-tzdata fallback: UTC-normalize FIRST, then the fixed -4h (EDT) shift")
    print("(the safest of the three old chains — the others double-shifted")
    print("non-UTC offsets by subtracting 4h in the timestamp's own zone):")
    real = et_time._eastern
    et_time._eastern = lambda: None  # simulate a minimal image with no tzdata
    try:
        # Winter date now lands on the fixed -4h answer — documented approximation.
        check("fixed -4h in winter (documented ~1h error near midnight)",
              et_time.to_et_date("2026-01-15T04:30:00Z") == date(2026, 1, 15))
        # A +05:00 timestamp is 23:30Z Jan 14 -> 19:30 'ET' Jan 14. The old
        # runlib fallbacks subtracted 4h without converting and answered Jan 15.
        check("non-UTC offsets are normalized before the shift",
              et_time.to_et_date("2026-01-15T04:30:00+05:00") == date(2026, 1, 14))
        now_fallback = et_time.et_now()
        approx = datetime.now(timezone.utc) - timedelta(hours=4)
        check("et_now falls back to UTC-4",
              abs((now_fallback - approx).total_seconds()) < 5,
              str(now_fallback))
        check("et_date/et_today stay consistent with et_now in fallback",
              et_time.et_date() == et_time.et_today().isoformat())
    finally:
        et_time._eastern = real


def test_the_daily_buy_cap_path_still_works():
    print("validator's calendar consumers still function through the delegation:")
    today = validator._et_today()
    ts = datetime.now(timezone.utc).isoformat()
    n = validator._count_filled_buys_today(
        {"history": [
            {"ticker": "AAA", "action": "BUY", "status": "filled", "filled_at": ts},
            {"ticker": "BBB", "action": "BUY", "status": "filled",
             "filled_at": "2020-01-02T15:00:00Z"},
            {"ticker": "CCC", "action": "BUY", "status": "filled",
             "filled_at": "un-parseable"},   # fail open: not counted
        ]}, today_et=today)
    check("counts exactly today's filled BUY", n == 1, str(n))


if __name__ == "__main__":
    for fn in (test_single_source_everywhere,
               test_dst_is_real_when_zoneinfo_present,
               test_naive_timestamps_are_utc,
               test_strict_contract_matches_old_validator,
               test_string_contract_matches_old_runlib,
               test_fallback_equivalence_without_tzdata,
               test_the_daily_buy_cap_path_still_works):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All et_time consolidation checks passed.")
