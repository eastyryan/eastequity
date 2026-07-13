"""Tests for smart_money_13f: the CUSIP-keyed QoQ delta engine (_compute_deltas)
and the strict normalized issuer-name matcher (_names_match).

The tool's whole point is quarter-over-quarter ACTIVITY (new/added/trimmed/
exited), not static position levels; and holdings must attach to the right
ticker without the old first-word-substring mismatches.

Pure Python, no network - run with:  python3 tests/test_smart_money_deltas.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.smart_money_13f import _compute_deltas, _names_match

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _pos(shares, value, issuer):
    return {"shares": shares, "value_usd": value, "issuer": issuer}


def test_qoq_classification():
    print("QoQ delta classification with injected holdings:")
    latest = {
        "C1": _pos(100, 1000, "NVIDIA CORP"),   # unchanged -> held
        "C2": _pos(50, 500, "APPLE INC"),       # 80 -> 50 -> trimmed
        "C4": _pos(200, 2000, "NEWCO INC"),     # absent before -> new
        "C5": _pos(300, 3000, "BIGADD INC"),    # 100 -> 300 -> added
    }
    prior = {
        "C1": _pos(100, 900, "NVIDIA CORP"),
        "C2": _pos(80, 800, "APPLE INC"),
        "C3": _pos(30, 300, "GONE INC"),        # absent now -> exited
        "C5": _pos(100, 1000, "BIGADD INC"),
    }
    d = _compute_deltas(latest, prior)
    check("C1 held", d["C1"]["action"] == "held", d["C1"]["action"])
    check("C2 trimmed", d["C2"]["action"] == "trimmed", d["C2"]["action"])
    check("C3 exited", d["C3"]["action"] == "exited", d["C3"]["action"])
    check("C4 new", d["C4"]["action"] == "new", d["C4"]["action"])
    check("C5 added", d["C5"]["action"] == "added", d["C5"]["action"])
    # Deltas carry magnitude, not just direction.
    check("C2 share_delta -30", d["C2"]["share_delta"] == -30, d["C2"]["share_delta"])
    check("C2 value_delta -300", d["C2"]["value_delta_usd"] == -300, d["C2"]["value_delta_usd"])
    check("C4 share_delta +200 (from zero)", d["C4"]["share_delta"] == 200, d["C4"]["share_delta"])
    check("C4 prior_shares 0", d["C4"]["prior_shares"] == 0, d["C4"]["prior_shares"])
    check("C3 exited value_delta -300", d["C3"]["value_delta_usd"] == -300, d["C3"]["value_delta_usd"])
    check("C5 pct_share_change +200%", d["C5"]["pct_share_change"] == 200.0, d["C5"]["pct_share_change"])


def test_flat_threshold_is_held():
    print("sub-5% wobble is 'held', not added/trimmed:")
    d = _compute_deltas({"X": _pos(103, 1030, "FOO")}, {"X": _pos(100, 1000, "FOO")})
    check("+3% -> held", d["X"]["action"] == "held", d["X"]["action"])
    d = _compute_deltas({"X": _pos(110, 1100, "FOO")}, {"X": _pos(100, 1000, "FOO")})
    check("+10% -> added", d["X"]["action"] == "added", d["X"]["action"])


def test_empty_prior_all_new():
    print("no prior filing -> everything reads as new:")
    d = _compute_deltas({"X": _pos(100, 1000, "FOO")}, {})
    check("new when prior empty", d["X"]["action"] == "new", d["X"]["action"])


def test_names_match_strict():
    print("strict normalized name matching:")
    good = [
        ("NVIDIA CORP", "NVIDIA CORPORATION"),
        ("MICROSOFT CORP", "Microsoft Corporation"),
        ("VERTIV HOLDINGS CO", "Vertiv Holdings Co"),
        ("PALO ALTO NETWORKS INC", "Palo Alto Networks Inc"),
        ("ADVANCED MICRO DEVICES INC", "Advanced Micro Devices Inc"),
        ("ARM HOLDINGS PLC", "Arm Holdings plc"),
        ("BROADCOM INC", "Broadcom Inc"),
    ]
    for issuer, company in good:
        check(f"match {issuer!r} ~ {company!r}", _names_match(issuer, company))
    bad = [
        ("APPLE INC", "APPLE HOSPITALITY REIT INC"),   # single-token overreach
        ("ARM HOLDINGS PLC", "Armstrong World Industries"),  # substring trap
        ("MICRON TECHNOLOGY INC", "MICROSOFT CORP"),
        ("NVIDIA CORP", "NVE CORP"),
        ("", "NVIDIA CORP"),
    ]
    for issuer, company in bad:
        check(f"reject {issuer!r} !~ {company!r}", not _names_match(issuer, company))


if __name__ == "__main__":
    for fn in (test_qoq_classification, test_flat_threshold_is_held,
               test_empty_prior_all_new, test_names_match_strict):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All smart-money delta/name-match checks passed.")
