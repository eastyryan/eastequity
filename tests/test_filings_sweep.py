"""Tests for the universe-wide 8-K sweep in tools/filings_sweep.py.
Pure Python, no network — parse_form_idx and the filter are fed fixture
strings:

    python3 tests/test_filings_sweep.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.filings_sweep import (_index_url, _recent_business_days, parse_form_idx,
                                 select_universe_8ks)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# Mirrors the LIVE daily form.idx shape (verified 2026-07-14): preamble, a
# header WRAPPED mid-"CIK" whose label offsets do NOT match the data columns,
# dashed rule, then rows with trailing spaces — plus a form type containing a
# space, a malformed short line, and a row with a non-numeric CIK.
IDX_FIXTURE = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Jul 14, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/

Form Type   Company Name                                                  CIK
      Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
1-A POS          Masterworks Vault 5, LLC                                      1999710     20260714    edgar/data/1999710/0001493152-26-033028.txt
8-K              NVIDIA CORP                                                   1045810     20260714    edgar/data/1045810/0001045810-26-000123.txt
8-K/A            Dell Technologies Inc.                                        1571996     20260714    edgar/data/1571996/0001571996-26-000456.txt
10-Q             SOME OTHER CO                                                 999999      20260714    edgar/data/999999/0000999999-26-000001.txt
8-K              NOT IN UNIVERSE INC                                           555555      20260714    edgar/data/555555/0000555555-26-000002.txt
8-K              BROKEN ROW NO PATH
8-K              BAD CIK CO                                                    12AB34      20260714    edgar/data/bad/0000000000-26-000003.txt
424B2            BOND SHELF LLC                                                777777      20260714    edgar/data/777777/0000777777-26-000004.txt
"""
# The live file pads every row with trailing spaces; add them here explicitly
# (in-source trailing whitespace gets stripped by editors).
IDX_FIXTURE = IDX_FIXTURE.replace("0001045810-26-000123.txt",
                                  "0001045810-26-000123.txt" + " " * 40)


def test_parse_form_idx():
    print("parse_form_idx:")
    rows = parse_form_idx(IDX_FIXTURE)
    forms = [r["form"] for r in rows]
    check("valid rows parsed (header/preamble/dashes skipped)", len(rows) == 6, str(forms))
    check("malformed short line skipped", "BROKEN" not in str(rows))
    check("non-numeric CIK skipped", all(r["cik"].isdigit() for r in rows), str(rows))
    nvda = next((r for r in rows if r["cik"] == "1045810"), None)
    check("form extracted", nvda and nvda["form"] == "8-K", str(nvda))
    check("company extracted", nvda and nvda["company"] == "NVIDIA CORP", str(nvda))
    check("date extracted", nvda and nvda["date"] == "20260714", str(nvda))
    check("path extracted (trailing spaces stripped)",
          nvda and nvda["path"] == "edgar/data/1045810/0001045810-26-000123.txt", str(nvda))
    check("8-K/A amendment parsed", "8-K/A" in forms, str(forms))
    check("form type containing a space parsed", "1-A POS" in forms, str(forms))
    check("empty text -> []", parse_form_idx("") == [])
    check("None -> []", parse_form_idx(None) == [])
    check("noise-only text -> []", parse_form_idx("no rows here\njust noise\n") == [])


def test_select_universe_8ks():
    print("select_universe_8ks:")
    rows = parse_form_idx(IDX_FIXTURE)
    cik_map = {"1045810": "NVDA", "1571996": "DELL", "999999": "XYZ"}
    filers = select_universe_8ks(rows, cik_map)
    tickers = sorted(f["ticker"] for f in filers)
    check("8-K forms filtered to universe CIKs", tickers == ["DELL", "NVDA"], str(filers))
    check("10-Q by universe name excluded", "XYZ" not in tickers)
    check("8-K by non-universe CIK excluded", all(f["ticker"] in cik_map.values() for f in filers))
    nvda = next(f for f in filers if f["ticker"] == "NVDA")
    check("Archives URL built",
          nvda["url"] == "https://www.sec.gov/Archives/edgar/data/1045810/0001045810-26-000123.txt",
          str(nvda))
    padded = [{"form": "8-K", "company": "X", "cik": "0001045810",
               "date": "20260714", "path": "edgar/data/x.txt"}]
    check("zero-padded row CIK matched numerically",
          [f["ticker"] for f in select_universe_8ks(padded, {"1045810": "NVDA"})] == ["NVDA"])
    check("empty rows -> []", select_universe_8ks([], cik_map) == [])


def test_date_helpers():
    print("_recent_business_days / _index_url:")
    days = _recent_business_days(date(2026, 7, 14), 4)  # a Tuesday
    check("weekday start kept, walks back over weekend",
          days == [date(2026, 7, 14), date(2026, 7, 13), date(2026, 7, 10), date(2026, 7, 9)],
          str(days))
    sat = _recent_business_days(date(2026, 7, 11), 1)  # a Saturday
    check("weekend start rolls back to Friday", sat == [date(2026, 7, 10)], str(sat))
    url = _index_url(date(2026, 7, 14))
    check("index url has year/quarter/date",
          url == "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260714.idx",
          url)
    check("Q1 boundary", "QTR1/form.20260331" in _index_url(date(2026, 3, 31)))


if __name__ == "__main__":
    for fn in (test_parse_form_idx, test_select_universe_8ks, test_date_helpers):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All filings-sweep checks passed.")
