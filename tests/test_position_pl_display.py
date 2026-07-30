"""Tests for publish._with_unrealized — the mark-to-market P/L the dashboard's
UNREALIZED column reads. Pure Python, no network - run with:

    python3 tests/test_position_pl_display.py

Regression context (2026-07-30): the site rendered `position.unrealized_pct`,
but nothing ever wrote that key - broker state carries only avg_cost,
last_price and market_value_usd. Across the last 40 published runs the field
was populated on 0 of 16 position rows, so every open position displayed a
dash where its live P/L should be. publish now derives it at publish time;
this file pins the arithmetic and the fail-soft behavior on partial data.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib.publish import _with_unrealized

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_winner_and_loser():
    print("P/L derives from avg_cost vs the current mark:")
    up = _with_unrealized({"ticker": "BKR", "quantity": 0.995395,
                           "avg_cost": 58.198, "last_price": 59.89,
                           "market_value_usd": 59.61})
    check("percent gain", up["unrealized_pct"] == 2.91, f"got {up['unrealized_pct']}")
    check("dollar gain", up["unrealized_usd"] == 1.68, f"got {up['unrealized_usd']}")
    down = _with_unrealized({"ticker": "DELL", "quantity": 2.0,
                             "avg_cost": 450.0, "last_price": 400.0})
    check("percent loss is negative", down["unrealized_pct"] == -11.11,
          f"got {down['unrealized_pct']}")
    check("dollar loss is negative", down["unrealized_usd"] == -100.0,
          f"got {down['unrealized_usd']}")


def test_mark_falls_back_to_market_value():
    print("no last_price: the mark is implied by market_value / quantity:")
    p = _with_unrealized({"ticker": "X", "quantity": 2.0, "avg_cost": 10.0,
                          "market_value_usd": 18.0})
    check("implied mark recorded", p["last_price"] == 9.0, f"got {p.get('last_price')}")
    check("percent off the implied mark", p["unrealized_pct"] == -10.0,
          f"got {p['unrealized_pct']}")


def test_original_fields_survive():
    print("the position dict is returned intact, not replaced:")
    src = {"ticker": "BKR", "quantity": 1.0, "avg_cost": 50.0, "last_price": 55.0,
           "plan": {"stop_loss": 45.0}, "sector": "energy"}
    p = _with_unrealized(src)
    check("plan preserved", p["plan"] == {"stop_loss": 45.0})
    check("sector preserved", p["sector"] == "energy")
    check("input not mutated", "unrealized_pct" not in src)


def test_partial_data_fails_soft():
    print("missing or unusable inputs leave the row alone (dash, never a wrong number):")
    for name, pos in (
        ("no mark at all", {"ticker": "Z", "quantity": 1.0, "avg_cost": 10.0}),
        ("zero quantity", {"ticker": "Y", "quantity": 0, "avg_cost": 10.0, "last_price": 5.0}),
        ("zero cost basis", {"ticker": "W", "quantity": 1.0, "avg_cost": 0, "last_price": 5.0}),
        ("garbage cost", {"ticker": "V", "quantity": 1.0, "avg_cost": "oops", "last_price": 5.0}),
        ("empty position", {}),
    ):
        p = _with_unrealized(pos)
        check(f"{name}: no P/L invented",
              "unrealized_pct" not in p and "unrealized_usd" not in p,
              f"got {p.get('unrealized_pct')}")


if __name__ == "__main__":
    for fn in (test_winner_and_loser, test_mark_falls_back_to_market_value,
               test_original_fields_survive, test_partial_data_fails_soft):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All position P/L display checks passed.")
