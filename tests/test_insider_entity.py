"""Tests for insider_form4._is_entity — the whole-word entity tokenizer.

The old bare-substring check tagged PEOPLE as institutions ("RALPH" contains
"LP", "PHILIP"/"ALPHA" likewise) and dropped them from the discretionary
buy/sell tallies, which could suppress a real cluster-buy or notable-selling
signal. This proves people stay people and real entities are still caught.

Pure Python, no network - run with:  python3 tests/test_insider_entity.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.insider_form4 import _is_entity

FAILURES = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILURES.append(name)


def test_people_are_not_entities():
    print("people are NOT entities (the RALPH/PHILIP/ALPHA bug):")
    # These all contain the substring "LP" or "LC"/"CORP" fragments but are people.
    for person in ["RALPH LAUREN", "PHILIP FALCONE", "ALPHA REMY", "RALPH J ROBERTS",
                   "DELL MICHAEL S", "PHILP T JONES", "CAROL P WILLIAMS",
                   "CARL P ICAHN", "MARCORP ANDERSON"]:
        check(f"{person!r} -> person", _is_entity(person) is False)


def test_entities_are_entities():
    print("real entities ARE entities:")
    for ent in ["ACME CAPITAL LP", "ACME CAPITAL L.P.", "BLACKROCK INC",
                "VANGUARD GROUP INC", "SOME GROWTH FUND LLC", "XYZ L.L.C.",
                "TIGER GLOBAL MANAGEMENT LLC", "SILVER LAKE PARTNERS",
                "SEQUOIA HOLDINGS LTD", "MORGAN TRUST", "GS ADVISORS LP"]:
        check(f"{ent!r} -> entity", _is_entity(ent) is True)


def test_dotted_acronyms_collapse():
    print("dotted acronyms collapse to a single marker token:")
    check("'FOO L. P.' (spaced dots) -> entity", _is_entity("FOO L. P.") is True)
    check("'BAR L.L.C.' -> entity", _is_entity("BAR L.L.C.") is True)
    # A person with a plain middle initial P. must NOT trip the LP marker.
    check("'JANE P SMITH' -> person", _is_entity("JANE P SMITH") is False)


if __name__ == "__main__":
    for fn in (test_people_are_not_entities, test_entities_are_entities,
               test_dotted_acronyms_collapse):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All insider entity-tokenizer checks passed.")
