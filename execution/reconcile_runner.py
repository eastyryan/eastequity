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


_ID_FIELDS = ("client_order_id", "order_id", "alpaca_order_id")


def _already_journaled(client_order_id: str | None,
                       alpaca_order_id: str | None = None) -> bool:
    """True when a trade carrying either id is already in the journal.

    Guards against two nodes reconciling the same resting fill (each node has
    its own checkout of the mirror until git syncs them, so the journal is the
    shared record). Scans only the most recent trade files — a resting order
    never outlives a few sessions.

    The Alpaca order id counts as a match because an OUT-OF-BAND fill is
    discovered from the broker's ORDER LIST, so it can arrive carrying a client
    id this system never issued; the broker's own id is then the only handle
    both nodes agree on."""
    ids = [str(i) for i in (client_order_id, alpaca_order_id) if i]
    if not ids:
        return False
    trades_dir = ROOT / "journal" / "trades"
    try:
        recent = sorted(trades_dir.glob("*.jsonl"))[-4:]
    except Exception:
        return False
    for f in recent:
        try:
            for line in f.read_text().splitlines():
                if not any(i in line for i in ids):
                    continue
                rec = json.loads(line)
                for blob in (rec.get("order"), rec.get("fill")):
                    if not isinstance(blob, dict):
                        continue
                    for k in _ID_FIELDS:
                        if blob.get(k) and str(blob[k]) in ids:
                            return True
        except Exception:
            continue
    return False


def record_fill(order: dict, fill: dict) -> None:
    """Journal one async fill + replicate brain_io.execute()'s side effects."""
    if _already_journaled(order.get("client_order_id"),
                          order.get("alpaca_order_id")):
        print(f"  (fill {order.get('client_order_id')} already journaled — skipped)")
        return
    run_id = str(order.get("proposal_id") or "async-executor")
    journal.log_trade(order, fill, run_id)
    action = str(fill.get("action", "")).upper()
    # An executor fill is a TRADE the run itself never saw, so the run's plain
    # X draft never earned its `_trade` suffix and the poster ignored it — the
    # BKR 07-28 buy went unannounced for exactly this reason. Promote it here,
    # at the moment the fill becomes real. Fail-soft inside the helper.
    try:
        from tools.x_poster import promote_draft_for_fill
        promote_draft_for_fill(order.get("proposal_id"), order=order, fill=fill)
    except Exception:
        pass
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


# --------------------------------------------------------------------------- #
# Pre-context broker sync (added 2026-10-07)
# --------------------------------------------------------------------------- #
# ROOT CAUSE THIS CLOSES. The trading cycle never called run_reconcile(): it read
# the book through broker.get_portfolio() -> sync_mirror(), which only FLAGS a
# position the broker no longer holds (not_at_broker) and keeps it in the mirror.
# Recording a broker-side exit was left entirely to the stop-watch workflow — and
# that job's commit step staged nothing (see stop-watch.yml), so every fill it
# ingested died with the runner. ILMN's stop filled 09:39 ET on 2026-10-07 and
# the 10:30 slot still reviewed it as held. Every trading context is now built
# AFTER the same ingest the stop watch runs, and carries a loud
# broker_reconciliation block comparing the two books.

def _fill_line(f: dict) -> dict:
    return {"ticker": f.get("ticker"), "action": f.get("action"),
            "quantity": f.get("quantity"), "fill_price": f.get("fill_price"),
            "filled_at": f.get("filled_at"),
            "reason": f.get("forced_exit_reason")
            or ("resting_stop_breached" if str(f.get("client_order_id") or "")
                .startswith("EESTOP-") else None),
            "realized_pnl_usd": f.get("realized_pnl_usd")}


def sync_broker_state(*, ingest: bool = True) -> dict:
    """Ingest broker-side fills, then compare mirror vs broker positions.

    Alpaca backend only (simulation has no broker-side lifecycle). Fail-soft:
    never raises. `ingest=False` runs the read-only comparison alone (used when
    another process holds the run lock and owns ledger writes)."""
    try:
        if broker.backend_name() != "alpaca_paper":
            return {"status": "not_applicable", "mismatches": [],
                    "ingested_fills": []}
    except Exception:
        return {"status": "unavailable", "mismatches": [], "ingested_fills": [],
                "reason": "backend unknown"}
    fills: list[dict] = []
    if ingest:
        fills = run_reconcile()
    try:
        from execution import alpaca_broker
        rec = alpaca_broker.broker_position_mismatches()
    except Exception as e:
        rec = {"status": "unavailable", "mismatches": [],
               "reason": f"mismatch check failed: {str(e)[:160]}"}
    rec["ingested_fills"] = [_fill_line(f) for f in fills]
    rec["ingest_ran"] = bool(ingest)
    return rec


def reconciliation_block(rec: dict | None) -> dict | None:
    """The brain-facing context block. Loud by construction: a mismatch or a
    fill booked this run leads with an ALERT the brain cannot read past."""
    if not isinstance(rec, dict) or rec.get("status") == "not_applicable":
        return None
    block = dict(rec)
    alerts = []
    for f in rec.get("ingested_fills") or []:
        alerts.append(
            f"{f.get('ticker')} was CLOSED AT THE BROKER ({f.get('reason') or 'broker-side fill'}) "
            f"- {f.get('quantity')} @ {f.get('fill_price')} at {f.get('filled_at')}, "
            f"P&L {f.get('realized_pnl_usd')}. Booked into the ledger just before this "
            f"run. It is NOT held: do not review or propose selling it; explain the exit "
            f"in commentary.")
    for m in rec.get("mismatches") or []:
        alerts.append(
            f"BROKER/LEDGER MISMATCH {m.get('ticker')} ({m.get('kind')}): ledger qty "
            f"{m.get('mirror_qty')} vs broker qty {m.get('broker_qty')}. The BROKER is the "
            f"truth for quantities; treat the ledger row as unreliable and say so in "
            f"commentary.")
    if rec.get("status") == "unavailable":
        alerts.append("Broker position check DID NOT RUN ("
                      f"{rec.get('reason') or 'unavailable'}) - holdings come from the "
                      "last committed ledger and are UNVERIFIED against the account.")
    block["ALERT"] = (" | ".join(alerts) if alerts
                      else "ok - ledger positions match the broker account")
    return block


def ingest_allowed_here() -> bool:
    """Ledger-writing ingest only where the ledger is persisted: everywhere
    except the hourly GitHub bundle-refresh gather, which commits data/ only
    (its ingested fills would die with the runner). grok-cycle.yml sets
    EE_SCHEDULED_TRADER and does persist the ledger."""
    import os
    if os.environ.get("EE_SCHEDULED_TRADER"):
        return True
    return not os.environ.get("GITHUB_ACTIONS")


def annotate_portfolio(portfolio: dict | None, block: dict | None) -> None:
    """Stamp every mismatched holding in-place so a reader of the portfolio
    block alone (not just broker_reconciliation) sees the row is not backed."""
    if not isinstance(portfolio, dict) or not isinstance(block, dict):
        return
    by_t = {str(m.get("ticker", "")).upper(): m
            for m in block.get("mismatches") or []}
    for pos in portfolio.get("positions") or []:
        m = by_t.get(str(pos.get("ticker", "")).upper())
        if m:
            pos["BROKER_MISMATCH"] = (
                f"{m.get('kind')}: ledger qty {m.get('mirror_qty')} vs broker qty "
                f"{m.get('broker_qty')} - the broker is the truth; see "
                f"broker_reconciliation")
