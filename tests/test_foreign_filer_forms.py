"""Tests for foreign-private-issuer form acceptance in tools/fundamentals.py
(_rows_from_units / ACCEPTED_FORMS) plus the runs-heartbeat slot schedule.
Pure Python, no network:

    python3 tests/test_foreign_filer_forms.py

Regression context: _rows_from_units only accepted 10-Q/10-K, so foreign
filers in the universe (TSM, ASML, ARM file 20-F annuals + 6-K interims)
silently returned EMPTY quarterly_facts and blank quality_ratios even though
sec_filings.py handled those forms fine.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.fundamentals import ACCEPTED_FORMS, _rows_from_units

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def unit(form, start, end, val, filed):
    return {"form": form, "start": start, "end": end, "val": val, "filed": filed}


def test_foreign_forms_accepted():
    print("20-F / 40-F / 6-K facts flow through:")
    rows = _rows_from_units([
        unit("20-F", "2025-01-01", "2025-12-31", 90_000_000_000, "2026-03-01"),
        unit("6-K", "2026-01-01", "2026-03-31", 26_000_000_000, "2026-05-10"),
        unit("40-F", "2024-01-01", "2024-12-31", 80_000_000_000, "2025-03-01"),
    ], instant=False, keep=6)
    check("all three rows kept", len(rows) == 3, f"got {len(rows)}")
    check("newest last", rows[-1]["period_end"] == "2026-03-31")
    check("form carried on row", rows[-1]["form"] == "6-K")
    check("period_days present for flow facts", rows[-1].get("period_days") == 89)


def test_amended_variants_accepted():
    print("amended foreign forms (20-F/A, 6-K/A) accepted and win by filed date:")
    rows = _rows_from_units([
        unit("20-F", "2025-01-01", "2025-12-31", 90_000_000_000, "2026-03-01"),
        unit("20-F/A", "2025-01-01", "2025-12-31", 91_000_000_000, "2026-04-01"),
    ], instant=False, keep=6)
    check("deduped to one row", len(rows) == 1, f"got {len(rows)}")
    check("latest-filed amendment wins", rows[0]["value"] == 91_000_000_000)


def test_non_periodic_forms_still_excluded():
    print("8-K / S-1 facts still excluded:")
    rows = _rows_from_units([
        unit("8-K", "2026-01-01", "2026-03-31", 1, "2026-05-01"),
        unit("S-1", "2026-01-01", "2026-03-31", 2, "2026-05-01"),
    ], instant=False, keep=6)
    check("no rows from non-periodic forms", rows == [], f"got {rows}")
    check("8-K not in ACCEPTED_FORMS", "8-K" not in ACCEPTED_FORMS)


def test_heartbeat_slots():
    print("runs-heartbeat expected slots match scripts/run_cycle.sh (7-slot policy):")
    import orchestrator
    wk = orchestrator.expected_slots(True)
    we = orchestrator.expected_slots(False)
    check("7 weekday slots", len(wk) == 7, f"got {len(wk)}: {wk}")
    check("the seven: 6,8:45,10:30,12,2,3:30,5:30",
          wk == [6, 8.75, 10.5, 12, 14, 15.5, 17.5], str(wk))
    check("2 weekend slots (midnight + 11:59pm)", len(we) == 2 and 0 in we, f"got {we}")


if __name__ == "__main__":
    for fn in (test_foreign_forms_accepted, test_amended_variants_accepted,
               test_non_periodic_forms_still_excluded, test_heartbeat_slots):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All foreign-filer / heartbeat checks passed.")
