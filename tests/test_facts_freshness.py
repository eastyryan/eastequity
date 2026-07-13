"""Regression tests for the frame-filter staleness bug (the DELL $23.4B incident).

DELL's newest quarters exist in companyfacts ONLY as frame-annotated entries; the
old `frame is None` filters silently served the year-old quarter as "latest". These
tests inject a synthetic companyfacts units array reproducing that exact reporting
pattern and assert the fixed selectors return the true newest quarter.

    python3 tests/test_facts_freshness.py

Pure Python, no network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sec_filings import dedupe_facts, _annualish  # noqa: E402
from tools.fundamentals import _rows_from_units  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# Synthetic units mimicking DELL's real reporting pattern (values in $B for legibility):
# - old quarters appear twice: framed original + unframed comparative from a later filing
# - the NEWEST quarter (2026-05-01) exists ONLY as a framed entry  <-- the trap
# - unframed rows also exist for YTD spans (181d/272d) which must be excluded
# - one period is amended: a later-filed 10-Q/A corrects the value
UNITS = [
    # Q1 FY2026 (year-old): framed original + unframed comparative (identical value)
    {"start": "2025-02-01", "end": "2025-05-02", "val": 23.4, "form": "10-Q",
     "filed": "2025-06-10", "frame": None, "fy": 2026, "fp": "Q1"},
    {"start": "2025-02-01", "end": "2025-05-02", "val": 23.4, "form": "10-Q",
     "filed": "2026-06-09", "frame": "CY2025Q1", "fy": 2027, "fp": "Q1"},
    # Q2 FY2026: single-quarter row is FRAMED-ONLY; unframed twin is the 181d YTD row
    {"start": "2025-05-03", "end": "2025-08-01", "val": 29.8, "form": "10-Q",
     "filed": "2025-09-08", "frame": "CY2025Q2", "fy": 2026, "fp": "Q2"},
    {"start": "2025-02-01", "end": "2025-08-01", "val": 53.2, "form": "10-Q",
     "filed": "2025-09-08", "frame": None, "fy": 2026, "fp": "Q2"},  # YTD
    # Q3 FY2026: framed-only single quarter + unframed 272d YTD
    {"start": "2025-08-02", "end": "2025-10-31", "val": 27.0, "form": "10-Q",
     "filed": "2025-12-09", "frame": "CY2025Q3", "fy": 2026, "fp": "Q3"},
    {"start": "2025-02-01", "end": "2025-10-31", "val": 80.2, "form": "10-Q",
     "filed": "2025-12-09", "frame": None, "fy": 2026, "fp": "Q3"},  # YTD
    # FY2026 annual (363d) in the 10-K
    {"start": "2025-02-01", "end": "2026-01-30", "val": 110.0, "form": "10-K",
     "filed": "2026-03-16", "frame": None, "fy": 2026, "fp": "FY"},
    # THE NEWEST QUARTER - framed-only (this is what the old code dropped)
    {"start": "2026-01-31", "end": "2026-05-01", "val": 43.8, "form": "10-Q",
     "filed": "2026-06-09", "frame": "CY2026Q1", "fy": 2027, "fp": "Q1"},
    # A proxy-statement re-report that must be ignored (form filter)
    {"start": "2025-02-01", "end": "2026-01-30", "val": 110.0, "form": "DEF 14A",
     "filed": "2026-05-15", "frame": "CY2025", "fy": 2026, "fp": "FY"},
]

# An amended period: original + later-filed correction (latest filed must win)
AMENDED = [
    {"start": "2025-11-01", "end": "2026-01-30", "val": 99.0, "form": "10-Q",
     "filed": "2026-03-01", "frame": None, "fy": 2026, "fp": "Q3"},
    {"start": "2025-11-01", "end": "2026-01-30", "val": 101.0, "form": "10-Q/A",
     "filed": "2026-04-01", "frame": None, "fy": 2026, "fp": "Q3"},
]


def test_dedupe_facts_keeps_framed_newest():
    print("dedupe_facts (the core fix):")
    q = dedupe_facts(UNITS, min_span=80, max_span=100)
    ends = [u["end"] for u in q]
    check("newest framed-only quarter (2026-05-01) is KEPT",
          "2026-05-01" in ends, str(ends))
    check("newest quarter is LAST (sorted)", ends[-1] == "2026-05-01", str(ends))
    check("framed-only Q2/Q3 kept too",
          "2025-08-01" in ends and "2025-10-31" in ends, str(ends))
    check("YTD rows excluded by span",
          all(u["val"] not in (53.2, 80.2) for u in q))
    check("annual row excluded from quarterly", all(u["end"] != "2026-01-30" for u in q))
    check("duplicate old quarter deduped to one row",
          ends.count("2025-05-02") == 1)
    check("DEF 14A ignored", all(u["form"] != "DEF 14A" for u in dedupe_facts(UNITS)))


def test_dedupe_prefers_latest_filed():
    print("amendments win:")
    rows = dedupe_facts(AMENDED, min_span=80, max_span=100)
    check("one row for the amended period", len(rows) == 1, str(rows))
    check("later-filed 10-Q/A value wins", rows and rows[0]["val"] == 101.0,
          str(rows and rows[0]["val"]))


def test_annualish_quarterly_and_annual():
    print("_annualish (filing brief path):")
    q = _annualish(UNITS, quarterly=True)
    check("quarterly ends at 2026-05-01 with val 43.8",
          q and q[-1]["end"] == "2026-05-01" and q[-1]["val"] == 43.8,
          str(q and (q[-1]["end"], q[-1]["val"])))
    a = _annualish(UNITS, quarterly=False)
    check("annual returns the 10-K FY row",
          a and a[-1]["end"] == "2026-01-30" and a[-1]["val"] == 110.0)


def test_fundamentals_rows_from_units():
    print("fundamentals._rows_from_units (deep fundamentals path):")
    rows = _rows_from_units(UNITS, instant=False, keep=8)
    ends = [r["period_end"] for r in rows]
    check("newest framed-only quarter present", "2026-05-01" in ends, str(ends))
    newest = [r for r in rows if r["period_end"] == "2026-05-01"][0]
    check("value is the fresh 43.8", newest["value"] == 43.8)
    check("period_days carried for de-YTD logic", newest.get("period_days") == 90,
          str(newest.get("period_days")))
    am = _rows_from_units(AMENDED, instant=False, keep=4)
    check("amendment wins here too", am and am[-1]["value"] == 101.0)


def test_old_behavior_would_have_failed():
    """Prove the test catches the bug: simulate the OLD filter and show it's stale."""
    print("counterfactual (old filter is provably stale):")
    old = [u for u in UNITS if u.get("form") in ("10-Q", "10-K") and u.get("frame") is None]
    from datetime import date
    old_q = [u for u in old
             if u.get("start") and 80 <= (date.fromisoformat(u["end"])
                                          - date.fromisoformat(u["start"])).days <= 100]
    newest_old = max((u["end"] for u in old_q), default=None)
    check("old filter tops out at the YEAR-OLD quarter (2025-05-02)",
          newest_old == "2025-05-02", str(newest_old))


if __name__ == "__main__":
    for fn in (test_dedupe_facts_keeps_framed_newest, test_dedupe_prefers_latest_filed,
               test_annualish_quarterly_and_annual, test_fundamentals_rows_from_units,
               test_old_behavior_would_have_failed):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All facts-freshness checks passed.")
