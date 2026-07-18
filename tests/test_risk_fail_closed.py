"""Risk controls must fail CLOSED, and plan fields carrying risk authority must survive.

Three defects pinned here, all variations on the same theme: a safety layer that
degrades quietly in the risk-INCREASING direction.

1. _position_committed_risk returned 0.0 for a position with no parseable stop, so
   the 8% heat cap under-counted true committed risk. exit_guard meanwhile reads a
   different source (original_plan), so it would stop that position out on a plan the
   heat calculation pretended did not exist.
2. backfill_position_plans compared a 7-key fill-time plan against a 5-key rebuilt
   one, found them unequal BY CONSTRUCTION every run, and wholesale-replaced —
   dropping demand_driver (removing the lot from the 2% theme bucket) and
   thesis_invalidators (breaking exit autopsy).
3. runlib/brain_io compared p["action"] == "BUY" case-sensitively while the validator
   upper()s everything, so a lowercase "buy" validated and then skipped every
   execution gate INCLUDING plan construction.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator  # noqa: E402
from tools import portfolio_state  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    return json.loads((ROOT / "autonomy_config.json").read_text())


def stopped(ticker="AAA", qty=10.0, cost=100.0, stop=95.0):
    return {"ticker": ticker, "quantity": qty, "avg_cost": cost,
            "market_value_usd": qty * cost, "plan": {"stop_loss": stop}}


def stopless(ticker="BBB", qty=10.0, cost=100.0):
    """A position as sync_mirror adopts it, or as the lowercase-action bug created
    it: real size, no plan."""
    return {"ticker": ticker, "quantity": qty, "avg_cost": cost,
            "market_value_usd": qty * cost}


# --------------------------------------------------------------------------- #
# 1. Heat fails closed
# --------------------------------------------------------------------------- #
def test_committed_risk_returns_none_not_zero():
    assert validator._position_committed_risk(stopped()) == pytest.approx(50.0)
    assert validator._position_committed_risk(stopless()) is None
    assert validator._position_committed_risk({"ticker": "X"}) is None
    assert validator._position_committed_risk({"ticker": "X", "quantity": "bad",
                                               "avg_cost": 1, "plan": {"stop_loss": 1}}) is None


def test_sum_separates_unverifiable_from_zero():
    total, bad = validator._sum_committed_risk([stopped(), stopless()])
    assert total == pytest.approx(50.0)
    assert bad == ["BBB"], "a stopless position must be reported, never summed as 0"


def test_trailed_stop_above_cost_is_genuinely_zero_heat():
    """Zero heat must still be reachable — the fix must not conflate 'no risk' with
    'unknown risk'. A trailed winner frees budget by design."""
    pos = stopped(cost=100.0, stop=95.0)
    pos["trailing_stop"] = 120.0
    assert validator._position_committed_risk(pos) == 0.0
    total, bad = validator._sum_committed_risk([pos])
    assert total == 0.0 and bad == []


def test_heat_cap_refuses_when_a_position_is_unverifiable(cfg):
    book = {"total_equity_usd": 10000.0, "cash_usd": 9000.0,
            "positions": [stopless(qty=1.0, cost=50.0)]}
    p = {"ticker": "NVDA", "action": "BUY", "instrument": "EQUITY",
         "position_size_usd": 200, "entry_price_max": 100.0, "stop_loss": 92.0,
         "target_price": 115.0, "holding_horizon_days": 30, "confidence": 0.72,
         "risk_reward_ratio": 2.0, "thesis": "x" * 80,
         "demand_driver": "ai_compute_gpu", "variant_perception": "y" * 120,
         "risk_map": "z" * 60, "macro_context": "m" * 40,
         "catalysts": ["dated catalyst"],
         "scenarios": {"bull": {"price": 115, "prob": 0.3},
                       "base": {"price": 108, "prob": 0.5},
                       "bear": {"price": 92, "prob": 0.2}},
         "thesis_invalidators": {"invalidating_print": "p" * 90,
                                 "invalidating_structure": "s" * 90,
                                 "time_box": "t" * 90}}
    res = validator.validate_proposals([p], book, cfg)[0]
    assert not res.approved
    assert any("heat_unverifiable" in r for r in res.reasons)
    # And the reason must name the offending position so it can be repaired.
    assert any("BBB" in r for r in res.reasons)


# --------------------------------------------------------------------------- #
# 2. Plan backfill merges instead of stripping
# --------------------------------------------------------------------------- #
def test_backfill_preserves_risk_fields(monkeypatch, tmp_path):
    """demand_driver and thesis_invalidators must survive the numeric repair."""
    from execution import simulated_broker as sb

    fill_plan = {"stop_loss": 44.75, "target_price": 60.0,
                 "holding_horizon_days": 30, "entry_price_max": 50.0,
                 "confidence": 0.7,
                 "demand_driver": "hyperscaler_server_capex",
                 "thesis_invalidators": {"invalidating_print": "p",
                                         "invalidating_structure": "s",
                                         "time_box": "t"}}
    state = {"cash_usd": 100.0, "total_equity_usd": 100.0, "history": [],
             "positions": [{"ticker": "HPE", "quantity": 1.0, "avg_cost": 45.0,
                            "market_value_usd": 45.0, "proposal_id": "run-1",
                            "adds_count": 0, "plan": dict(fill_plan)}]}
    state_file = tmp_path / "portfolio.json"
    state_file.write_text(json.dumps(state))
    monkeypatch.setattr(sb, "STATE_FILE", state_file)

    # The journal is authoritative for the NUMERICS and disagrees on the stop.
    monkeypatch.setattr(portfolio_state, "_proposal_numeric_plan",
                        lambda tk, pid: {"stop_loss": 43.75, "target_price": 60.0,
                                         "holding_horizon_days": 30,
                                         "entry_price_max": 50.0, "confidence": 0.7})
    monkeypatch.setattr(portfolio_state, "_journal_plans", lambda: {})

    portfolio_state.backfill_position_plans()

    plan = json.loads(state_file.read_text())["positions"][0]["plan"]
    assert plan["stop_loss"] == 43.75, "numeric repair must still apply"
    assert plan["demand_driver"] == "hyperscaler_server_capex", "risk field stripped"
    assert plan["thesis_invalidators"]["time_box"] == "t", "invalidators stripped"


def test_backfill_is_a_noop_when_numerics_already_agree(monkeypatch, tmp_path):
    """The old whole-dict comparison rewrote the plan on EVERY run because a 7-key
    plan never equals a 5-key one."""
    from execution import simulated_broker as sb

    plan = {"stop_loss": 43.75, "target_price": 60.0, "holding_horizon_days": 30,
            "entry_price_max": 50.0, "confidence": 0.7,
            "demand_driver": "networking",
            "thesis_invalidators": {"invalidating_print": "p"}}
    state = {"cash_usd": 100.0, "total_equity_usd": 100.0, "history": [],
             "positions": [{"ticker": "ANET", "quantity": 1.0, "avg_cost": 45.0,
                            "market_value_usd": 45.0, "proposal_id": "run-1",
                            "adds_count": 0, "plan": dict(plan)}]}
    state_file = tmp_path / "portfolio.json"
    state_file.write_text(json.dumps(state))
    monkeypatch.setattr(sb, "STATE_FILE", state_file)
    monkeypatch.setattr(portfolio_state, "_proposal_numeric_plan",
                        lambda tk, pid: {k: plan[k] for k in
                                         portfolio_state.PLAN_NUMERIC_FIELDS})
    monkeypatch.setattr(portfolio_state, "_journal_plans", lambda: {})

    assert portfolio_state.backfill_position_plans() == 0


def test_preserve_fields_are_declared():
    assert "demand_driver" in portfolio_state.PLAN_PRESERVE_FIELDS
    assert "thesis_invalidators" in portfolio_state.PLAN_PRESERVE_FIELDS
    # They must NOT be in the numeric set, or the comparison breaks again.
    for f in portfolio_state.PLAN_PRESERVE_FIELDS:
        assert f not in portfolio_state.PLAN_NUMERIC_FIELDS


# --------------------------------------------------------------------------- #
# 3. Action normalization
# --------------------------------------------------------------------------- #
def test_lowercase_action_is_normalized_before_the_gates():
    """The validator upper()s everything, so 'buy' validated cleanly and then
    bypassed the daily cap, market-hours gate, entry ceiling, and plan construction."""
    import re
    src = (ROOT / "runlib" / "brain_io.py").read_text()
    loop = src.split("for vr in approved[:max_orders]:", 1)[1]

    # Assign-back to p["action"] from an upper()-ing expression, anywhere in the loop.
    norm = re.search(r'p\["action"\]\s*=\s*str\(.*?\.upper\(\)', loop, re.S)
    assert norm, ("action must be normalized ONCE at the top of the execution loop "
                  "and written back onto the proposal, so downstream branches and "
                  "place_order cannot diverge from the validator's view")

    gate = re.search(r'if p\["action"\] == "BUY"', loop)
    assert gate, "expected the BUY gates to still exist"
    assert norm.start() < gate.start(), (
        "normalization must precede the first BUY gate — otherwise a lowercase "
        "'buy' skips the daily cap, market-hours check, entry ceiling, and plan "
        "construction, leaving a filled position with plan=None that exit_guard "
        "refuses to stop")
