"""Deterministic exit enforcement — runs BEFORE the brain each cycle.

An LLM re-underwriting a losing position can rationalize holding through its
own stop ("thesis intact, stop was too tight"). This module removes that
option: stops and holding horizons are enforced in pure Python from the
position's original_plan, using the same scanner prices the brain will see.
Forced exits execute through the normal broker place_order/readback contract
so nothing about execution or journaling changes.

Never force-exits blind: positions with no recorded plan or no fresh price
are left for the brain to review.
"""

from __future__ import annotations

import journal
from execution import simulated_broker


def check_forced_exits(portfolio: dict, prices: dict, atr_by_ticker: dict | None = None) -> list[dict]:
    """Return forced-exit instructions for stop breaches and expired horizons.

    portfolio: dict from tools.portfolio_state.get_portfolio_state()
    prices: {ticker: last_close} from the universe scanner
    atr_by_ticker: optional {TICKER: atr_pct} so each stop-breach exit carries
        the name's ATR — used downstream to model overnight stop gap-through.
        Optional/defaulted: the current call site works unchanged.
    """
    atr_by_ticker = atr_by_ticker or {}
    exits = []
    for pos in portfolio.get("positions", []):
        plan = pos.get("original_plan")
        if not plan:
            continue  # no recorded plan — never force-exit blind
        ticker = pos["ticker"].upper()
        last = prices.get(ticker)
        if last is None:
            continue  # no fresh price — leave for the brain
        stop = plan.get("stop_loss")
        horizon = plan.get("holding_horizon_days")
        days_held = pos.get("days_held")

        reason = None
        if stop and float(last) <= float(stop):
            reason = "stop_loss_breached"
        elif horizon and days_held is not None and days_held >= float(horizon):
            reason = "horizon_expired"
        if reason:
            exits.append({
                "ticker": ticker, "reason": reason,
                "last_price": float(last),
                "stop_loss": float(stop) if stop else None,
                "atr_pct": atr_by_ticker.get(ticker),
                "days_held": days_held, "horizon": horizon,
            })
    return exits


def execute_forced_exits(exits: list, prices: dict, run_id: str,
                         atr_by_ticker: dict | None = None) -> list[dict]:
    """Close each flagged position via the broker; mandatory readback + journal.

    atr_by_ticker: optional {TICKER: atr_pct}. For stop-breach exits, the stop
    level and ATR are passed to the broker so the fill models overnight
    gap-through (fills BELOW the stop) instead of pretending it filled at the
    stop. Optional/defaulted — the existing orchestrator call still works; the
    exit dict's own atr_pct (from check_forced_exits) is used when this is absent."""
    atr_by_ticker = atr_by_ticker or {}
    fills = []
    for ex in exits:
        ref = prices.get(ex["ticker"])
        if ref is None:
            continue  # price vanished between check and execute — skip, never guess
        order = simulated_broker.place_order({
            "ticker": ex["ticker"], "action": "SELL_TO_CLOSE",
            "reference_price": ref, "proposal_id": run_id,
            "forced_exit_reason": ex["reason"],
            # stop + ATR let the broker model overnight gap-through on stop exits
            "stop_loss": ex.get("stop_loss"),
            "atr_pct": ex.get("atr_pct") if ex.get("atr_pct") is not None
                       else atr_by_ticker.get(ex["ticker"]),
        })
        fill = simulated_broker.readback(order["order_id"])  # mandatory readback
        if fill is None or fill.get("status") != "filled":
            print(f"  FORCED EXIT FAILED {ex['ticker']}: "
                  f"{(fill or {}).get('status')}")
            continue
        journal.log_trade(order, fill, run_id)
        fills.append(fill)
        print(f"  FORCED EXIT {fill['ticker']} {fill['quantity']} "
              f"@ {fill['fill_price']} ({ex['reason']})")
    return fills
