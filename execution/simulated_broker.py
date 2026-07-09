"""Simulated broker — the default (and safest) execution backend.

Maintains a paper portfolio in state/portfolio.json. Fills BUY/SELL_TO_CLOSE
orders at the provided reference price (last close from the scanner) with a
small slippage haircut, and supports the same readback interface the real
Moomoo/IBKR adapters will implement later:

    place_order(order) -> pending order dict
    readback(order_id) -> fill confirmation dict (or None)

The orchestrator must call readback() and verify the fill before journaling a
trade as executed — same contract as live trading, so nothing changes when we
swap the backend.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state" / "portfolio.json"
SLIPPAGE = 0.001  # 10 bps assumed slippage on paper fills


def _load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    cfg = json.loads((ROOT / "autonomy_config.json").read_text())
    return {
        "cash_usd": cfg["position_sizing"]["starting_capital_usd"],
        "total_equity_usd": cfg["position_sizing"]["starting_capital_usd"],
        "positions": [],
        "pending_orders": {},
        "history": [],
    }


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def get_portfolio() -> dict:
    return _load()


def place_order(order: dict) -> dict:
    """order: {ticker, action, position_size_usd|quantity, reference_price, proposal_id}"""
    state = _load()
    order_id = f"SIM-{uuid.uuid4().hex[:10]}"
    order = {**order, "order_id": order_id, "status": "pending",
             "submitted_at": datetime.now(timezone.utc).isoformat()}
    state["pending_orders"][order_id] = order
    _save(state)
    return order


def readback(order_id: str) -> dict | None:
    """Fill the pending order and return the broker's confirmation."""
    state = _load()
    order = state["pending_orders"].pop(order_id, None)
    if order is None:
        return None
    ref = float(order["reference_price"])
    action = order["action"].upper()

    if action == "BUY":
        fill_price = round(ref * (1 + SLIPPAGE), 4)
        qty = order.get("quantity") or round(float(order["position_size_usd"]) / fill_price, 4)
        cost = round(qty * fill_price, 2)
        if cost > state["cash_usd"]:
            fill = {**order, "status": "rejected_insufficient_cash"}
        else:
            state["cash_usd"] = round(state["cash_usd"] - cost, 2)
            state["positions"].append({
                "ticker": order["ticker"].upper(), "quantity": qty,
                "avg_cost": fill_price, "market_value_usd": cost,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "proposal_id": order.get("proposal_id"),
            })
            fill = {**order, "status": "filled", "fill_price": fill_price,
                    "quantity": qty, "notional_usd": cost}
    elif action == "SELL_TO_CLOSE":
        pos = next((p for p in state["positions"] if p["ticker"] == order["ticker"].upper()), None)
        if pos is None:
            fill = {**order, "status": "rejected_no_position"}
        else:
            fill_price = round(ref * (1 - SLIPPAGE), 4)
            proceeds = round(pos["quantity"] * fill_price, 2)
            state["cash_usd"] = round(state["cash_usd"] + proceeds, 2)
            state["positions"].remove(pos)
            pnl = round(proceeds - pos["quantity"] * pos["avg_cost"], 2)
            fill = {**order, "status": "filled", "fill_price": fill_price,
                    "quantity": pos["quantity"], "notional_usd": proceeds,
                    "realized_pnl_usd": pnl}
    else:
        fill = {**order, "status": "rejected_unsupported_action"}

    fill["filled_at"] = datetime.now(timezone.utc).isoformat()
    state["history"].append(fill)
    state["total_equity_usd"] = round(
        state["cash_usd"] + sum(p["market_value_usd"] for p in state["positions"]), 2)
    _save(state)
    return fill


def mark_to_market(prices: dict[str, float]) -> dict:
    """Update market values from {ticker: last_price} and return the portfolio."""
    state = _load()
    for pos in state["positions"]:
        px = prices.get(pos["ticker"])
        if px:
            pos["market_value_usd"] = round(pos["quantity"] * px, 2)
            pos["last_price"] = px
    state["total_equity_usd"] = round(
        state["cash_usd"] + sum(p["market_value_usd"] for p in state["positions"]), 2)
    _save(state)
    return state
