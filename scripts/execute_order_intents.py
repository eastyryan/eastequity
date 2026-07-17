#!/usr/bin/env python3
"""Order-intent executor — runs where Alpaca IS reachable (GitHub Actions
runner or the local Mac), completing orders that a cloud trading run queued.

Why this exists: the claude.ai routine sandbox can reach only GitHub, so a
cloud run cannot call paper-api.alpaca.markets. Instead its --act-on step
writes validated orders to state/order_intents.json and pushes; the push
triggers .github/workflows/execute-orders.yml, which runs this script:

  1. KILL_SWITCH: BUY intents are dropped (journaled); SELL intents still
     execute — protective exits must never be blocked by a halt.
  2. Each intent re-places through execution.alpaca_broker with its original
     client_order_id (idempotent: a duplicate submit resolves to the existing
     broker order, so retries/crashes can never double-trade).
  3. Fills are journaled with the ORIGINATING run_id (order.proposal_id) and
     get the same side effects a synchronous fill gets (exit autopsy,
     shadow-book close) via execution.reconcile_runner.record_fill.
  4. reconcile() completes any previously-resting orders (e.g. an after-hours
     protective sell that filled at the open).
  5. dashboard/data/latest.json gets a minimal truthful patch (portfolio
     block + executor fills + timestamp) so the site reflects the trade as
     soon as Vercel rebuilds; the next full run rewrites the rich view.
  6. Everything is committed and pushed WITHOUT [vercel skip].

Usage: python scripts/execute_order_intents.py [--no-git]
Env:   ALPACA_API_KEY / ALPACA_SECRET_KEY (Actions secrets or local .env)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EE_BROKER_FORCE_DIRECT", "1")  # this node has egress

import journal                                    # noqa: E402
from execution import alpaca_broker, broker      # noqa: E402
from execution.reconcile_runner import record_fill, run_reconcile  # noqa: E402


def _kill_switch_on() -> bool:
    return (ROOT / "state" / "KILL_SWITCH").exists()


def execute_intents() -> dict:
    summary = {"executed": [], "resting": [], "rejected": [], "dropped": []}
    intents = alpaca_broker.queued_intents()
    if not intents:
        return summary
    kill = _kill_switch_on()
    consumed: list[str] = []
    for intent in intents:
        oid = intent.get("order_id")
        action = str(intent.get("action", "")).upper()
        ticker = str(intent.get("ticker", "")).upper()
        run_id = str(intent.get("proposal_id") or "async-executor")
        if kill and action == "BUY":
            journal.log_rejection(
                {"ticker": ticker, "action": action},
                ["kill_switch_active_buy_intent_dropped"], run_id)
            summary["dropped"].append(f"{action} {ticker}")
            consumed.append(oid)
            continue
        # Consume the intent from the LOCAL file BEFORE placing: readback()
        # short-circuits on queued intents, so a still-listed order would
        # shadow its own fill. Crash-safe: the committed copy on origin keeps
        # the intent until the final push, and client_order_id dedupe makes a
        # replayed submit resolve to the existing broker order.
        alpaca_broker.clear_intents([oid])
        # Re-place with the ORIGINAL client id (idempotent), then readback.
        order = {k: v for k, v in intent.items()
                 if k not in ("status", "queued_at", "submitted_at", "order_id")}
        placed = alpaca_broker.place_order(order)
        fill = alpaca_broker.readback(placed["order_id"])
        status = (fill or {}).get("status")
        if status == "filled":
            record_fill(placed, fill)
            summary["executed"].append(
                f"{action} {ticker} {fill.get('quantity')} @ {fill.get('fill_price')}")
        elif status in broker.RESTING_STATUSES:
            # order is live at the broker; reconcile (scheduled) completes it
            summary["resting"].append(f"{action} {ticker} ({status})")
        else:
            journal.log_rejection({"ticker": ticker, "action": action},
                                  [f"executor_fill_failed:{status}"], run_id)
            summary["rejected"].append(f"{action} {ticker} ({status})")
        consumed.append(oid)
    alpaca_broker.clear_intents(consumed)
    return summary


def patch_dashboard(executed_fills_summary: dict) -> bool:
    """Minimal truthful refresh of latest.json after async fills: the portfolio
    block, the executor's fills, and a timestamp. Never touches equity_history
    (its benchmark-preservation logic belongs to the full publisher)."""
    latest_file = ROOT / "dashboard" / "data" / "latest.json"
    try:
        latest = json.loads(latest_file.read_text())
    except Exception:
        return False
    try:
        from tools.portfolio_state import get_portfolio_state
        state = get_portfolio_state()
        # Never clobber a known-good portfolio with a zeroed read. A reachable
        # broker that reports $0 equity while the dashboard last showed real
        # money is a bad account read, not a wipeout — keep the prior block.
        new_equity = float(state.get("total_equity_usd") or 0.0)
        prior_equity = float((latest.get("portfolio") or {}).get("total_equity_usd") or 0.0)
        if new_equity <= 0 and prior_equity > 0:
            print("  (skipped portfolio patch: broker read returned $0 equity)")
        else:
            latest["portfolio"] = {
                "cash_usd": state.get("cash_usd"),
                "positions": state.get("positions", []),
                "total_equity_usd": state.get("total_equity_usd"),
            }
        note = {
            "executor_updated_at": datetime.now(timezone.utc).isoformat(),
            **{k: v for k, v in executed_fills_summary.items() if v},
        }
        latest["executor_update"] = note
        latest_file.write_text(json.dumps(latest, indent=2, default=str))
        return True
    except Exception as e:
        print(f"  (dashboard patch failed: {e})")
        return False


def commit_and_push() -> None:
    """Commit executor results with the same pull-rebase-retry the publisher
    uses. NO [vercel skip]: position changes must rebuild the site."""
    # Vercel blocks deploys for commits authored by unverified emails (the
    # hws.edu lesson) — use the account's proven noreply identity.
    subprocess.run(["git", "config", "user.name", "ee-executor"], cwd=ROOT)
    subprocess.run(["git", "config", "user.email",
                    "239281986+eastyryan@users.noreply.github.com"], cwd=ROOT)
    paths = ["state/portfolio.json", "state/order_intents.json",
             "journal", "dashboard/data/latest.json"]
    for p in paths:
        subprocess.run(["git", "add", "-A", p], cwd=ROOT, capture_output=True)
    r = subprocess.run(["git", "commit", "-m",
                        "Execute order intents via Alpaca [executor]"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("  nothing to commit")
        return
    # Same pull-rebase-retry as runlib.publish (concurrent relay/Action pushes).
    for attempt in range(3):
        r = subprocess.run(["git", "push", "origin", "HEAD:main"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print("  pushed — Vercel deploying")
            return
        print(f"  push rejected (attempt {attempt + 1}/3), rebasing...")
        rb = subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
        if rb.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=ROOT,
                           capture_output=True, text=True)
            print(f"  REBASE FAILED: {rb.stderr[-300:]}", file=sys.stderr)
            return
    print(f"  PUSH FAILED after retries: {r.stderr[-300:]}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-git", action="store_true",
                    help="execute + journal but do not commit/push")
    args = ap.parse_args()

    if not alpaca_broker.api_reachable():
        print("Alpaca API unreachable (or keys missing) — nothing to do here.")
        return 0

    print("== executing queued intents ==")
    summary = execute_intents()
    print(json.dumps(summary, indent=2))

    print("== reconciling resting orders ==")
    reconciled = run_reconcile()
    if reconciled:
        summary["executed"] += [
            f"{f.get('action')} {f.get('ticker')} {f.get('quantity')} "
            f"@ {f.get('fill_price')} (reconciled)" for f in reconciled]

    changed_anything = any(summary.values()) or bool(reconciled)
    if changed_anything:
        patch_dashboard(summary)
    else:
        # keep the mirror fresh even on quiet runs (cheap, idempotent)
        alpaca_broker.sync_mirror()

    if not args.no_git and changed_anything:
        commit_and_push()
    return 0


if __name__ == "__main__":
    sys.exit(main())
