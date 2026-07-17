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

import json

import journal
from execution import broker

# Chandelier trailing-stop ATR multiple used when autonomy_config's
# trade_quality_requirements block does not carry trailing_stop_atr_multiple.
TRAILING_STOP_ATR_MULTIPLE_DEFAULT = 3.0


def _trailing_atr_multiple() -> float:
    """Chandelier multiple from config (trade_quality_requirements ->
    trailing_stop_atr_multiple) with a hard-coded 3.0 default. Fail-soft:
    an unreadable config or missing key never breaks a run."""
    try:
        cfg = json.loads((broker.ROOT / "autonomy_config.json").read_text())
        v = (cfg.get("trade_quality_requirements") or {}).get("trailing_stop_atr_multiple")
        if v is not None:
            return float(v)
    except Exception:
        pass
    return TRAILING_STOP_ATR_MULTIPLE_DEFAULT


def _chandelier_stop(high_water, atr_pct, last, multiple: float):
    """Chandelier exit level: high-water mark since entry − multiple × ATR,
    with ATR expressed in absolute dollars (atr_pct of the CURRENT price).
    None when any input is missing/unparseable — the caller then behaves
    exactly as today (plan stop only)."""
    if high_water is None or atr_pct is None or last is None:
        return None
    try:
        hw, atrp, px = float(high_water), float(atr_pct), float(last)
    except (TypeError, ValueError):
        return None
    if hw <= 0 or atrp <= 0 or px <= 0:
        return None
    return hw - multiple * (atrp / 100.0) * px


def check_forced_exits(portfolio: dict, prices: dict, atr_by_ticker: dict | None = None) -> list[dict]:
    """Return forced-exit instructions for stop breaches and expired horizons.

    portfolio: dict from tools.portfolio_state.get_portfolio_state()
    prices: {ticker: last_close} from the universe scanner
    atr_by_ticker: optional {TICKER: atr_pct} so each stop-breach exit carries
        the name's ATR — used downstream to model overnight stop gap-through.
        Optional/defaulted: the current call site works unchanged.

    Chandelier trailing stop (ratchet-up only): when a position carries a
    high_water mark and fresh ATR, the trail = high_water − N×ATR$ (N from
    config, default 3.0). It becomes active ONLY once it exceeds the plan
    stop — until then the plan stop governs, and there is deliberately NO
    breakeven jump at +1R. Once active it only ever RATCHETS UP (never
    lowers, never widens): effective stop = max(plan stop_loss, trailing).
    New trail levels are persisted back to the ledger via the broker
    (update_trailing_stops) — exit_guard itself never writes state.
    """
    atr_by_ticker = atr_by_ticker or {}
    multiple = _trailing_atr_multiple()
    exits = []
    trail_updates: dict = {}  # {TICKER: new level} persisted via the broker
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

        # --- Chandelier trail (only meaningful alongside a plan stop) ---
        trailing = None
        if stop:
            try:
                trailing = float(pos.get("trailing_stop") or 0.0) or None
            except (TypeError, ValueError):
                trailing = None
            chandelier = _chandelier_stop(
                pos.get("high_water"), atr_by_ticker.get(ticker), last, multiple)
            # Activation gate: the trail exists only once the chandelier has
            # climbed ABOVE the plan stop; ratchet: it can only ever rise.
            if chandelier is not None and chandelier > float(stop) \
                    and chandelier > (trailing or 0.0):
                trailing = round(chandelier, 4)
                trail_updates[ticker] = trailing
                pos["trailing_stop"] = trailing  # callers see it this run too

        # Effective stop for the breach check: the trail never lowers it.
        effective_stop = None
        trail_binding = False
        if stop:
            effective_stop = float(stop)
            if trailing is not None and trailing > effective_stop:
                effective_stop = trailing
                trail_binding = True

        reason = None
        if effective_stop is not None and float(last) <= effective_stop:
            reason = "trailing_stop_breached" if trail_binding else "stop_loss_breached"
        elif horizon and days_held is not None and days_held >= float(horizon):
            reason = "horizon_expired"
        if reason:
            exits.append({
                "ticker": ticker, "reason": reason,
                "last_price": float(last),
                # stop_loss carries the BINDING level so the broker's gap-through
                # fill models the level that actually fired (trail or plan stop).
                "stop_loss": effective_stop,
                "plan_stop_loss": float(stop) if stop else None,
                "trailing_stop": trailing,
                "atr_pct": atr_by_ticker.get(ticker),
                "days_held": days_held, "horizon": horizon,
            })
    if trail_updates:
        try:  # broker owns all ledger writes; a failed persist never blocks exits
            broker.update_trailing_stops(trail_updates)
        except Exception as e:
            print(f"  (trailing-stop persist failed: {e})")
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
    # Snapshot positions before closes so exit autopsies keep plan / days_held.
    try:
        pre_positions = {
            str(p.get("ticker", "")).upper(): dict(p)
            for p in (broker.get_portfolio().get("positions") or [])
        }
    except Exception:
        pre_positions = {}
    for ex in exits:
        ref = prices.get(ex["ticker"])
        if ref is None:
            continue  # price vanished between check and execute — skip, never guess
        pos_before = pre_positions.get(str(ex["ticker"]).upper())
        order = broker.place_order({
            "ticker": ex["ticker"], "action": "SELL_TO_CLOSE",
            "reference_price": ref, "proposal_id": run_id,
            "forced_exit_reason": ex["reason"],
            # stop + ATR let the broker model overnight gap-through on stop exits
            "stop_loss": ex.get("stop_loss"),
            "atr_pct": ex.get("atr_pct") if ex.get("atr_pct") is not None
                       else atr_by_ticker.get(ex["ticker"]),
        })
        fill = broker.readback(order["order_id"])  # mandatory readback
        status = (fill or {}).get("status")
        if fill is not None and status in broker.QUEUED_STATUSES + broker.RESTING_STATUSES:
            # Queued for the Actions executor, or resting at the broker until
            # the next session (protective sells are never canceled). The
            # executor/reconcile journals the fill + autopsy when it completes.
            journal.log_intent(order, status, run_id)
            print(f"  FORCED EXIT QUEUED {ex['ticker']} ({status})")
            continue
        if fill is None or status != "filled":
            print(f"  FORCED EXIT FAILED {ex['ticker']}: {status}")
            continue
        journal.log_trade(order, fill, run_id)
        fills.append(fill)
        try:
            from tools.exit_autopsy import (
                build_exit_autopsy_from_fill, grade_and_persist_autopsy,
            )
            rec = build_exit_autopsy_from_fill(
                fill, order, pos_before, forced=True, reason=ex.get("reason"))
            grade_and_persist_autopsy(rec)
        except Exception as e:
            print(f"  (exit autopsy skipped: {e})")
        print(f"  FORCED EXIT {fill['ticker']} {fill['quantity']} "
              f"@ {fill['fill_price']} ({ex['reason']})")
    return fills
