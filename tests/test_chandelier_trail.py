"""Offline tests for the chandelier trailing stop (high_water tracking in the
broker + ratcheted trail in exit_guard + effective-stop cushion in analytics).

    python3 -m pytest tests/test_chandelier_trail.py -q

Spec under test (research-approved):
  * chandelier = high-water mark since entry − N × ATR$ (ATR% of current price),
    N from autonomy_config trade_quality_requirements.trailing_stop_atr_multiple,
    default 3.0;
  * the trail activates ONLY once the chandelier exceeds the plan stop (no
    breakeven jump at +1R), and from then on only ever RATCHETS UP;
  * effective stop = max(plan stop_loss, trailing_stop) — never widened;
  * missing high_water / ATR -> plan-stop-only behaviour, exactly as before;
  * gap-through fill modeling applies to trailing exits like plan-stop exits.

Ledger isolation follows tests/test_exit_autopsy.py / conftest: the broker's
STATE_FILE is pointed at a tmp_path copy for every test, never the real book.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import exit_guard  # noqa: E402
from execution import simulated_broker as SB  # noqa: E402

COSTS = dict(SB._COST_DEFAULTS)  # flat 10bps slippage, no fees, no gap modeling


@pytest.fixture
def tmp_book(tmp_path, monkeypatch):
    """Empty paper book in a temp ledger; deterministic execution costs."""
    state = tmp_path / "portfolio.json"
    state.write_text(json.dumps({
        "cash_usd": 10000.0, "total_equity_usd": 10000.0,
        "positions": [], "pending_orders": {}, "history": [],
    }))
    monkeypatch.setattr(SB, "STATE_FILE", state)
    monkeypatch.setattr(SB, "_execution_costs", lambda: dict(COSTS))
    return state


def buy(ticker="NVDA", ref=100.0, qty=10):
    order = SB.place_order({"ticker": ticker, "action": "BUY", "quantity": qty,
                            "reference_price": ref, "proposal_id": "test-run"})
    fill = SB.readback(order["order_id"])
    assert fill["status"] == "filled"
    return fill


def ledger_pos(ticker="NVDA"):
    state = json.loads(SB.STATE_FILE.read_text())
    return next((p for p in state["positions"] if p["ticker"] == ticker), None)


def seed_position(ticker="NVDA", **extra):
    """Write a position straight into the temp ledger (bypasses fills)."""
    state = json.loads(SB.STATE_FILE.read_text())
    pos = {"ticker": ticker, "quantity": 10.0, "avg_cost": 100.0,
           "market_value_usd": 1000.0, **extra}
    state["positions"].append(pos)
    SB.STATE_FILE.write_text(json.dumps(state))
    return pos


def portfolio_with(ticker="NVDA", stop=90.0, horizon=60, days_held=5, **pos_extra):
    """A get_portfolio_state()-shaped dict: position + original_plan attached."""
    pos = {"ticker": ticker, "quantity": 10.0, "avg_cost": 100.0,
           "days_held": days_held,
           "original_plan": {"stop_loss": stop, "target_price": 130.0,
                             "holding_horizon_days": horizon},
           **pos_extra}
    return {"positions": [pos]}, pos


# ---------------------------------------------------------------------------
# Broker: high_water lifecycle
# ---------------------------------------------------------------------------

def test_high_water_initialized_at_entry_fill(tmp_book):
    fill = buy(ref=100.0)
    pos = ledger_pos()
    assert pos["high_water"] == fill["fill_price"]  # entry fill IS the first high
    assert pos["high_water"] == pytest.approx(100.1)  # ref * (1 + 10bps)


def test_mark_to_market_ratchets_high_water_up_never_down(tmp_book):
    buy(ref=100.0)
    SB.mark_to_market({"NVDA": 120.0})
    assert ledger_pos()["high_water"] == 120.0
    SB.mark_to_market({"NVDA": 110.0})  # lower mark must NOT pull it down
    pos = ledger_pos()
    assert pos["high_water"] == 120.0
    assert pos["last_price"] == 110.0  # marks themselves still update


def test_mark_to_market_seeds_legacy_position_missing_high_water(tmp_book):
    seed_position(last_price=100.0)  # pre-trail position: no high_water key
    SB.mark_to_market({"NVDA": 105.0})
    assert ledger_pos()["high_water"] == 105.0


def test_scale_in_add_keeps_max_high_water(tmp_book):
    buy(ref=100.0)
    SB.mark_to_market({"NVDA": 130.0})  # ran to 130 first
    buy(ref=125.0)                       # add on the pullback
    pos = ledger_pos()
    assert pos["adds_count"] == 1
    assert pos["high_water"] == 130.0    # add below the high never lowers it

    buy(ref=140.0)                       # add ABOVE the high raises it
    assert ledger_pos()["high_water"] == pytest.approx(140.0 * 1.001)


def test_high_water_survives_partial_sell(tmp_book):
    buy(ref=100.0, qty=10)
    SB.mark_to_market({"NVDA": 130.0})
    order = SB.place_order({"ticker": "NVDA", "action": "SELL_TO_CLOSE",
                            "reference_price": 128.0, "sell_fraction": 0.5,
                            "proposal_id": "test-run"})
    fill = SB.readback(order["order_id"])
    assert fill["status"] == "filled" and fill["sell_fraction"] == 0.5
    pos = ledger_pos()
    assert pos is not None and pos["quantity"] == 5.0
    assert pos["high_water"] == 130.0    # trail anchor untouched by the partial


def test_buy_fill_accounting_unchanged_by_high_water(tmp_book):
    """high_water is bookkeeping only — cost basis / cash math is untouched."""
    fill = buy(ref=100.0, qty=10)
    assert fill["fill_price"] == pytest.approx(100.1)
    assert fill["position_avg_cost_after"] == pytest.approx(100.1)
    state = json.loads(SB.STATE_FILE.read_text())
    assert state["cash_usd"] == pytest.approx(10000.0 - 1001.0)


# ---------------------------------------------------------------------------
# Broker: update_trailing_stops (the single persistence path)
# ---------------------------------------------------------------------------

def test_update_trailing_stops_ratchet_only(tmp_book):
    seed_position(trailing_stop=105.0)
    assert SB.update_trailing_stops({"NVDA": 100.0}) == 0   # lower -> refused
    assert ledger_pos()["trailing_stop"] == 105.0
    assert SB.update_trailing_stops({"NVDA": 107.5}) == 1   # higher -> ratchets
    assert ledger_pos()["trailing_stop"] == 107.5


def test_update_trailing_stops_ignores_unknown_and_garbage(tmp_book):
    seed_position()
    assert SB.update_trailing_stops({"ZZZZ": 50.0}) == 0    # not held
    assert SB.update_trailing_stops({"NVDA": "junk"}) == 0  # unparseable
    assert SB.update_trailing_stops({}) == 0
    assert "trailing_stop" not in ledger_pos()


# ---------------------------------------------------------------------------
# exit_guard: activation gate, ratchet, effective-stop breach
# ---------------------------------------------------------------------------

def test_no_trail_until_chandelier_exceeds_plan_stop(tmp_book):
    seed_position()
    # chandelier = 100 - 3 x 5% x 100 = 85 < plan stop 90 -> plan stop governs
    portfolio, pos = portfolio_with(stop=90.0, high_water=100.0)
    exits = exit_guard.check_forced_exits(portfolio, {"NVDA": 100.0}, {"NVDA": 5.0})
    assert exits == []
    assert "trailing_stop" not in pos              # not set in-memory
    assert "trailing_stop" not in ledger_pos()     # not persisted either


def test_plan_stop_breach_still_fires_as_before(tmp_book):
    seed_position()
    portfolio, _ = portfolio_with(stop=90.0, high_water=100.0)
    exits = exit_guard.check_forced_exits(portfolio, {"NVDA": 89.0}, {"NVDA": 5.0})
    assert len(exits) == 1
    ex = exits[0]
    assert ex["reason"] == "stop_loss_breached"    # trail inactive -> plan stop
    assert ex["stop_loss"] == 90.0
    assert ex["plan_stop_loss"] == 90.0
    assert ex["trailing_stop"] is None


def test_trail_activates_and_persists_once_above_plan_stop(tmp_book):
    seed_position()
    # chandelier = 120 - 3 x 5% x 110 = 103.5 > plan stop 90 -> trail activates
    portfolio, pos = portfolio_with(stop=90.0, high_water=120.0)
    exits = exit_guard.check_forced_exits(portfolio, {"NVDA": 110.0}, {"NVDA": 5.0})
    assert exits == []                              # 110 > 103.5, no breach
    assert pos["trailing_stop"] == pytest.approx(103.5)
    assert ledger_pos()["trailing_stop"] == pytest.approx(103.5)  # broker-persisted


def test_trail_ratchets_up_only(tmp_book):
    seed_position(trailing_stop=103.5)
    # Same high, higher price -> chandelier 120 - 3 x 5% x 118 = 102.3 < 103.5:
    # the stored trail must NOT come down.
    portfolio, pos = portfolio_with(stop=90.0, high_water=120.0, trailing_stop=103.5)
    exit_guard.check_forced_exits(portfolio, {"NVDA": 118.0}, {"NVDA": 5.0})
    assert pos["trailing_stop"] == pytest.approx(103.5)
    assert ledger_pos()["trailing_stop"] == pytest.approx(103.5)

    # New high 125 -> chandelier 125 - 17.7 = 107.3 > 103.5: ratchets up.
    portfolio, pos = portfolio_with(stop=90.0, high_water=125.0, trailing_stop=103.5)
    exit_guard.check_forced_exits(portfolio, {"NVDA": 118.0}, {"NVDA": 5.0})
    assert pos["trailing_stop"] == pytest.approx(107.3)
    assert ledger_pos()["trailing_stop"] == pytest.approx(107.3)


def test_trail_never_stored_below_plan_stop(tmp_book):
    seed_position()
    # chandelier 100 - 3 x 2% x 100 = 94 > 90: stored, and it exceeds the plan stop.
    portfolio, pos = portfolio_with(stop=90.0, high_water=100.0)
    exit_guard.check_forced_exits(portfolio, {"NVDA": 100.0}, {"NVDA": 2.0})
    assert pos["trailing_stop"] == pytest.approx(94.0)
    assert pos["trailing_stop"] > 90.0
    # Even a direct persistence attempt below an existing trail is refused
    # (ratchet lives in the broker too, not just in exit_guard).
    assert SB.update_trailing_stops({"NVDA": 89.0}) == 0
    assert ledger_pos()["trailing_stop"] == pytest.approx(94.0)


def test_breach_fires_on_effective_stop_not_plan_stop(tmp_book):
    seed_position(trailing_stop=105.0)
    # last 104 sits ABOVE the plan stop (90) but BELOW the trail (105).
    portfolio, _ = portfolio_with(stop=90.0, high_water=120.0, trailing_stop=105.0)
    exits = exit_guard.check_forced_exits(portfolio, {"NVDA": 104.0}, {"NVDA": 5.0})
    assert len(exits) == 1
    ex = exits[0]
    assert ex["reason"] == "trailing_stop_breached"  # the trail was binding
    assert ex["stop_loss"] == pytest.approx(105.0)   # broker gaps below THIS level
    assert ex["plan_stop_loss"] == 90.0
    assert ex["trailing_stop"] == pytest.approx(105.0)


def test_no_breach_above_effective_stop(tmp_book):
    seed_position(trailing_stop=105.0)
    portfolio, _ = portfolio_with(stop=90.0, high_water=120.0, trailing_stop=105.0)
    exits = exit_guard.check_forced_exits(portfolio, {"NVDA": 106.0}, {"NVDA": 5.0})
    assert exits == []


def test_fail_soft_missing_high_water_or_atr(tmp_book):
    seed_position()
    # No high_water -> plan stop only (no trail, no crash).
    portfolio, pos = portfolio_with(stop=90.0)
    assert exit_guard.check_forced_exits(portfolio, {"NVDA": 100.0}, {"NVDA": 5.0}) == []
    assert "trailing_stop" not in pos
    # high_water present, no ATR -> plan stop only.
    portfolio, pos = portfolio_with(stop=90.0, high_water=120.0)
    assert exit_guard.check_forced_exits(portfolio, {"NVDA": 100.0}, {}) == []
    assert "trailing_stop" not in pos
    # Garbage high_water -> plan stop only.
    portfolio, pos = portfolio_with(stop=90.0, high_water="junk")
    assert exit_guard.check_forced_exits(portfolio, {"NVDA": 100.0}, {"NVDA": 5.0}) == []
    assert "trailing_stop" not in pos
    # And the plan stop still enforces through all three shapes.
    portfolio, _ = portfolio_with(stop=90.0)
    exits = exit_guard.check_forced_exits(portfolio, {"NVDA": 89.0}, {})
    assert exits and exits[0]["reason"] == "stop_loss_breached"


def test_horizon_expiry_unaffected(tmp_book):
    seed_position()
    portfolio, _ = portfolio_with(stop=90.0, horizon=30, days_held=31, high_water=120.0)
    exits = exit_guard.check_forced_exits(portfolio, {"NVDA": 118.0}, {"NVDA": 5.0})
    assert len(exits) == 1 and exits[0]["reason"] == "horizon_expired"


def test_atr_multiple_config_key_if_present_else_3(tmp_book):
    # Contract: trade_quality_requirements.trailing_stop_atr_multiple wins when
    # present; otherwise the hard-coded 3.0 default (config never edited here).
    cfg = json.loads((SB.ROOT / "autonomy_config.json").read_text())
    key = (cfg.get("trade_quality_requirements") or {}).get("trailing_stop_atr_multiple")
    expected = float(key) if key is not None else 3.0
    assert exit_guard._trailing_atr_multiple() == expected
    assert exit_guard.TRAILING_STOP_ATR_MULTIPLE_DEFAULT == 3.0
    # The multiple parameter flows through the chandelier math.
    assert exit_guard._chandelier_stop(120.0, 5.0, 100.0, 2.0) == pytest.approx(110.0)
    assert exit_guard._chandelier_stop(120.0, 5.0, 100.0, 3.0) == pytest.approx(105.0)


# ---------------------------------------------------------------------------
# Gap-through fill modeling (unchanged for plan stops, applied to trail exits)
# ---------------------------------------------------------------------------

def test_gap_through_applies_to_trailing_stop_exits():
    costs = {**COSTS, "model_stop_gaps": True, "stop_gap_atr_fraction": 0.5}
    price, gapped = SB._sell_fill_price(
        {"forced_exit_reason": "trailing_stop_breached",
         "stop_loss": 105.0, "atr_pct": 5.0}, 104.0, 0.001, costs)
    assert gapped is True
    # gap below the TRAIL: 105 - 0.5 x 5% x 104 = 102.4, then sell slippage
    assert price == pytest.approx(round(102.4 * 0.999, 4))


def test_gap_through_unchanged_for_plan_stop_exits():
    costs = {**COSTS, "model_stop_gaps": True, "stop_gap_atr_fraction": 0.5}
    price, gapped = SB._sell_fill_price(
        {"forced_exit_reason": "stop_loss_breached",
         "stop_loss": 90.0, "atr_pct": 5.0}, 89.0, 0.001, costs)
    assert gapped is True
    assert price == pytest.approx(round((90.0 - 0.5 * 0.05 * 89.0) * 0.999, 4))
    # Non-stop reasons still never gap.
    price, gapped = SB._sell_fill_price(
        {"forced_exit_reason": "horizon_expired",
         "stop_loss": 90.0, "atr_pct": 5.0}, 118.0, 0.001, costs)
    assert gapped is False
    assert price == pytest.approx(round(118.0 * 0.999, 4))


# ---------------------------------------------------------------------------
# analytics: cushion measured against the EFFECTIVE stop
# ---------------------------------------------------------------------------

def test_stop_cushion_uses_effective_stop():
    from runlib.analytics import build_position_stop_cushion
    pos = {"ticker": "NVDA", "last_price": 110.0, "avg_cost": 100.0,
           "days_held": 5, "trailing_stop": 105.0,
           "original_plan": {"stop_loss": 90.0, "entry_price_max": 100.0}}
    out = build_position_stop_cushion({"positions": [pos]}, {"NVDA": {"atr_pct": 5.0}}, {})
    info = out["NVDA"]
    assert info["recorded_stop"] == 90.0            # plan stop still shown
    assert info["trailing_stop"] == 105.0
    assert info["effective_stop"] == 105.0
    # Cushion vs the trail, NOT the plan stop: (110-105)/110 = 4.55%.
    assert info["cushion_to_stop_pct"] == pytest.approx(4.55, abs=0.01)
    assert info["cushion_in_atr"] == pytest.approx(0.91, abs=0.01)
    assert info["inside_noise_band"] is True        # < 1 ATR of room to the trail
    # Stop above entry -> negative distance (profit locked in), not an error.
    assert info["stop_distance_from_entry_pct"] == pytest.approx(-5.0)


def test_stop_cushion_shape_unchanged_without_trail():
    from runlib.analytics import build_position_stop_cushion
    pos = {"ticker": "NVDA", "last_price": 110.0, "avg_cost": 100.0,
           "days_held": 5,
           "original_plan": {"stop_loss": 90.0, "entry_price_max": 100.0}}
    out = build_position_stop_cushion({"positions": [pos]}, {"NVDA": {"atr_pct": 5.0}}, {})
    info = out["NVDA"]
    assert "trailing_stop" not in info              # no additive keys w/o a trail
    assert "effective_stop" not in info
    assert info["recorded_stop"] == 90.0
    assert info["cushion_to_stop_pct"] == pytest.approx((110 - 90) / 110 * 100, abs=0.01)
    assert info["stop_distance_from_entry_pct"] == pytest.approx(10.0)
