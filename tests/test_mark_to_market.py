"""Tests for simulated_broker.mark_to_market — the every-run re-mark that keeps
last_price/market_value/total_equity honest on hold days. Pure Python, no
network - run with:

    python3 tests/test_mark_to_market.py

Regression context: marks used to refresh only when a trade filled, so a
holding book flat-lined at the last fill price for days (equity frozen at
$9,979.74 July 10-13 while real value drifted). The orchestrator now calls
mark_to_market on every run; this file pins the broker-side behavior.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import simulated_broker

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def seed_state(tmp: Path) -> None:
    tmp.write_text(json.dumps({
        "cash_usd": 8200.01,
        "total_equity_usd": 9979.74,
        "positions": [
            {"ticker": "DELL", "quantity": 2.2189, "avg_cost": 450.67,
             "market_value_usd": 980.53, "last_price": 441.9},
            {"ticker": "HPE", "quantity": 16.3771, "avg_cost": 48.85,
             "market_value_usd": 799.2, "last_price": 48.8},
        ],
        "pending_orders": {},
        "history": [],
    }, indent=2))


try:  # pytest-only: seed a temp book ONCE per module (tests share state in file
    # order, mirroring the __main__ script flow below). Without this, pytest
    # collection ran these against the REAL ledger in isolation and against
    # whatever STATE_FILE a prior test left behind in a full suite.
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _tmp_book(tmp_path_factory):
        orig = simulated_broker.STATE_FILE
        tmp_state = tmp_path_factory.mktemp("mtm") / "portfolio.json"
        seed_state(tmp_state)
        simulated_broker.STATE_FILE = tmp_state
        yield
        simulated_broker.STATE_FILE = orig
except ImportError:  # plain-script mode
    pass


def test_marks_update():
    print("fresh prices update marks and equity:")
    state = simulated_broker.mark_to_market({"DELL": 427.11, "HPE": 47.24})
    dell = next(p for p in state["positions"] if p["ticker"] == "DELL")
    hpe = next(p for p in state["positions"] if p["ticker"] == "HPE")
    check("DELL last_price updated", dell["last_price"] == 427.11)
    check("DELL market value repriced",
          abs(dell["market_value_usd"] - round(2.2189 * 427.11, 2)) < 0.01,
          f"got {dell['market_value_usd']}")
    check("HPE last_price updated", hpe["last_price"] == 47.24)
    expected_equity = round(8200.01 + round(2.2189 * 427.11, 2) + round(16.3771 * 47.24, 2), 2)
    check("total equity recomputed from fresh marks",
          abs(state["total_equity_usd"] - expected_equity) < 0.01,
          f"got {state['total_equity_usd']} want {expected_equity}")
    check("state persisted to disk",
          json.loads(simulated_broker.STATE_FILE.read_text())["total_equity_usd"]
          == state["total_equity_usd"])


def test_missing_ticker_untouched():
    print("position missing from the price map keeps its old mark:")
    state = simulated_broker.mark_to_market({"DELL": 430.00})
    hpe = next(p for p in state["positions"] if p["ticker"] == "HPE")
    check("HPE last_price unchanged", hpe["last_price"] == 47.24, f"got {hpe['last_price']}")
    check("equity still recomputed",
          abs(state["total_equity_usd"]
              - round(8200.01 + round(2.2189 * 430.00, 2) + hpe["market_value_usd"], 2)) < 0.01)


def test_zero_and_empty_are_noops():
    print("zero/None prices skipped; empty dict is a safe no-op:")
    before = json.loads(simulated_broker.STATE_FILE.read_text())
    state = simulated_broker.mark_to_market({"DELL": 0, "HPE": None})
    check("zero price ignored",
          next(p for p in state["positions"] if p["ticker"] == "DELL")["last_price"]
          == next(p for p in before["positions"] if p["ticker"] == "DELL")["last_price"])
    state = simulated_broker.mark_to_market({})
    check("empty map leaves equity unchanged",
          state["total_equity_usd"] == before["total_equity_usd"],
          f"got {state['total_equity_usd']}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        tmp_state = Path(td) / "portfolio.json"
        seed_state(tmp_state)
        simulated_broker.STATE_FILE = tmp_state  # never touch the real book
        for fn in (test_marks_update, test_missing_ticker_untouched,
                   test_zero_and_empty_are_noops):
            fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All mark-to-market checks passed.")
