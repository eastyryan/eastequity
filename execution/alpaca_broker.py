"""Alpaca paper-trading backend — same contract as simulated_broker.

    place_order(order) -> pending order dict
    readback(order_id) -> fill confirmation dict (or None)
    get_portfolio() / mark_to_market(prices) / update_trailing_stops(trailing)

Real orders hit the Alpaca paper account; state/portfolio.json remains as an
ENRICHED MIRROR of the account. Alpaca is the source of truth for cash,
position quantities and cost basis; the mirror carries everything Alpaca
cannot hold — the plan (stop/target/horizon), high_water + trailing_stop
(chandelier trail), opened_at/proposal_id, adds_count, dividends metadata —
so every downstream consumer (validator, exit_guard, dashboard, closed-trade
pairing) keeps reading the exact same file and shape as the simulation.

CASH-ONLY BY DESIGN: the paper account reports 4x margin buying power; this
adapter spends against CASH and rejects a BUY whose notional exceeds it
(status "rejected_insufficient_cash", same string the simulator uses).

INTENT MODE (cloud): the claude.ai routine sandbox can reach only GitHub, so
when API keys are absent / the API is unreachable / EE_BROKER_FORCE_INTENT=1,
place_order appends the validated order to state/order_intents.json instead
of submitting, and readback returns status "queued_intent". The committed
intent file triggers .github/workflows/execute-orders.yml, whose runner has
normal egress and executes through THIS module in direct mode.

Order mapping (long-only equities):
  BUY  -> market DAY order by NOTIONAL (position_size_usd), guarded by a live
          last-trade check against entry_price_max (fail-open when no quote).
          Unfilled after the poll window -> canceled; a partial fill at
          cancel time is honored as a smaller fill (qty = filled portion).
  SELL_TO_CLOSE -> market DAY order by QTY (sell_fraction pro-rata of the
          live position). Sells are risk-reducing: submitted even when the
          market is closed (queues to the next open) and left RESTING when
          the poll window expires — reconcile() journals the eventual fill.

NOT modeled by Alpaca paper (was modeled by the simulator): dividends,
SEC/FINRA sell fees, borrow. Fills carry zeroed fee fields so downstream
math keeps working; dividend attribution still reads the mirror metadata.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from execution import simulated_broker

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = simulated_broker.STATE_FILE          # one mirror, one shape
INTENTS_FILE = ROOT / "state" / "order_intents.json"
TRADING_BASE = "https://paper-api.alpaca.markets"

_DEFAULTS = {
    "poll_interval_seconds": 2.0,
    "buy_poll_timeout_seconds": 90.0,
    "sell_poll_timeout_seconds": 90.0,
    "price_guard": True,       # live last-trade vs entry_price_max pre-check
}

_probe_cache: dict = {}        # process-lifetime reachability memo


# --------------------------------------------------------------------------- #
# config / auth / transport
# --------------------------------------------------------------------------- #
def _cfg() -> dict:
    out = dict(_DEFAULTS)
    try:
        cfg = json.loads((ROOT / "autonomy_config.json").read_text())
        out.update({k: v for k, v in (cfg.get("alpaca") or {}).items()
                    if not str(k).startswith("_")})
    except Exception:
        pass
    return out


def _keys() -> tuple[str, str] | None:
    k = os.environ.get("ALPACA_API_KEY", "")
    s = os.environ.get("ALPACA_SECRET_KEY", "")
    if not (k and s):
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
            k = os.environ.get("ALPACA_API_KEY", "")
            s = os.environ.get("ALPACA_SECRET_KEY", "")
        except Exception:
            pass
    return (k, s) if k and s else None


def _headers() -> dict | None:
    ks = _keys()
    if not ks:
        return None
    return {"APCA-API-KEY-ID": ks[0], "APCA-API-SECRET-KEY": ks[1]}


def _req(method: str, path: str, *, params=None, body=None, timeout=10):
    """One API call with a single retry on transient failures.
    Returns (status_code, parsed_json_or_None); (0, None) = transport failure."""
    hdrs = _headers()
    if hdrs is None:
        return 0, None
    url = f"{TRADING_BASE}{path}"
    for attempt in (1, 2):
        try:
            r = requests.request(method, url, params=params, json=body,
                                 headers=hdrs, timeout=timeout)
            if r.status_code >= 500 and attempt == 1:
                time.sleep(1.0)
                continue
            try:
                return r.status_code, (r.json() if r.text.strip() else None)
            except ValueError:
                return r.status_code, None
        except requests.RequestException:
            if attempt == 1:
                time.sleep(1.0)
                continue
            return 0, None
    return 0, None


def api_reachable() -> bool:
    """Can this node talk to the Alpaca paper API? Memoized per process.
    False when keys are missing (cloud checkout has no .env) or egress is
    blocked (the routine sandbox) — those nodes queue intents instead."""
    if os.environ.get("EE_BROKER_FORCE_INTENT") == "1":
        return False
    if os.environ.get("EE_BROKER_FORCE_DIRECT") == "1":
        return True
    if "ok" not in _probe_cache:
        code, _ = _req("GET", "/v2/clock", timeout=5)
        _probe_cache["ok"] = code == 200
    return _probe_cache["ok"]


def _clock() -> dict:
    code, body = _req("GET", "/v2/clock")
    return body if code == 200 and isinstance(body, dict) else {}


# --------------------------------------------------------------------------- #
# mirror sync (Alpaca -> state/portfolio.json, metadata preserved)
# --------------------------------------------------------------------------- #
_META_FIELDS = ("opened_at", "proposal_id", "plan", "high_water", "trailing_stop",
                "adds_count", "buy_commission_usd", "dividends_received_usd",
                "demand_driver")


def _fetch_account_positions() -> tuple[dict | None, list | None]:
    code_a, acct = _req("GET", "/v2/account")
    code_p, poss = _req("GET", "/v2/positions")
    if code_a != 200 or not isinstance(acct, dict):
        return None, None
    return acct, (poss if code_p == 200 and isinstance(poss, list) else [])


def sync_mirror() -> dict | None:
    """Pull cash/positions from Alpaca into the mirror, carrying metadata.
    Returns the synced state, or None when the API is unreachable (mirror
    left untouched — cloud nodes keep reading the last committed ledger)."""
    if not api_reachable():
        return None
    acct, poss = _fetch_account_positions()
    if acct is None:
        return None
    state = simulated_broker._load()
    meta = {str(p.get("ticker", "")).upper(): {k: p.get(k) for k in _META_FIELDS}
            for p in state.get("positions", [])}
    new_positions = []
    seen = set()
    for ap in poss or []:
        t = str(ap.get("symbol", "")).upper()
        if not t:
            continue
        seen.add(t)
        qty = round(float(ap.get("qty") or 0.0), 6)
        last = float(ap.get("current_price") or 0.0) or None
        pos = {
            "ticker": t,
            "quantity": qty,
            "avg_cost": round(float(ap.get("avg_entry_price") or 0.0), 4),
            "market_value_usd": round(float(ap.get("market_value") or 0.0), 2),
        }
        if last:
            pos["last_price"] = round(last, 4)
        m = meta.get(t) or {}
        for k, v in m.items():
            if v is not None:
                pos[k] = v
        # high_water ratchet vs the broker's own current price
        try:
            hw = float(pos.get("high_water") or 0.0)
        except (TypeError, ValueError):
            hw = 0.0
        if last:
            pos["high_water"] = max(hw, last)
        new_positions.append(pos)
    # Mirror-only positions (not at the broker) are NEVER silently dropped:
    # they stay flagged so a human/agent sees the drift and reconciles it.
    for p in state.get("positions", []):
        t = str(p.get("ticker", "")).upper()
        if t not in seen:
            p["not_at_broker"] = True
            new_positions.append(p)
    state["positions"] = new_positions
    new_cash = round(float(acct.get("cash") or 0.0), 2)
    try:  # broker equity is authoritative (cash + marked positions)
        new_equity = round(float(acct.get("equity")), 2)
    except (TypeError, ValueError):
        new_equity = round(
            new_cash + sum(p.get("market_value_usd", 0.0)
                           for p in new_positions), 2)
    # Corrupt-read guard: a reachable /v2/account that reports zero equity while
    # the mirror last held real money is a bad read (empty or mis-authed relay
    # response), not a real wipeout — a paper account cannot drop to $0 cash and
    # $0 equity with no positions. Never persist it over a known-good ledger;
    # leave the mirror untouched so downstream keeps the last committed balance.
    prior_equity = round(float(state.get("total_equity_usd") or 0.0), 2)
    if new_equity <= 0 and prior_equity > 0:
        return None
    state["cash_usd"] = new_cash
    state["total_equity_usd"] = new_equity
    state["broker_synced_at"] = datetime.now(timezone.utc).isoformat()
    state["broker_backend"] = "alpaca_paper"
    simulated_broker._save(state)
    return state


# --------------------------------------------------------------------------- #
# contract: get_portfolio / mark_to_market / update_trailing_stops
# --------------------------------------------------------------------------- #
def get_portfolio() -> dict:
    return sync_mirror() or simulated_broker._load()


def mark_to_market(prices: dict) -> dict:
    """Freshen the mirror. Reachable nodes sync from the broker first, then the
    provided prices overlay anything newer (and ratchet high_water) exactly like
    the simulator; unreachable nodes just mark the mirror from bundle prices."""
    sync_mirror()
    return simulated_broker.mark_to_market(prices)


def update_trailing_stops(trailing: dict) -> int:
    return simulated_broker.update_trailing_stops(trailing)  # mirror metadata only


# --------------------------------------------------------------------------- #
# intent queue (cloud path)
# --------------------------------------------------------------------------- #
def _load_intents() -> dict:
    try:
        blob = json.loads(INTENTS_FILE.read_text())
        return blob if isinstance(blob, dict) and isinstance(blob.get("intents"), list) \
            else {"intents": []}
    except Exception:
        return {"intents": []}


def _save_intents(blob: dict) -> None:
    INTENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    INTENTS_FILE.write_text(json.dumps(blob, indent=2, default=str))


def queued_intents() -> list[dict]:
    return _load_intents()["intents"]


def clear_intents(consumed_ids: list[str]) -> None:
    blob = _load_intents()
    blob["intents"] = [i for i in blob["intents"]
                       if i.get("order_id") not in set(consumed_ids)]
    _save_intents(blob)


# --------------------------------------------------------------------------- #
# contract: place_order / readback
# --------------------------------------------------------------------------- #
def place_order(order: dict) -> dict:
    """Submit (or queue) an order. Returns the pending order dict; all fill
    interpretation happens in readback(), same as the simulator."""
    # The executor re-places queued intents with their ORIGINAL client id, so a
    # crashed/retried executor run can never submit the same intent twice
    # (Alpaca enforces client_order_id uniqueness; the 409 path picks it up).
    order_id = str(order.get("client_order_id") or f"EE-{uuid.uuid4().hex[:12]}")
    pending = {**order, "order_id": order_id, "client_order_id": order_id,
               "status": "pending",
               "submitted_at": datetime.now(timezone.utc).isoformat()}

    if not api_reachable():  # cloud node: queue for the GitHub Actions executor
        pending["status"] = "queued_intent"
        blob = _load_intents()
        if not any(i.get("order_id") == order_id for i in blob["intents"]):
            blob["intents"].append({**pending,
                                    "queued_at": datetime.now(timezone.utc).isoformat()})
            _save_intents(blob)
        return pending

    action = str(order.get("action", "")).upper()
    ticker = str(order.get("ticker", "")).upper()
    is_open = bool(_clock().get("is_open"))

    if action == "BUY":
        if not is_open:
            # BUYs never queue overnight: the brain priced NOW, not tomorrow's open.
            pending["status"] = "rejected_market_closed"
            return _record_dead_order(pending)
        notional = round(float(order.get("position_size_usd") or 0.0), 2)
        if notional < 1.0:
            pending["status"] = "rejected_bad_notional"
            return _record_dead_order(pending)
        state = sync_mirror() or simulated_broker._load()
        if notional > float(state.get("cash_usd") or 0.0):
            pending["status"] = "rejected_insufficient_cash"
            return _record_dead_order(pending)
        if _cfg().get("price_guard", True):
            entry_max = order.get("entry_price_max")
            live = _live_last_trade(ticker)
            if entry_max is not None and live is not None \
                    and live > float(entry_max):
                pending["status"] = "rejected_price_above_entry_max_live"
                pending["live_price"] = live
                return _record_dead_order(pending)
        body = {"symbol": ticker, "side": "buy", "type": "market",
                "time_in_force": "day", "notional": f"{notional:.2f}",
                "client_order_id": order_id}
    elif action == "SELL_TO_CLOSE":
        qty = _sell_qty(ticker, order.get("sell_fraction"))
        if qty is None or qty <= 0:
            pending["status"] = "rejected_no_position"
            return _record_dead_order(pending)
        pending["requested_qty"] = qty
        body = {"symbol": ticker, "side": "sell", "type": "market",
                "time_in_force": "day", "qty": _fmt_qty(qty),
                "client_order_id": order_id}
    else:
        pending["status"] = "rejected_unsupported_action"
        return _record_dead_order(pending)

    code, resp = _req("POST", "/v2/orders", body=body, timeout=15)
    if code == 200 and isinstance(resp, dict) and resp.get("id"):
        pending["alpaca_order_id"] = resp["id"]
        pending["status"] = "submitted"
    elif code in (409, 422) and "client_order_id" in json.dumps(resp or {}):
        # duplicate client_order_id -> an identical submit already exists (idempotent retry)
        existing = _get_order_by_client_id(order_id)
        if existing:
            pending["alpaca_order_id"] = existing.get("id")
            pending["status"] = "submitted"
        else:
            pending["status"] = f"rejected_submit_{code}"
            pending["broker_response"] = resp
            return _record_dead_order(pending)
    else:
        pending["status"] = f"rejected_submit_{code}"
        pending["broker_response"] = resp
        return _record_dead_order(pending)

    # stash for readback (and for reconcile() if the poll window expires)
    state = simulated_broker._load()
    state.setdefault("pending_orders", {})[order_id] = pending
    simulated_broker._save(state)
    return pending


def readback(order_id: str) -> dict | None:
    """Resolve a placed order into a fill dict (simulator-shaped).

    Intent-mode orders return their queued record (status "queued_intent") —
    the Actions executor performs the real submit + readback later.
    """
    for it in queued_intents():
        if it.get("order_id") == order_id:
            return dict(it)

    state = simulated_broker._load()
    pending = (state.get("pending_orders") or {}).get(order_id)
    if pending is None:
        return None
    if str(pending.get("status", "")).startswith("rejected"):
        state["pending_orders"].pop(order_id, None)
        simulated_broker._save(state)
        return _finalize_dead(pending)

    cfg = _cfg()
    action = str(pending.get("action", "")).upper()
    timeout = float(cfg["buy_poll_timeout_seconds"] if action == "BUY"
                    else cfg["sell_poll_timeout_seconds"])
    interval = max(0.5, float(cfg["poll_interval_seconds"]))
    is_open = bool(_clock().get("is_open"))
    if action == "SELL_TO_CLOSE" and not is_open:
        # risk-reducing order resting until the next session -> reconcile() finishes it
        pending["status"] = "resting_market_closed"
        state["pending_orders"][order_id] = pending
        simulated_broker._save(state)
        return dict(pending)

    deadline = time.time() + timeout
    ao = None
    while time.time() < deadline:
        ao = _get_order_by_client_id(order_id)
        if ao and ao.get("status") in ("filled", "canceled", "expired",
                                       "rejected", "done_for_day"):
            break
        time.sleep(interval)

    if ao and ao.get("status") == "filled":
        return _apply_fill(pending, ao)

    filled_qty = float((ao or {}).get("filled_qty") or 0.0)
    if action == "BUY":
        # cancel the remainder; honor any partial fill as a smaller fill
        if pending.get("alpaca_order_id"):
            _req("DELETE", f"/v2/orders/{pending['alpaca_order_id']}")
            time.sleep(1.5)
            ao = _get_order_by_client_id(order_id) or ao
            filled_qty = float((ao or {}).get("filled_qty") or 0.0)
        if filled_qty > 0:
            return _apply_fill(pending, ao)
        pending["status"] = "rejected_unfilled_canceled"
        state = simulated_broker._load()
        state["pending_orders"].pop(order_id, None)
        simulated_broker._save(state)
        return _finalize_dead(pending)

    # SELL that hasn't completed: leave it working (protective); a partial has
    # already reduced the position at the broker — reconcile() records the rest.
    pending["status"] = "resting_awaiting_fill"
    state = simulated_broker._load()
    state["pending_orders"][order_id] = pending
    simulated_broker._save(state)
    return dict(pending)


def reconcile() -> list[tuple[dict, dict]]:
    """Complete any pending orders that reached a terminal state at the broker.
    Returns [(order, fill), ...] for the caller to journal. Safe to run
    anytime; no-ops when unreachable or nothing is pending."""
    if not api_reachable():
        return []
    state = simulated_broker._load()
    done: list[tuple[dict, dict]] = []
    for oid, pending in list((state.get("pending_orders") or {}).items()):
        ao = _get_order_by_client_id(oid)
        if not ao:
            continue
        status = ao.get("status")
        filled_qty = float(ao.get("filled_qty") or 0.0)
        if status == "filled" or (status in ("canceled", "expired", "rejected",
                                             "done_for_day") and filled_qty > 0):
            fill = _apply_fill(pending, ao)
            if fill and fill.get("status") == "filled":
                done.append((pending, fill))
        elif status in ("canceled", "expired", "rejected"):
            pending["status"] = f"dead_{status}"
            st = simulated_broker._load()
            st["pending_orders"].pop(oid, None)
            st["history"].append({**pending,
                                  "filled_at": datetime.now(timezone.utc).isoformat()})
            simulated_broker._save(st)
    return done


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _fmt_qty(q: float) -> str:
    s = f"{q:.9f}".rstrip("0").rstrip(".")
    return s or "0"


def _live_last_trade(ticker: str) -> float | None:
    try:
        from tools import alpaca_data
        rec = alpaca_data.get_prices([ticker]).get(ticker)
        if rec and rec.get("via") in ("latest_trade", "minute_bar"):
            return float(rec["price"])
    except Exception:
        pass
    return None


def _get_order_by_client_id(client_order_id: str) -> dict | None:
    code, body = _req("GET", "/v2/orders:by_client_order_id",
                      params={"client_order_id": client_order_id})
    return body if code == 200 and isinstance(body, dict) else None


def _sell_qty(ticker: str, sell_fraction) -> float | None:
    """Quantity for a sell: live position qty_available pro-rata by fraction."""
    code, ap = _req("GET", f"/v2/positions/{ticker}")
    if code != 200 or not isinstance(ap, dict):
        return None
    try:
        avail = float(ap.get("qty_available") or ap.get("qty") or 0.0)
    except (TypeError, ValueError):
        return None
    if avail <= 0:
        return None
    try:
        frac = float(sell_fraction or 1.0)
    except (TypeError, ValueError):
        frac = 1.0
    frac = min(max(frac, 0.0001), 1.0)
    return avail if frac >= 1.0 else round(avail * frac, 6)


def _record_dead_order(pending: dict) -> dict:
    """Rejected before reaching the broker: journal it in history immediately
    so readback() can hand back the terminal record (simulator parity)."""
    state = simulated_broker._load()
    state.setdefault("pending_orders", {})[pending["order_id"]] = pending
    simulated_broker._save(state)
    return dict(pending)


def _finalize_dead(pending: dict) -> dict:
    dead = {**pending, "filled_at": datetime.now(timezone.utc).isoformat()}
    state = simulated_broker._load()
    state["history"].append(dead)
    simulated_broker._save(state)
    return dead


def _apply_fill(pending: dict, ao: dict) -> dict:
    """Turn a terminal Alpaca order into a simulator-shaped fill dict and
    bring the mirror in line (metadata stamped, account synced)."""
    action = str(pending.get("action", "")).upper()
    ticker = str(pending.get("ticker", "")).upper()
    qty = round(float(ao.get("filled_qty") or 0.0), 6)
    fill_price = round(float(ao.get("filled_avg_price") or 0.0), 4)
    zero_fees = {"commission": 0.0, "sec_fee": 0.0, "taf": 0.0,
                 "slippage_bps": 0.0, "effective_slippage_pct": 0.0}

    state = simulated_broker._load()
    pre_pos = next((p for p in state.get("positions", [])
                    if str(p.get("ticker", "")).upper() == ticker), None)

    if action == "BUY":
        is_add = pre_pos is not None and not pre_pos.get("not_at_broker")
        notional = round(qty * fill_price, 2)
        fill = {**pending, "status": "filled", "fill_price": fill_price,
                "quantity": qty, "notional_usd": notional,
                "is_add": is_add, "total_cost_usd": notional,
                "entry_gap_usd": 0.0, "fees_usd": zero_fees,
                "alpaca_status": ao.get("status")}
    else:  # SELL_TO_CLOSE
        avg_cost = float((pre_pos or {}).get("avg_cost") or 0.0)
        pre_qty = float((pre_pos or {}).get("quantity") or 0.0)
        frac = round(min(max(qty / pre_qty, 0.0), 1.0), 4) if pre_qty > 0 else 1.0
        total_divs = float((pre_pos or {}).get("dividends_received_usd", 0.0) or 0.0)
        divs = round(total_divs * frac, 2)
        price_pnl = round((fill_price - avg_cost) * qty, 2) if avg_cost else None
        fill = {**pending, "status": "filled", "fill_price": fill_price,
                "quantity": qty, "notional_usd": round(qty * fill_price, 2),
                "sell_fraction": frac,
                "avg_cost": avg_cost or None,
                "position_opened_at": (pre_pos or {}).get("opened_at"),
                "entry_plan": (pre_pos or {}).get("plan"),
                "entry_proposal_id": (pre_pos or {}).get("proposal_id"),
                "realized_pnl_usd": price_pnl,
                "total_realized_pnl_usd": (round(price_pnl + divs, 2)
                                           if price_pnl is not None else None),
                "dividends_received_usd": divs,
                "fees_usd": zero_fees, "alpaca_status": ao.get("status")}

    fill["filled_at"] = ao.get("filled_at") or datetime.now(timezone.utc).isoformat()

    # ---- mirror update: sync from the broker, then stamp metadata ----
    synced = sync_mirror()
    state = synced if synced is not None else simulated_broker._load()
    pos = next((p for p in state.get("positions", [])
                if str(p.get("ticker", "")).upper() == ticker), None)

    if action == "BUY" and pos is not None:
        if fill["is_add"]:
            pos["adds_count"] = int(pre_pos.get("adds_count", 0) or 0) + 1
            pos["opened_at"] = pre_pos.get("opened_at")
            pos["proposal_id"] = pre_pos.get("proposal_id")
            if pending.get("plan"):
                pos["plan"] = pending["plan"]
            elif pre_pos.get("plan"):
                pos["plan"] = pre_pos["plan"]
        else:
            pos["opened_at"] = datetime.now(timezone.utc).isoformat()
            pos["proposal_id"] = pending.get("proposal_id")
            pos["adds_count"] = 0
            pos["buy_commission_usd"] = 0.0
            if pending.get("plan"):
                pos["plan"] = pending["plan"]
        dd = pending.get("demand_driver") or (
            (pending.get("plan") or {}).get("demand_driver")
            if isinstance(pending.get("plan"), dict) else None)
        if dd:
            pos["demand_driver"] = str(dd).strip().lower()
        try:
            hw = float(pos.get("high_water") or 0.0)
        except (TypeError, ValueError):
            hw = 0.0
        pos["high_water"] = max(hw, fill_price)
        fill["position_avg_cost_after"] = pos.get("avg_cost")
    elif action == "SELL_TO_CLOSE":
        if pos is not None and pos.get("not_at_broker") and qty > 0 \
                and pre_pos is not None \
                and qty >= float(pre_pos.get("quantity") or 0.0) - 1e-6:
            # Full close: the broker no longer holds it and THIS fill is the
            # record of why — the "never silently drop" flag doesn't apply.
            state["positions"] = [p for p in state["positions"] if p is not pos]
            pos = None
        if pos is not None and pre_pos is not None:  # partial: carry remaining divs
            pos["dividends_received_usd"] = round(
                float(pre_pos.get("dividends_received_usd", 0.0) or 0.0)
                - fill["dividends_received_usd"], 2)
            for k in ("opened_at", "proposal_id", "plan", "high_water",
                      "trailing_stop", "adds_count", "demand_driver"):
                if pre_pos.get(k) is not None and pos.get(k) is None:
                    pos[k] = pre_pos[k]

    state["pending_orders"].pop(pending["order_id"], None)
    state["history"].append(fill)
    if state.get("positions"):
        state["total_equity_usd"] = round(
            float(state.get("cash_usd") or 0.0)
            + sum(p.get("market_value_usd", 0.0) for p in state["positions"]), 2)
    simulated_broker._save(state)
    return fill


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Alpaca broker utilities")
    ap.add_argument("--reconcile", action="store_true",
                    help="complete pending orders; print fills as JSON")
    ap.add_argument("--sync", action="store_true", help="sync mirror from Alpaca")
    args = ap.parse_args()
    if args.reconcile:
        fills = reconcile()
        print(json.dumps([f for _, f in fills], indent=2, default=str))
    if args.sync or not (args.reconcile or args.sync):
        st = sync_mirror()
        print(json.dumps({"synced": st is not None,
                          "cash": (st or {}).get("cash_usd"),
                          "equity": (st or {}).get("total_equity_usd"),
                          "positions": len((st or {}).get("positions", []))},
                         indent=2))
