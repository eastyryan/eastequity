"""Tests for plan persistence on position records. Pure Python, no network:

    python3 tests/test_position_plan_persistence.py

Regression context: stops/targets/horizons used to exist ONLY in the proposals
journal, reconstructed each run by ticker (latest BUY wins). Pruning the
journal silently disabled forced-exit enforcement for every open position, and
a re-buy aliased the old lot's plan. Numeric plan fields now ride on the
position itself; the journal remains the narrative source and fallback.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import simulated_broker
from tools.portfolio_state import _merge_plan

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


PLAN_A = {"stop_loss": 408.0, "target_price": 520.0, "holding_horizon_days": 45,
          "entry_price_max": 455.0, "confidence": 0.7}
PLAN_B = {"stop_loss": 430.0, "target_price": 540.0, "holding_horizon_days": 30,
          "entry_price_max": 470.0, "confidence": 0.75}


def buy(plan, size=900.0, ref=450.0):
    order = simulated_broker.place_order({
        "ticker": "DELL", "action": "BUY", "position_size_usd": size,
        "reference_price": ref, "proposal_id": "test-run", "plan": plan,
    })
    return simulated_broker.readback(order["order_id"])


try:  # pytest-only: seed a temp EMPTY book once per module (tests share state
    # in file order, mirroring the __main__ script flow) - never the real ledger.
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _tmp_book(tmp_path_factory):
        orig = simulated_broker.STATE_FILE
        tmp_state = tmp_path_factory.mktemp("plans") / "portfolio.json"
        tmp_state.write_text(json.dumps({
            "cash_usd": 10000.0, "total_equity_usd": 10000.0,
            "positions": [], "pending_orders": {}, "history": [],
        }))
        simulated_broker.STATE_FILE = tmp_state
        yield
        simulated_broker.STATE_FILE = orig
except ImportError:  # plain-script mode
    pass


def test_new_position_carries_plan():
    print("new BUY persists the numeric plan on the position:")
    fill = buy(PLAN_A)
    check("filled", fill["status"] == "filled", fill["status"])
    state = simulated_broker.get_portfolio()
    pos = next(p for p in state["positions"] if p["ticker"] == "DELL")
    check("plan stored on position", pos.get("plan") == PLAN_A, f"got {pos.get('plan')}")


def test_scale_in_overwrites_plan():
    print("scale-in add overwrites with the latest BUY's plan:")
    fill = buy(PLAN_B, size=400.0, ref=460.0)
    check("add filled", fill["status"] == "filled" and fill.get("is_add"), fill["status"])
    state = simulated_broker.get_portfolio()
    pos = next(p for p in state["positions"] if p["ticker"] == "DELL")
    check("latest plan governs", pos.get("plan") == PLAN_B, f"got {pos.get('plan')}")


def test_order_without_plan_keeps_position_clean():
    print("orders without a plan don't write one:")
    order = simulated_broker.place_order({
        "ticker": "HPE", "action": "BUY", "position_size_usd": 300.0,
        "reference_price": 48.0, "proposal_id": "test-run",
    })
    simulated_broker.readback(order["order_id"])
    state = simulated_broker.get_portfolio()
    pos = next(p for p in state["positions"] if p["ticker"] == "HPE")
    check("no plan key", "plan" not in pos, f"got {pos.get('plan')}")


def test_merge_plan_semantics():
    print("_merge_plan: persisted numbers win, journal supplies narrative:")
    journal_plan = {"thesis": "Original written thesis.", "stop_loss": 400.0,
                    "catalysts": ["Earnings"], "confidence": 0.65}
    persisted = {"stop_loss": 430.0, "target_price": 540.0, "confidence": None}
    merged = _merge_plan(persisted, journal_plan)
    check("persisted stop wins", merged["stop_loss"] == 430.0)
    check("persisted target added", merged["target_price"] == 540.0)
    check("journal thesis kept", merged["thesis"] == "Original written thesis.")
    check("None in persisted does not clobber journal", merged["confidence"] == 0.65)
    check("both empty -> None (exit_guard skips, never force-exits blind)",
          _merge_plan(None, None) is None)
    check("persisted-only works after journal pruning",
          _merge_plan({"stop_loss": 10.0}, None) == {"stop_loss": 10.0})


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        tmp_state = Path(td) / "portfolio.json"
        tmp_state.write_text(json.dumps({
            "cash_usd": 10000.0, "total_equity_usd": 10000.0,
            "positions": [], "pending_orders": {}, "history": [],
        }))
        simulated_broker.STATE_FILE = tmp_state  # never touch the real book
        for fn in (test_new_position_carries_plan, test_scale_in_overwrites_plan,
                   test_order_without_plan_keeps_position_clean, test_merge_plan_semantics):
            fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All plan-persistence checks passed.")
