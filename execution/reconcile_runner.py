"""Shared fill-recording for async executions (Actions executor + feeder tick).

When an order fills OUTSIDE a trading run (queued intent executed later, or a
resting sell completing at the next open), the run that placed it never saw
the fill — so journaling + the fill side effects (exit autopsy, shadow-book
close) happen here instead. Used by scripts/execute_order_intents.py and the
local feeder's reconcile pass. Everything is fail-soft: a side-effect failure
never loses the journal line, and a journal failure never breaks reconcile.
"""

from __future__ import annotations

import json
from pathlib import Path

import journal
from execution import broker

ROOT = Path(__file__).resolve().parent.parent


def _already_journaled(client_order_id: str | None) -> bool:
    """True when a trade with this client_order_id is already in the journal.
    Guards against two nodes reconciling the same resting fill (each node has
    its own checkout of the mirror until git syncs them). Scans only the most
    recent trade files — a resting order never outlives a few sessions."""
    if not client_order_id:
        return False
    trades_dir = ROOT / "journal" / "trades"
    try:
        recent = sorted(trades_dir.glob("*.jsonl"))[-4:]
    except Exception:
        return False
    for f in recent:
        try:
            for line in f.read_text().splitlines():
                if client_order_id in line:
                    rec = json.loads(line)
                    if (rec.get("order", {}).get("client_order_id") == client_order_id
                            or rec.get("fill", {}).get("client_order_id") == client_order_id):
                        return True
        except Exception:
            continue
    return False


def record_fill(order: dict, fill: dict) -> None:
    """Journal one async fill + replicate brain_io.execute()'s side effects."""
    if _already_journaled(order.get("client_order_id")):
        print(f"  (fill {order.get('client_order_id')} already journaled — skipped)")
        return
    run_id = str(order.get("proposal_id") or "async-executor")
    journal.log_trade(order, fill, run_id)
    action = str(fill.get("action", "")).upper()
    if action == "BUY":
        try:
            from tools.shadow_portfolio import close_shadow_if_bought
            close_shadow_if_bought(fill.get("ticker"), fill.get("fill_price"))
        except Exception:
            pass
    elif action == "SELL_TO_CLOSE":
        try:
            from tools.exit_autopsy import (
                build_exit_autopsy_from_fill, grade_and_persist_autopsy,
            )
            # The fill carries its own entry facts (avg_cost / opened_at /
            # entry_plan), so a synthetic pre-position is enough for grading.
            pos_before = {
                "ticker": fill.get("ticker"),
                "avg_cost": fill.get("avg_cost"),
                "opened_at": fill.get("position_opened_at"),
                "plan": fill.get("entry_plan"),
                "quantity": fill.get("quantity"),
                "proposal_id": fill.get("entry_proposal_id"),
            }
            forced = bool(order.get("forced_exit_reason"))
            reason = str(order.get("forced_exit_reason")
                         or "async_executor_fill")[:300]
            rec = build_exit_autopsy_from_fill(fill, order, pos_before,
                                               forced=forced, reason=reason)
            grade_and_persist_autopsy(rec)
        except Exception as e:
            print(f"  (exit autopsy skipped: {e})")
    print(f"  ASYNC FILL {action} {fill.get('ticker')} "
          f"{fill.get('quantity')} @ {fill.get('fill_price')}")


def run_reconcile() -> list[dict]:
    """Complete pending broker orders and journal every resulting fill.
    Returns the fills. No-op (empty list) on simulation / unreachable API."""
    fills = []
    try:
        for order, fill in broker.reconcile():
            try:
                record_fill(order, fill)
            except Exception as e:
                print(f"  (reconcile journaling failed: {e})")
            fills.append(fill)
    except Exception as e:
        print(f"  (reconcile failed: {e})")
    return fills
