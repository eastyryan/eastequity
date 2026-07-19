"""The chain, not the links: brain JSON -> parse -> validate -> execute -> ledger.

THE GAP THIS CLOSES. Before 2026-07-19 the suite had ~535 tests and NOT ONE drove a
proposal through the whole pipeline. Every link was tested in isolation; the chain
was not. A field renamed in transit, or an approved proposal that never reached
place_order, was invisible to the entire suite.

That is not hypothetical. The repo already contains a source-grep test
(test_risk_fail_closed.py) that asserts one line of brain_io.py appears BEFORE
another, because a lowercase `"buy"` once bypassed the daily cap, the market-hours
gate and plan construction — producing a permanently unstopped position. That bug
was caught by regex over source text, which passes just as well if the code is
refactored into a helper. A behavioural test of the chain catches it either way.

Everything here is offline: the broker is the simulation backend against a tmp
ledger, and no network call is made.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator  # noqa: E402
from execution import simulated_broker as sb  # noqa: E402
from runlib.brain_io import parse_proposals  # noqa: E402
from tools.demand_drivers import driver_for_ticker  # noqa: E402


BRAIN_RESPONSE = """
Some prose the model wrote before its JSON block.

```json
{
  "proposals": [
    {
      "ticker": "NVDA",
      "action": "BUY",
      "instrument": "EQUITY",
      "position_size_usd": 900,
      "entry_price_max": 100.0,
      "stop_loss": 92.0,
      "target_price": 120.0,
      "holding_horizon_days": 30,
      "confidence": 0.72,
      "risk_reward_ratio": 2.5,
      "thesis": "Clean base breakout with rising estimates and a dated catalyst.",
      "macro_context": "Supportive tape; leadership intact.",
      "catalysts": ["Earnings 2026-08-20"],
      "risk_map": "Guide cut or a close below the 50-DMA on volume kills it.",
      "scenarios": {"bull": {"price": 130, "prob": 0.3},
                    "base": {"price": 120, "prob": 0.5},
                    "bear": {"price": 90, "prob": 0.2}},
      "variant_perception": "Consensus models a flat multiple; I see mispriced growth because estimates lag the order book; resolves at the August print.",
      "demand_driver": "ai_compute_gpu",
      "thesis_invalidators": {
        "invalidating_print": "EPS guide cut or estimate cuts greater than 5 percent",
        "invalidating_structure": "Close below the 50-DMA on rising volume",
        "time_box": "If no progress toward thesis in 25 trading days, exit"
      }
    }
  ],
  "commentary": "Adding one position on a clean setup.",
  "watchlist": [{"ticker": "AMD", "one_line": "Next in line.",
                 "thoughts": "Waiting on a base.", "would_buy_at": "near 95",
                 "status": "hold"}]
}
```
"""


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A funded, empty book on the simulation backend."""
    state_file = tmp_path / "portfolio.json"
    state_file.write_text(json.dumps({
        "cash_usd": 10_000.0, "total_equity_usd": 10_000.0,
        "positions": [], "history": [], "pending_orders": {},
    }))
    monkeypatch.setattr(sb, "STATE_FILE", state_file)
    return state_file


def market_context(ticker="NVDA", atr_pct=3.0):
    return {ticker: {"atr_pct": atr_pct, "expected_move_pct": 5.0,
                     "market_cap_usd": 2_000_000_000_000}}


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def place_like_brain_io(proposal, ref=98.0, run_id="TEST-E2E"):
    """Place an order exactly the way runlib/brain_io.execute does.

    Mirrors brain_io.py:535-552 — normalize the action, build the numeric plan from
    the proposal, and hand BOTH to the broker. Calling sb.place_order with a bare
    proposal is NOT the production path and silently omits the plan.
    """
    p = dict(proposal)
    p["action"] = str(p.get("action", "")).strip().upper()
    plan = None
    if p["action"] == "BUY":
        plan = {k: p.get(k) for k in (
            "stop_loss", "target_price", "holding_horizon_days",
            "entry_price_max", "confidence", "demand_driver",
            "thesis_invalidators")}
    return sb.place_order({
        "ticker": p["ticker"], "action": p["action"],
        "position_size_usd": p.get("position_size_usd"),
        "reference_price": ref, "proposal_id": run_id,
        "sell_fraction": p.get("sell_fraction"),
        "plan": plan, "demand_driver": p.get("demand_driver"),
    })


