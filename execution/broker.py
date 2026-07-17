"""Broker router — every execution call site goes through here.

autonomy_config.json -> mode.broker selects the backend:
    "simulation"   -> execution.simulated_broker (JSON-ledger paper sim)
    "alpaca_paper" -> execution.alpaca_broker    (real Alpaca paper account)

Both backends expose the identical contract (place_order / readback /
get_portfolio / mark_to_market / update_trailing_stops) and write the same
state/portfolio.json shape, so flipping the config key is the entire
migration — and the instant rollback."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_KNOWN = ("simulation", "alpaca_paper")


def backend_name() -> str:
    # EE_BROKER env overrides config: the test suite pins "simulation" so
    # seeded-state tests can never dispatch to the real API, and ops can
    # force a backend for a one-off run without editing config.
    env = str(os.environ.get("EE_BROKER", "")).strip().lower()
    if env in _KNOWN:
        return env
    try:
        cfg = json.loads((ROOT / "autonomy_config.json").read_text())
        name = str(cfg["mode"]["broker"]).strip().lower()
    except Exception:
        return "simulation"
    return name if name in _KNOWN else "simulation"


def _backend():
    if backend_name() == "alpaca_paper":
        from execution import alpaca_broker
        return alpaca_broker
    from execution import simulated_broker
    return simulated_broker


def place_order(order: dict) -> dict:
    return _backend().place_order(order)


def readback(order_id: str):
    return _backend().readback(order_id)


def get_portfolio() -> dict:
    return _backend().get_portfolio()


def mark_to_market(prices: dict) -> dict:
    return _backend().mark_to_market(prices)


def update_trailing_stops(trailing: dict) -> int:
    return _backend().update_trailing_stops(trailing)


def reconcile() -> list:
    """Complete broker-side pending orders (async fills). Simulation has no
    async lifecycle — returns []."""
    b = _backend()
    return b.reconcile() if hasattr(b, "reconcile") else []


# Order/readback statuses that mean "handed to the cloud->Actions executor,
# not yet executed" — call sites journal these as intents, not fills/rejections.
QUEUED_STATUSES = ("queued_intent",)
RESTING_STATUSES = ("resting_market_closed", "resting_awaiting_fill")
