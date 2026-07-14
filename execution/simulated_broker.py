"""Simulated broker — the default (and safest) execution backend.

Maintains a paper portfolio in state/portfolio.json. Fills BUY/SELL_TO_CLOSE
orders at the provided reference price (last close from the scanner) with
realistic trading costs so the public track record is not flattered by a
frictionless simulation. What IS modeled (all config-driven from
autonomy_config.json -> "execution_costs", with a fail-soft fallback to today's
behaviour of 10bps slippage / no fees when the block is missing):

    * Slippage (volatility-scaled, symmetric): the effective one-way fraction is
      max(slippage_bps/10000, slippage_atr_fraction x ATR%), so a 7% ATR name costs
      ~0.35% each way (0.05x7) instead of a flat 0.10%. BUY fills at ref*(1+slip),
      SELL at ref*(1-slip). Applied everywhere slippage is (entries AND exits, incl.
      the stop-gap fill). Falls back to the flat slippage_bps fraction when atr_pct
      is absent, so a missing ATR reproduces today's behaviour exactly.
    * Entry gap (BUY opens only): on TOP of slippage, a BUY fills at
      ref*(1 + slip + entry_gap_atr_fraction x ATR%) — an adverse open-gap that
      approximates buying at a worse next session than the exact close the brain saw.
      Zero when atr_pct is absent. It is baked into the fill price (and thus cost
      basis / cash) once and only reported separately, so it is never double-charged.
    * Commission (both sides): max(commission_min_usd, per_share * shares).
      Moomoo US equities are commission-free, so the shipped defaults are 0.
    * Regulatory fees on SELLS ONLY: SEC fee on sell notional + FINRA TAF per
      share (capped). Buy commission is folded into the position's avg_cost;
      sell fees are netted out of proceeds, so realized P&L nets ALL costs.
    * Stop gap-through: a forced stop exit does not fill exactly at the stop —
      when model_stop_gaps is on and the order carries a stop-exit marker plus
      stop_loss + atr_pct, the fill gaps BELOW the stop by
      stop_gap_atr_fraction x ATR before slippage/fees.

What is NOT modeled: partial fills (every order fills in full or is rejected),
queue position / market impact, borrow, and trading halts. The entry gap is an ATR
APPROXIMATION of next-session risk, NOT a true next-open fill — no pending-fill
lifecycle re-prices the order against the following session's actual bar. Do not
claim these.

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
SLIPPAGE = 0.001  # legacy default (10 bps) — used only if config is unreadable


# Fallbacks reproduce today's behaviour exactly (10bps slippage, no fees, no
# gap modeling) so a missing/partial execution_costs block never breaks a run.
_COST_DEFAULTS = {
    "slippage_bps": 10.0,
    "slippage_atr_fraction": 0.0,   # 0 -> vol-scaling off, flat slippage_bps only
    "entry_gap_atr_fraction": 0.0,  # 0 -> no modeled entry gap (same-close behaviour)
    "commission_per_share_usd": 0.0,
    "commission_min_usd": 0.0,
    "sec_sell_fee_rate": 0.0,
    "finra_taf_per_share_usd": 0.0,
    "finra_taf_max_usd": float("inf"),
    "model_stop_gaps": False,
    "stop_gap_atr_fraction": 0.5,
}


def _execution_costs() -> dict:
    """Read the execution_costs block the same way the codebase reads config.

    Robust to a missing block / unreadable file / null values: any absent key
    falls back to _COST_DEFAULTS (today's frictionless-ish behaviour)."""
    block = {}
    try:
        cfg = json.loads((ROOT / "autonomy_config.json").read_text())
        block = cfg.get("execution_costs") or {}
    except Exception:
        block = {}
    out = dict(_COST_DEFAULTS)
    for k in _COST_DEFAULTS:
        v = block.get(k)
        if v is not None:
            out[k] = v
    return out


def _effective_slip(costs: dict, atr_pct) -> float:
    """Effective one-way slippage FRACTION, volatility-scaled and symmetric.

    max(slippage_bps/10000, slippage_atr_fraction x ATR%): a 7% ATR name at
    slippage_atr_fraction=0.05 costs ~0.35% each way, not a flat 0.10%. Falls back
    to the flat slippage_bps fraction when atr_pct is absent/unparseable (today's
    behaviour) and never returns less than that flat floor. Fail-soft; never raises."""
    flat = float(costs["slippage_bps"]) / 10000.0
    if atr_pct is None:
        return flat
    try:
        vol = float(costs["slippage_atr_fraction"]) * (float(atr_pct) / 100.0)
    except (TypeError, ValueError):
        return flat
    return max(flat, vol)


def _entry_gap_frac(costs: dict, atr_pct) -> float:
    """Adverse open-gap FRACTION added to a BUY fill on TOP of slippage.

    entry_gap_atr_fraction x ATR%: approximates buying at a worse next session than
    the exact close the decision used. Zero when atr_pct is absent/unparseable, so a
    missing ATR reproduces the old same-close entry. Fail-soft; never raises."""
    if atr_pct is None:
        return 0.0
    try:
        gap = float(costs["entry_gap_atr_fraction"]) * (float(atr_pct) / 100.0)
    except (TypeError, ValueError):
        return 0.0
    return max(gap, 0.0)


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


def _sell_fill_price(order: dict, ref: float, slip: float, costs: dict) -> tuple[float, bool]:
    """Sell fill price, modeling overnight stop gap-through when applicable.

    ``slip`` is the EFFECTIVE (volatility-scaled) one-way fraction from
    _effective_slip(), so every sell path here — ordinary, gap-through, and the
    no-ATR 2x penalty — is vol-scaled, not flat-bps.

    Returns (fill_price, gap_modeled). A forced stop exit does not fill exactly
    at the stop: with model_stop_gaps on and a stop-exit marker present,
      * with stop_loss + atr_pct: gap BELOW the stop by
        stop_gap_atr_fraction x (atr_pct/100) x ref, then apply sell slippage;
      * with stop_loss but NO atr_pct: degrade gracefully with a 2x slippage
        penalty off ref.
    Otherwise it is an ordinary sell: ref*(1-slip)."""
    reason = str(order.get("forced_exit_reason") or "")
    is_stop = reason.startswith("stop")
    if costs.get("model_stop_gaps") and is_stop:
        stop_loss = order.get("stop_loss")
        atr_pct = order.get("atr_pct")
        if stop_loss is not None and atr_pct is not None:
            try:
                sl = float(stop_loss)
                atrp = float(atr_pct)
                gapped = sl - float(costs["stop_gap_atr_fraction"]) * (atrp / 100.0) * ref
                if gapped <= 0:  # pathological atr — fall back to ref before slippage
                    gapped = ref
                return round(gapped * (1 - slip), 4), True
            except (TypeError, ValueError):
                pass  # bad numbers — fall through to ordinary sell
        elif stop_loss is not None:  # stop but no ATR: 2x slippage penalty
            return round(ref * (1 - 2 * slip), 4), True
    return round(ref * (1 - slip), 4), False


def readback(order_id: str) -> dict | None:
    """Fill the pending order and return the broker's confirmation.

    All slippage/commission/regulatory-fee/stop-gap math lives here (place_order
    only stashes the pending order verbatim), so any cost-bearing fields the
    order carries — forced_exit_reason, stop_loss, atr_pct — are read here."""
    state = _load()
    order = state["pending_orders"].pop(order_id, None)
    if order is None:
        return None
    costs = _execution_costs()
    atr_pct = order.get("atr_pct")  # percent, e.g. 7.36; may be None (fail-soft)
    # Volatility-scaled, symmetric slippage: flat bps floor, ATR-scaled above it.
    # Applied to BOTH entries and exits (incl. the stop-gap fill via _sell_fill_price).
    slip = _effective_slip(costs, atr_pct)
    ref = float(order["reference_price"])
    action = order["action"].upper()

    if action == "BUY":
        # Entry gap: an adverse open-gap on TOP of slippage (opening BUYs only),
        # approximating a worse next-session fill than the close the brain saw.
        # Baked into fill_price ONCE (so cost basis / cash already include it) and
        # only reported via entry_gap_usd — never charged a second time.
        entry_gap = _entry_gap_frac(costs, atr_pct)
        fill_price = round(ref * (1 + slip + entry_gap), 4)
        qty = order.get("quantity") or round(float(order["position_size_usd"]) / fill_price, 4)
        gross = qty * fill_price
        commission = round(max(float(costs["commission_min_usd"]),
                               float(costs["commission_per_share_usd"]) * qty), 4)
        cash_out = round(gross + commission, 2)  # buy pays notional + commission
        if cash_out > state["cash_usd"]:
            fill = {**order, "status": "rejected_insufficient_cash"}
        else:
            notional = round(gross, 2)
            # Cost basis INCLUDES the buy commission, so realized P&L on close
            # nets it out automatically (avg_cost == fill_price when commission 0).
            avg_cost = round((gross + commission) / qty, 4)
            state["cash_usd"] = round(state["cash_usd"] - cash_out, 2)
            existing = next((p for p in state["positions"]
                             if p["ticker"] == order["ticker"].upper()), None)
            is_add = existing is not None
            if is_add:
                # SCALE-IN: merge into the open position with a blended cost basis
                # (both legs commission-inclusive). opened_at / proposal_id stay
                # with the ORIGINAL entry; adds_count feeds the validator's cap.
                old_qty, old_cost = existing["quantity"], existing["avg_cost"]
                new_qty = round(old_qty + qty, 4)
                existing["avg_cost"] = round(
                    (old_qty * old_cost + gross + commission) / new_qty, 4)
                existing["quantity"] = new_qty
                existing["market_value_usd"] = round(new_qty * fill_price, 2)
                existing["buy_commission_usd"] = round(
                    existing.get("buy_commission_usd", 0.0) + commission, 4)
                existing["adds_count"] = int(existing.get("adds_count", 0)) + 1
                if order.get("plan"):  # latest BUY's plan governs the merged position
                    existing["plan"] = order["plan"]
            else:
                new_pos = {
                    "ticker": order["ticker"].upper(), "quantity": qty,
                    "avg_cost": avg_cost, "market_value_usd": notional,
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "proposal_id": order.get("proposal_id"),
                    "buy_commission_usd": commission,
                }
                if order.get("plan"):  # numeric stop/target/horizon persisted with the lot
                    new_pos["plan"] = order["plan"]
                state["positions"].append(new_pos)
            fill = {**order, "status": "filled", "fill_price": fill_price,
                    "quantity": qty, "notional_usd": notional,
                    "is_add": is_add,
                    "position_avg_cost_after": (existing["avg_cost"] if is_add else avg_cost),
                    "total_cost_usd": cash_out,
                    # Per-share dollars the modeled open-gap added to the entry fill
                    # (0 when atr_pct absent). Already inside fill_price/cost basis —
                    # transparency only, do NOT subtract it anywhere.
                    "entry_gap_usd": round(entry_gap * ref, 4),
                    "fees_usd": {"commission": commission, "sec_fee": 0.0, "taf": 0.0,
                                 "slippage_bps": float(costs["slippage_bps"]),
                                 "effective_slippage_pct": round(slip * 100.0, 4)}}
    elif action == "SELL_TO_CLOSE":
        pos = next((p for p in state["positions"] if p["ticker"] == order["ticker"].upper()), None)
        if pos is None:
            fill = {**order, "status": "rejected_no_position"}
        else:
            # PARTIAL EXITS: sell_fraction in (0, 1] - default full close. Forced
            # exits (stops/horizon) never set it, so they remain full closes.
            try:
                frac = float(order.get("sell_fraction") or 1.0)
            except (TypeError, ValueError):
                frac = 1.0
            frac = min(max(frac, 0.0001), 1.0)
            qty = round(pos["quantity"] * frac, 4) if frac < 1.0 else pos["quantity"]
            fill_price, gap_modeled = _sell_fill_price(order, ref, slip, costs)
            if fill_price <= 0:  # never let a modeled gap fill go non-positive
                fill_price = round(max(ref, 0.01) * (1 - slip), 4)
            gross = qty * fill_price
            commission = round(max(float(costs["commission_min_usd"]),
                                   float(costs["commission_per_share_usd"]) * qty), 4)
            sec_fee = round(float(costs["sec_sell_fee_rate"]) * gross, 4)   # on sell notional
            taf = round(min(float(costs["finra_taf_max_usd"]),
                            float(costs["finra_taf_per_share_usd"]) * qty), 4)  # per share, capped
            proceeds = round(gross - commission - sec_fee - taf, 2)  # net cash in
            state["cash_usd"] = round(state["cash_usd"] + proceeds, 2)
            # Price realized P&L, net of ALL costs: buy commission is baked into
            # avg_cost, sell fees are already out of proceeds.
            price_pnl = round(proceeds - qty * pos["avg_cost"], 2)
            # Dividends were ALREADY credited to cash when the event was processed
            # (corporate_actions). We only ATTRIBUTE them to the trade here for
            # per-trade reporting — cash is NOT touched again (no double count).
            # Partials attribute pro-rata; the remainder stays with the position.
            total_divs = float(pos.get("dividends_received_usd", 0.0) or 0.0)
            divs = round(total_divs * frac, 2)
            entry_cost, opened_at = pos["avg_cost"], pos.get("opened_at")
            if frac < 1.0:
                pos["quantity"] = round(pos["quantity"] - qty, 4)
                pos["market_value_usd"] = round(pos["quantity"] * fill_price, 2)
                pos["dividends_received_usd"] = round(total_divs - divs, 2)
            else:
                state["positions"].remove(pos)
            fill = {**order, "status": "filled", "fill_price": fill_price,
                    "quantity": qty, "notional_usd": round(gross, 2),
                    "sell_fraction": round(frac, 4),
                    # entry data stamped on the fill so closed-trade records never
                    # need fragile open/close pairing (adds/partials break pairing)
                    "avg_cost": entry_cost, "position_opened_at": opened_at,
                    "realized_pnl_usd": price_pnl,               # price-only, net of fees
                    "total_realized_pnl_usd": round(price_pnl + divs, 2),  # + carried dividends
                    "dividends_received_usd": divs,
                    "fees_usd": {"commission": commission, "sec_fee": sec_fee, "taf": taf,
                                 "slippage_bps": float(costs["slippage_bps"]),
                                 "effective_slippage_pct": round(slip * 100.0, 4)}}
            if gap_modeled:
                fill["gap_modeled"] = True
                sl = order.get("stop_loss")
                if sl is not None:
                    try:  # how far below the recorded stop we actually filled
                        fill["gap_usd"] = round(float(sl) - fill_price, 4)
                    except (TypeError, ValueError):
                        pass
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