def test_a_clean_proposal_survives_the_whole_pipeline(ledger):
    """parse -> validate -> place -> readback -> ledger, asserting at every hop."""
    parsed = parse_proposals(BRAIN_RESPONSE)
    proposals = parsed["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["ticker"] == "NVDA"

    portfolio = json.loads(ledger.read_text())
    results = validator.validate_proposals(proposals, portfolio, market_context())
    assert results[0].approved, results[0].reasons

    order = place_like_brain_io(results[0].proposal)
    fill = sb.readback(order["order_id"])
    assert fill and fill.get("status") == "filled", fill

    state = json.loads(ledger.read_text())
    assert len(state["positions"]) == 1
    pos = state["positions"][0]
    assert pos["ticker"] == "NVDA"
    assert pos["quantity"] > 0
    assert state["cash_usd"] < 10_000.0


def test_the_filled_position_carries_a_complete_enforceable_plan(ledger):
    """A position whose plan did not survive execution is an UNSTOPPED position.

    This is the assertion that matters most in this file. exit_guard.check_forced_exits
    does `plan = pos.get("original_plan"); if not plan: continue` — it will not stop a
    position it cannot read a plan for. So a plan lost in transit produces a live
    position that no safety layer will ever exit, silently.
    """
    parsed = parse_proposals(BRAIN_RESPONSE)
    portfolio = json.loads(ledger.read_text())
    results = validator.validate_proposals(parsed["proposals"], portfolio,
                                           market_context())
    order = place_like_brain_io(results[0].proposal)
    sb.readback(order["order_id"])

    pos = json.loads(ledger.read_text())["positions"][0]
    plan = pos.get("plan") or {}
    for field in ("stop_loss", "target_price", "holding_horizon_days"):
        assert plan.get(field) is not None, (field, plan)
    assert plan["stop_loss"] < plan["target_price"]
    assert pos.get("high_water") is not None       # trail needs a seed


def test_a_plan_less_position_blocks_all_new_buys(ledger):
    """The real protection against an unstoppable position, asserted end to end.

    A plan-less position is legitimate at the broker layer (the journal can supply
    the plan later via portfolio_state.backfill_position_plans), so it is NOT
    rejected on placement. What must hold is that while one exists and cannot be
    resolved, the book takes on no NEW risk: _check_portfolio_heat fails closed
    because true committed risk is unknowable. exit_guard separately warns, since it
    will not stop that position on any run.
    """
    parsed = parse_proposals(BRAIN_RESPONSE)
    portfolio = json.loads(ledger.read_text())
    portfolio["positions"] = [{
        "ticker": "ORPHAN", "quantity": 10.0, "avg_cost": 50.0,
        "market_value_usd": 500.0, "last_price": 50.0,
        "opened_at": "2026-07-01T14:00:00+00:00",
    }]  # no plan, no stop, no trailing_stop

    results = validator.validate_proposals(parsed["proposals"], portfolio,
                                           market_context())
    assert not results[0].approved
    assert any("portfolio_heat_unverifiable" in r for r in results[0].reasons), \
        results[0].reasons


def test_exit_guard_announces_a_position_it_cannot_stop(ledger, capsys):
    """It used to `continue` with no output at all, so the condition was invisible."""
    from execution import exit_guard
    exit_guard.check_forced_exits(
        {"positions": [{"ticker": "ORPHAN", "quantity": 10.0, "avg_cost": 50.0}]},
        {"ORPHAN": 40.0})
    assert "UNSTOPPABLE POSITION ORPHAN" in capsys.readouterr().out


def test_risk_sizing_clamp_reaches_the_broker_not_just_the_result(ledger):
    """The clamp mutates the proposal in place; the ORDER must carry the clamped size.

    An oversized proposal that is clamped in validation and then placed at its
    ORIGINAL size would breach the risk budget while every validation log says it
    did not.
    """
    parsed = parse_proposals(BRAIN_RESPONSE)
    oversized = dict(parsed["proposals"][0])
    oversized["position_size_usd"] = 5_000.0      # 8% stop -> $400 risk, way over 1%
    portfolio = json.loads(ledger.read_text())

    results = validator.validate_proposals([oversized], portfolio, market_context())
    assert results[0].approved, results[0].reasons
    clamped = results[0].proposal["position_size_usd"]
    assert clamped < 5_000.0

    order = place_like_brain_io(results[0].proposal, run_id="TEST-CLAMP")
    sb.readback(order["order_id"])
    state = json.loads(ledger.read_text())
    spent = 10_000.0 - state["cash_usd"]
    assert spent <= clamped + 1.0, (spent, clamped)


def test_a_rejected_proposal_never_reaches_the_ledger(ledger):
    """The mirror. Without it, 'approve everything' passes every test above."""
    parsed = parse_proposals(BRAIN_RESPONSE)
    bad = dict(parsed["proposals"][0])
    bad["demand_driver"] = "healthcare"           # NVDA is canonically ai_compute_gpu

    portfolio = json.loads(ledger.read_text())
    results = validator.validate_proposals([bad], portfolio, market_context())
    assert not results[0].approved
    assert any("demand_driver_mismatch" in r for r in results[0].reasons)

    state = json.loads(ledger.read_text())
    assert state["positions"] == []
    assert state["cash_usd"] == 10_000.0


def test_lowercase_action_cannot_produce_an_unstopped_position(ledger):
    """Behavioural version of the source-grep test in test_risk_fail_closed.py.

    A lowercase "buy" once validated cleanly (validator upper()s every comparison)
    and then bypassed the daily cap, the market-hours gate AND plan construction —
    leaving a filled position with plan=None that exit_guard skips outright,
    permanently unstopped and contributing zero portfolio heat.

    The existing regression test greps brain_io.py source and asserts one line
    appears before another, which passes just as well if the code is refactored into
    a helper. This asserts the OUTCOME instead: whatever the pipeline does with a
    lowercase action, the ledger must never end up holding a position with no stop.
    """
    parsed = parse_proposals(BRAIN_RESPONSE)
    lower = dict(parsed["proposals"][0])
    lower["action"] = "buy"

    portfolio = json.loads(ledger.read_text())
    results = validator.validate_proposals([lower], portfolio, market_context())
    if results[0].approved:
        order = place_like_brain_io(results[0].proposal, run_id="TEST-LOWER")
        if order.get("status") == "pending":
            sb.readback(order["order_id"])

    for pos in json.loads(ledger.read_text())["positions"]:
        assert (pos.get("plan") or {}).get("stop_loss") is not None, pos


def test_the_canonical_driver_in_the_fixture_is_actually_canonical():
    """Guards the fixture itself: if NVDA's mapping changes, these tests should fail
    loudly rather than silently start exercising the mismatch path."""
    assert driver_for_ticker("NVDA") == "ai_compute_gpu"
