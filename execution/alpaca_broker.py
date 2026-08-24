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

RESTING PROTECTIVE STOPS (config: alpaca.resting_stops)
-------------------------------------------------------
Until this existed, every order body in the repo was `type: "market"` and a
"stop" was a float in state/portfolio.json evaluated only when a cycle ran —
scripts/stop_watch.py measured the blind windows at ~2h intraday, ~17.5h
overnight and ~65.5h over a weekend, and the one closed stop in this book's
history filled 4.5% THROUGH its level. A filled BUY now leaves a real stop
order resting at the exchange.

It is a SEPARATE stop order, deliberately NOT a bracket/OTO/OCO:
  1. Alpaca rejects `order_class` alongside `notional`, and every entry here is
     notional (position_size_usd) by design.
  2. Notional orders cannot be PATCHed at all — so a bracket's stop leg could
     never be ratcheted by the chandelier trail. That alone disqualifies it.
  3. Bracket/OCO are unsupported for fractional quantities generally.
A standalone qty-based stop is submitted after the entry readback: replaceable
(therefore ratchetable), and a failed arm is a LOUD journaled RISK EVENT rather
than a silent kill of the entry.

THE CONSTRAINT THAT COULD NOT BE ENGINEERED AROUND: Alpaca supports fractional
quantities only with time_in_force=day. A notional entry leaves a fractional
position, so a GTC stop on the full quantity is rejected outright. What we do
instead — and do not paper over — is cover the WHOLE-SHARE portion GTC (that is
what genuinely survives overnight and the weekend) and record the sub-share
remainder as protective_stop.uncovered_qty: visible, never assumed safe. A
position under one share has no whole-share leg at all and can only get a DAY
stop (protective_stop.session_only), which scripts/stop_watch.py re-arms on its
5-minute tick.

OUT-OF-BAND FILLS: a stop that fires at 03:00 is invisible to reconcile() — no
journal line, no closed trade, and a phantom position exit_guard re-fires on
forever. ingest_out_of_band_fills() sweeps the broker's closed-order list and
replays unseen SELLS through the ordinary _apply_fill path. Unknown broker-side
BUYS are flagged but NEVER journaled: inventing a trade record corrupts cost
basis and every closed-trade figure derived from it.

BROKER_SYNC_SUSPECT: broker equity is cross-checked against
starting_capital + sum(realized P&L) — a figure reconstructed entirely from our
own permanent record, which no broker read can corrupt. On material
disagreement the mirror is flagged and left UNTOUCHED, and new BUYs are refused
(exits never are). An agreeing read clears it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import journal
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
    # Fallbacks used only when autonomy_config.json is unreadable. resting_stops
    # defaults OFF there deliberately: if we cannot read the config we cannot
    # know the account is the one these stops belong on, and an unreadable
    # config is not the moment to start writing orders the operator did not ask
    # for. The shipped config turns it on.
    "resting_stops": False,
    "out_of_band_lookback_hours": 96.0,
    "sync_suspect_tolerance": 0.35,
}

# Alpaca order statuses that mean "this order is live and holding shares".
_STOP_WORKING = ("new", "accepted", "held", "partially_filled", "pending_new",
                 "accepted_for_bidding", "calculated")

# Statuses that mean an order is DEAD at the broker with nothing to show for it.
# Used by the duplicate-client_order_id path: Alpaca client ids are unique
# FOREVER per account, including for orders that died unfilled, so a terminally
# rejected order squats on its id — resolving to it is not idempotency, it is a
# permanently wedged retry (see _resolve_duplicate_submit, audited 2026-08-05).
_TERMINAL_DEAD = ("rejected", "canceled", "expired")
# Bounded retry-suffix walk: -r2 .. -r9. Eight extra attempts per base id are
# plenty for transient rejections; a name the broker rejects every time (halted
# all day) ends the day dead either way, and forced-exit ids reset with the ET
# day so the walk starts fresh tomorrow.
_MAX_DUP_RETRY_SUFFIX = 9

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
            from tools.envload import load_env
            load_env()
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
                "demand_driver", "protective_stop")


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# broker_sync_suspect — the ledger cross-check
#
# 2026-07-17, twice, 103 seconds apart: a bad /v2/account read persisted
# cash_usd = 0 over a $9,819.48 ledger and rejected every BUY for four days.
# The old guard compared the incoming read against total_equity_usd — a field
# the FIRST bad read had already zeroed — so prior_equity was 0 and the second
# read sailed straight through. The reconstruction below is built only from
# starting capital and our own booked realizations, so no broker read can move
# it, and it still fires on read #2.
# --------------------------------------------------------------------------- #
def _starting_capital() -> float:
    try:
        cfg = json.loads((ROOT / "autonomy_config.json").read_text())
        return float(cfg["position_sizing"]["starting_capital_usd"])
    except Exception:
        return 0.0


def ledger_expected_equity() -> float | None:
    """Account equity implied by our OWN permanent record: starting capital,
    plus every price realization we have ever booked, plus only those dividends
    the event trail says were actually CREDITED to cash. None when the baseline
    is unknown — the cross-check then stays off rather than inventing a
    comparison.

    DIVIDENDS ARE COUNTED FROM THE EVENT TRAIL, NOT ASSUMED (fixed 2026-08-05).
    corporate_actions.apply_corporate_actions makes dividends METADATA-ONLY on
    the alpaca backend — Alpaca paper never credits the cash — and records that
    decision on every dividend history row as `cash_credited`. The old
    reconstruction added open-position dividend metadata and preferred the
    dividend-inclusive total_realized_pnl_usd on closed rows, so on this
    backend every dividend pushed `expected` ABOVE broker equity by exactly the
    amount the broker never paid: a systematic bias that walks the cross-check
    toward a false broker_sync_suspect (which then refuses every new BUY).
    Summing per-row PRICE-ONLY P&L plus only cash_credited dividend events
    matches what each backend actually did to cash, on any mixed history."""
    start = _starting_capital()
    if start <= 0:
        return None
    state = simulated_broker._load()
    total = start
    for h in state.get("history", []) or []:
        if h.get("type") == "dividend":
            # A row with no cash_credited flag predates the backend split —
            # written when the simulation owned cash and always credited, so
            # the absent-flag default is True.
            if h.get("cash_credited", True):
                total += _f(h.get("amount_usd"))
            continue
        if str(h.get("action", "")).upper() != "SELL_TO_CLOSE":
            continue
        if str(h.get("status", "")) != "filled":
            continue
        v = h.get("realized_pnl_usd")            # price-only, net of fees
        if v is None:
            # Legacy fallback: strip the dividend component out of the
            # dividend-inclusive total — dividends are the event trail's job.
            t = h.get("total_realized_pnl_usd")
            if t is None:
                continue
            v = _f(t) - _f(h.get("dividends_received_usd"))
        total += _f(v)
    return round(total, 2)


def sync_suspect() -> dict | None:
    """The active broker_sync_suspect record, or None when the mirror is trusted."""
    d = simulated_broker._load().get("broker_sync_suspect")
    return d if isinstance(d, dict) and d.get("active") else None


def _flag_sync_suspect(reason: str, expected, actual) -> None:
    """Raise the circuit breaker WITHOUT touching cash/equity/positions.

    Deliberately re-loads the mirror instead of writing the half-rebuilt state
    sync_mirror was assembling: the whole point is that a suspect read must not
    reach the ledger."""
    already = sync_suspect() is not None
    state = simulated_broker._load()
    state["broker_sync_suspect"] = {
        "active": True,
        "allows_new_buys": False,
        "reason": reason,
        "ledger_expected_equity": expected,
        "broker_equity": actual,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    simulated_broker._save(state)
    if not already:
        try:
            journal.log_rejection(
                {"ticker": "*", "action": "BROKER_SYNC"},
                ["RISK_EVENT_broker_sync_suspect", reason],
                "broker-sync")
        except Exception:
            pass
        print(f"  RISK EVENT: broker sync suspect — {reason}")


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
    # ---- corrupt-read guards, in order of strength ----
    # (1) LEDGER CROSS-CHECK. What the account must hold according to our own
    # booked history — a figure no broker read contributes to, and therefore the
    # only one a bad read cannot corrupt. A material disagreement is flagged and
    # the mirror is left EXACTLY as it was.
    prior_equity = round(float(state.get("total_equity_usd") or 0.0), 2)
    expected = ledger_expected_equity()
    tol = _f(_cfg().get("sync_suspect_tolerance", 0.35), 0.35) or 0.35
    if expected is not None and expected > 0:
        dev = abs(new_equity - expected) / expected
        if dev > tol:
            _flag_sync_suspect(
                f"broker equity ${new_equity:,.2f} disagrees with the ledger's "
                f"${expected:,.2f} by {dev * 100:.1f}% (tolerance {tol * 100:.0f}%)",
                expected, new_equity)
            return None
    # (2) Legacy zero-equity guard, still load-bearing when the baseline is
    # unknown: a paper account cannot drop to $0 with no closing trade.
    if new_equity <= 0 and prior_equity > 0:
        _flag_sync_suspect(
            f"zero-equity read (${new_equity:,.2f}) over a funded mirror "
            f"(${prior_equity:,.2f})", prior_equity, new_equity)
        return None
    # An agreeing read is the all-clear: this is a circuit breaker, not a latch.
    state.pop("broker_sync_suspect", None)
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
    """Persist ratcheted chandelier levels AND move the resting stop orders.

    Before this, a ratcheted trail lived only in the ledger while the broker
    still held the original level — the trail was fiction to the exchange."""
    changed = simulated_broker.update_trailing_stops(trailing)  # mirror metadata
    if trailing and _stop_cfg_on() and api_reachable():
        for t in trailing:
            try:
                ensure_protective_stop(str(t).upper(), reason="trail_ratchet")
            except Exception as e:
                print(f"  (trail re-arm failed for {t}: {e})")
    return changed


# --------------------------------------------------------------------------- #
# static order guards — these must run ABOVE the intent short-circuit
# --------------------------------------------------------------------------- #
def _validate_static(order: dict) -> tuple[str | None, dict]:
    """(rejection_status, extra_fields). None = the order may proceed."""
    action = str(order.get("action", "")).upper()
    # A halt must stop the system OPENING risk, never CLOSING it. Protective
    # sells bypass every guard here by design.
    if action == "SELL_TO_CLOSE":
        return None, {}
    if action != "BUY":
        return "rejected_unsupported_action", {}

    if sync_suspect():
        return "rejected_broker_sync_suspect", {}

    notional = round(_f(order.get("position_size_usd")), 2)
    if notional < 1.0:
        return "rejected_bad_notional", {}

    reachable = api_reachable()
    state = sync_mirror() if reachable else None
    if reachable and sync_suspect():   # the fresh read raised it
        return "rejected_broker_sync_suspect", {}
    if state is None:
        state = simulated_broker._load()
    if notional > _f(state.get("cash_usd")):
        return "rejected_insufficient_cash", {}

    if reachable:
        # BUYs never queue overnight: the brain priced NOW, not tomorrow's open.
        # Only checkable with a live clock — the executor re-validates at the
        # other end of the relay, where a clock exists.
        if not bool(_clock().get("is_open")):
            return "rejected_market_closed", {}
        if _cfg().get("price_guard", True):
            entry_max = order.get("entry_price_max")
            live = _live_last_trade(str(order.get("ticker", "")).upper())
            if entry_max is not None and live is not None and live > _f(entry_max):
                return "rejected_price_above_entry_max_live", {"live_price": live}
    return None, {}


def validate_order_static(order: dict) -> str | None:
    """Cash / notional / market-hours / live-price / sync-suspect guards.

    THE UNH FAILURE: place_order returned the queued intent BEFORE any of these
    ran, so a cloud-queued order carried ZERO validation at commit time and the
    broker rejected it hours later for insufficient cash on a fully funded
    account. They now run above the intent short-circuit AND again at execution
    time in scripts/execute_order_intents.py (after a fresh sync_mirror), because
    the ledger the queueing node validated against is hours old by then.

    Returns the rejection status string, or None when the order is acceptable."""
    return _validate_static(order)[0]


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
    """Atomic write of the cloud order queue — same reasoning as
    simulated_broker._save. clear_intents is a read-modify-write over this file, so
    a partial write here strands or duplicates real orders."""
    INTENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(INTENTS_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(blob, indent=2, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, INTENTS_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
def _resolve_duplicate_submit(pending: dict, body: dict, action: str) -> dict | None:
    """Resolve a 409/422 duplicate-client_order_id submit. Mutates and returns
    `pending` on success; None when the duplicate cannot be resolved (the
    caller records the dead order).

    THE HOLE THIS CLOSES (audited 2026-08-05). forced_exit_client_order_id keys
    a protective sell on ticker+ET-day+reason so two nodes reacting to the same
    breach collide here instead of both selling. But Alpaca client ids are
    unique FOREVER per account — including for orders that died unfilled — so
    when the broker TERMINALLY rejected the first attempt (halted name,
    wash-trade block, a DAY sell that expired at the close), every later tick
    resolved to that same dead order, place_order reported "submitted",
    readback parked it as resting, and the position sat STOPLESS for the rest
    of the trading day. Retrying under a DETERMINISTIC suffix (-r2, -r3, ...)
    preserves the cross-node collision per attempt — both nodes walk the same
    sequence and meet at the same next id — while a live or (partially) filled
    order still short-circuits exactly as before.

    Retries are RISK-REDUCING ONLY: a BUY keeps the old resolve-as-submitted
    behaviour even when its duplicate is dead, because re-submitting a stale
    BUY hours after its first attempt died would be OPENING risk the run never
    re-priced (readback then reports the honest terminal status, e.g.
    rejected_unfilled_canceled)."""
    base = str(pending["client_order_id"])

    def _dead_unfilled(o: dict) -> bool:
        return (str(o.get("status") or "") in _TERMINAL_DEAD
                and _f(o.get("filled_qty")) <= 0)

    existing = _get_order_by_client_id(base)
    if existing is None:
        return None
    if action != "SELL_TO_CLOSE" or not _dead_unfilled(existing):
        # True idempotency: a live, filled or partially-filled duplicate IS the
        # order this submit meant — resolve to it, never sell a second time.
        pending["alpaca_order_id"] = existing.get("id")
        pending["status"] = "submitted"
        return pending

    for n in range(2, _MAX_DUP_RETRY_SUFFIX + 1):
        cand = f"{base}-r{n}"
        prior = _get_order_by_client_id(cand)
        if prior is None:
            code, resp = _req("POST", "/v2/orders",
                              body={**body, "client_order_id": cand}, timeout=15)
            if code == 200 and isinstance(resp, dict) and resp.get("id"):
                pending["order_id"] = cand
                pending["client_order_id"] = cand
                pending["alpaca_order_id"] = resp["id"]
                pending["retry_of_client_order_id"] = base
                pending["status"] = "submitted"
                print(f"  (dead duplicate {base} — re-submitted as {cand})")
                return pending
            if code in (409, 422) and "client_order_id" in json.dumps(resp or {}):
                # Another node won the race onto this suffix between our GET
                # and POST — inspect what it placed instead of skipping it.
                prior = _get_order_by_client_id(cand)
            else:
                return None  # the broker rejected the retry itself — go loud
        if prior is not None and not _dead_unfilled(prior):
            pending["order_id"] = cand
            pending["client_order_id"] = cand
            pending["alpaca_order_id"] = prior.get("id")
            pending["retry_of_client_order_id"] = base
            pending["status"] = "submitted"
            return pending
        # this retry attempt died at the broker too — keep walking
    return None


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

    # THE GUARDS RUN FIRST — above the intent short-circuit. An order that
    # cannot be funded/priced/timed must never reach the cloud queue, because
    # nothing downstream re-derives that judgement from the run's context.
    bad, extra = _validate_static(order)
    if bad:
        pending.update(extra)
        pending["status"] = bad
        return _record_dead_order(pending)

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

    if action == "BUY":
        notional = round(_f(order.get("position_size_usd")), 2)
        body = _buy_body(ticker, notional, order, order_id)
    elif action == "SELL_TO_CLOSE":
        # STAND THE RESTING STOP DOWN FIRST. A resting sell stop HOLDS the
        # shares it covers (qty_available -> the uncovered tail), so without
        # this every discretionary exit is sized at a fraction of the position
        # — or rejected outright as rejected_no_position.
        try:
            _stand_down_protective_stop(ticker, reason="discretionary_sell")
            # WAIT FOR THE CANCEL TO PROPAGATE. Alpaca's DELETE is ASYNCHRONOUS:
            # the order goes pending_cancel and the held shares are only released
            # on canceled. Sizing the sell immediately read qty_available while the
            # stop still held the whole-share leg, so on a 5.34-share position with
            # a 5-share GTC stop the "full close" was submitted for 0.34 shares —
            # then booked by _apply_fill as a 6% PARTIAL, journaled as a filled
            # forced exit, and graded by exit_autopsy, while 94% of the position
            # stayed open and a fresh stop was armed over it. The ledger said the
            # stop was honoured. It was not.
            # The BUY path already knew cancels are async (it sleeps and re-reads);
            # this path did not.
            _await_shares_released(ticker)
        except Exception as e:
            print(f"  (stop stand-down failed for {ticker}: {e})")
        qty = _sell_qty(ticker, order.get("sell_fraction"))
        if qty is None or qty <= 0:
            # CRITICAL: a deterministic forced-exit client_order_id may already
            # have FILLED at the broker on an earlier tick. Recording
            # rejected_no_position under that same id poisons OOB ingest
            # (HPE 2026-08-19) and leaves a not_at_broker ghost. Adopt first.
            adopted = _try_adopt_filled_client_order(pending)
            if adopted is not None:
                return adopted
            pending["status"] = "rejected_no_position"
            try:
                pos = _position_in_mirror(ticker)
                if pos and pos.get("not_at_broker"):
                    reconcile_not_at_broker_ghosts(tickers=[ticker])
            except Exception as e:
                print(f"  (post-reject ghost reconcile for {ticker}: {e})")
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
        # duplicate client_order_id -> a submit under this id already exists.
        # Live/filled duplicates short-circuit (idempotent retry); a terminally
        # DEAD unfilled duplicate on a protective sell re-submits under a
        # deterministic -rN suffix — see _resolve_duplicate_submit for the
        # stopless-position failure that made "any duplicate == submitted"
        # unacceptable on the exit path.
        if _resolve_duplicate_submit(pending, body, action) is None:
            pending["status"] = f"rejected_submit_{code}"
            pending["broker_response"] = resp
            return _record_dead_order(pending)
    else:
        pending["status"] = f"rejected_submit_{code}"
        pending["broker_response"] = resp
        return _record_dead_order(pending)

    # stash for readback (and for reconcile() if the poll window expires) —
    # keyed on pending["order_id"], which the duplicate-retry path may have
    # moved to a -rN suffix; readback() polls whatever id it is handed, so the
    # stash key and the id at the broker must be the same string.
    state = simulated_broker._load()
    state.setdefault("pending_orders", {})[pending["order_id"]] = pending
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
    # Every existing reconcile() call site absorbs broker-side fills for free —
    # that is how the Actions executor and the 5-minute stop_watch tick pick up
    # a stop that fired at 03:00.
    try:
        done.extend(ingest_out_of_band_fills())
    except Exception as e:
        print(f"  (out-of-band ingestion failed: {e})")
    return done


# --------------------------------------------------------------------------- #
# resting protective stops
# --------------------------------------------------------------------------- #
def _stop_cfg_on() -> bool:
    return bool(_cfg().get("resting_stops", False))


def _position_in_mirror(ticker: str) -> dict | None:
    for p in simulated_broker._load().get("positions", []) or []:
        if str(p.get("ticker", "")).upper() == ticker:
            return p
    return None


def _broker_position(ticker: str) -> dict | None:
    code, ap = _req("GET", f"/v2/positions/{ticker}")
    return ap if code == 200 and isinstance(ap, dict) else None


def _working_stop_order(ticker: str, rec: dict | None) -> dict | None:
    """The stop order actually resting at the broker for this ticker, or None.

    The mirror's own record is checked first; the open-order sweep behind it is
    not redundant, because a stop we have FORGOTTEN about still holds shares —
    a fresh checkout or a restored ledger would otherwise write a second stop
    over the top of a live one and hold the position twice."""
    cid = (rec or {}).get("client_order_id")
    if cid:
        o = _get_order_by_client_id(str(cid))
        if o and o.get("status") in _STOP_WORKING:
            return o
    code, orders = _req("GET", "/v2/orders",
                        params={"status": "open", "limit": 500})
    if code == 200 and isinstance(orders, list):
        for o in orders:
            if not isinstance(o, dict):
                continue
            if str(o.get("symbol", "")).upper() == ticker \
                    and str(o.get("type")) == "stop" \
                    and str(o.get("side")) == "sell" \
                    and o.get("status") in _STOP_WORKING:
                return o
    return None


def _desired_stop_level(pos: dict) -> float | None:
    """Highest protective level the ledger asks for: the plan stop, ratcheted by
    the chandelier trail. Never the lower of the two."""
    levels = []
    plan = pos.get("plan") if isinstance(pos.get("plan"), dict) else {}
    for v in (plan.get("stop_loss"), pos.get("trailing_stop")):
        f = _f(v, 0.0)
        if f > 0:
            levels.append(f)
    return round(max(levels), 4) if levels else None


def _stop_shape(qty: float) -> tuple[float, str, float]:
    """(order qty, time_in_force, uncovered qty) for a position of `qty` shares.

    Alpaca accepts fractional quantities ONLY with time_in_force=day, and every
    entry here is notional by design — so a notional entry leaves a fractional
    position and a GTC stop on the whole quantity is rejected. Cover the
    WHOLE-SHARE portion GTC (what genuinely survives overnight and the weekend)
    and DISCLOSE the sub-share remainder rather than assume it is safe. Under
    one share there is no whole-share leg, so DAY is the only order the broker
    will take: session-only protection, re-armed each stop_watch tick.

    Derived from the CURRENT quantity ALONE (fixed 2026-08-05). This used to
    take the existing order's tif and pin "day" forever, so a position opened
    under one share (the live book's BKR, 0.995 sh, carries exactly this DAY
    stop) that later scaled past a whole share would have kept session-only
    protection for life: ensure_protective_stop's cancel-and-rewrite path only
    fires when the wanted tif differs from the current one, and the wanted tif
    was computed AS the current one. The desired stop shape is a function of
    what we hold now, never of what the last order happened to be — a DAY stop
    on a position that now has a whole-share leg upgrades to GTC on the next
    ensure call."""
    whole = float(int(qty))
    if whole >= 1.0:
        return whole, "gtc", round(qty - whole, 6)
    return round(qty, 6), "day", 0.0


def _submit_stop(ticker: str, qty: float, stop_price: float,
                 tif: str) -> dict | None:
    body = {"symbol": ticker, "side": "sell", "type": "stop",
            "time_in_force": tif, "qty": _fmt_qty(qty),
            "stop_price": f"{float(stop_price):.2f}",
            "client_order_id": f"EESTOP-{ticker}-{uuid.uuid4().hex[:10]}"}
    code, resp = _req("POST", "/v2/orders", body=body, timeout=15)
    if code == 200 and isinstance(resp, dict) and resp.get("id"):
        return resp
    return None


def _write_protective_stop(ticker: str, rec: dict | None) -> None:
    state = simulated_broker._load()
    for p in state.get("positions", []) or []:
        if str(p.get("ticker", "")).upper() == ticker:
            if rec is None:
                p.pop("protective_stop", None)
            else:
                p["protective_stop"] = rec
            simulated_broker._save(state)
            return


def _record_protective_stop(ticker: str, o: dict, qty: float, tif: str,
                            uncovered: float, level: float) -> dict:
    rec = {
        "status": "resting",
        "stop_price": round(float(level), 4),
        "qty": round(float(qty), 6),
        # The share fraction NO resting order can carry. Disclosed, never
        # silently treated as protected.
        "uncovered_qty": round(float(uncovered), 6),
        "session_only": tif == "day",
        "time_in_force": tif,
        "client_order_id": o.get("client_order_id"),
        "alpaca_order_id": o.get("id"),
        "armed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_protective_stop(ticker, rec)
    return rec


def _fail_protective_stop(ticker: str, level, qty: float, reason: str) -> dict:
    """A position with NO resting stop is a risk event, not a footnote."""
    rec = {
        "status": "FAILED",
        "stop_price": round(float(level), 4) if level else None,
        "qty": round(float(qty), 6),
        "uncovered_qty": round(float(qty), 6),
        "session_only": False,
        "time_in_force": None,
        "reason": reason or None,
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_protective_stop(ticker, rec)
    try:
        journal.log_rejection(
            {"ticker": ticker, "action": "PROTECTIVE_STOP",
             "stop_price": rec["stop_price"], "quantity": rec["qty"]},
            ["RISK_EVENT_protective_stop_not_armed",
             f"the broker refused every protective stop for {ticker} "
             f"({reason or 'no reason given'}) — the position is carrying "
             f"UNPROTECTED risk until a cycle or stop_watch tick re-arms it"],
            "protective-stop")
    except Exception:
        pass
    print(f"  RISK EVENT: protective stop NOT armed for {ticker} @ {level}")
    return rec


def _stand_down_protective_stop(ticker: str, *, reason: str = "") -> bool:
    """Cancel the resting stop so the shares are free to sell."""
    ticker = str(ticker).upper()
    if not api_reachable():
        return False
    pos = _position_in_mirror(ticker)
    rec = (pos or {}).get("protective_stop")
    o = _working_stop_order(ticker, rec if isinstance(rec, dict) else None)
    if o is None or not o.get("id"):
        return False
    _req("DELETE", f"/v2/orders/{o['id']}")
    if isinstance(rec, dict):
        _write_protective_stop(ticker, {
            **rec, "status": "stood_down", "stood_down_reason": reason or None,
            "stood_down_at": datetime.now(timezone.utc).isoformat()})
    return True


def _retire_protective_stop(ticker: str, *, reason: str = "") -> None:
    """Position is gone: cancel anything still resting and drop the record.
    NOT gated on the resting_stops config — cleaning up after a disabled
    feature is always correct."""
    ticker = str(ticker).upper()
    if not api_reachable():
        return
    pos = _position_in_mirror(ticker)
    rec = (pos or {}).get("protective_stop")
    o = _working_stop_order(ticker, rec if isinstance(rec, dict) else None)
    if o is not None and o.get("id"):
        _req("DELETE", f"/v2/orders/{o['id']}")
    if isinstance(rec, dict):
        _write_protective_stop(ticker, None)


def ensure_protective_stop(ticker: str, *, reason: str = "") -> dict | None:
    """Arm / re-size / ratchet the resting stop for one position.

    Called on every filled BUY (including scale-ins), after a sell_fraction
    partial, and from update_trailing_stops. Idempotent: re-running it against
    an already-correct stop touches nothing.

    RATCHET-ONLY, ENFORCED AT THE BROKER as well as in the ledger. A live
    protective stop is raised or left alone — never widened. The mirror's guard
    is not sufficient on its own: a bad write or a stale metadata replay can ask
    for a lower level, and widening a live stop is the one thing a trail must
    never do."""
    ticker = str(ticker).upper()
    if not _stop_cfg_on() or not api_reachable():
        return None

    pos = _position_in_mirror(ticker)
    if pos is None:
        _retire_protective_stop(ticker, reason=reason or "no_mirror_position")
        return None

    # Ghost / mirror-only: there are no shares at the exchange to protect.
    # Never stamp FAILED here — that marked protective_stops_armed DEAD and
    # blocked every new BUY while an HPE fractional remnant sat not_at_broker
    # after its broker fill was poisoned out of OOB ingest (2026-08-19).
    if pos.get("not_at_broker"):
        try:
            reconcile_not_at_broker_ghosts(tickers=[ticker])
        except Exception as e:
            print(f"  (ghost reconcile for {ticker} failed: {e})")
        pos = _position_in_mirror(ticker)
        if pos is None:
            return None
        if pos.get("not_at_broker"):
            # Still a ghost after repair — clear FAILED so the capability
            # probe is not held hostage; next sync/tick retries the write-off.
            _write_protective_stop(ticker, None)
            return None

    bp = _broker_position(ticker)
    if bp is None:
        # Single-position GET miss is NOT proof the shares are gone (transient
        # 5xx looks identical to 404 here). Clear a FAILED stamp so we do not
        # block the book, but do not write off — only sync_mirror's full
        # positions list may flip not_at_broker, and ghost reconcile then owns
        # the cleanup.
        _write_protective_stop(ticker, None)
        return None

    qty = round(_f(bp.get("qty")), 6)
    if qty <= 0:
        _retire_protective_stop(ticker, reason=reason or "flat")
        return None

    desired = _desired_stop_level(pos)
    if desired is None:
        # No plan stop and no trail: there is no level to protect at. exit_guard
        # already shouts about unstoppable positions; do not invent one here.
        return pos.get("protective_stop")

    rec = pos.get("protective_stop")
    existing = _working_stop_order(ticker, rec if isinstance(rec, dict) else None)

    if existing is not None:
        cur_price = round(_f(existing.get("stop_price")), 4)
        cur_qty = round(_f(existing.get("qty")), 6)
        cur_tif = str(existing.get("time_in_force") or "").lower()
        level = max(desired, cur_price)          # <- the ratchet
        want_qty, want_tif, uncovered = _stop_shape(qty)
        if want_tif == cur_tif:
            if abs(level - cur_price) < 5e-5 and abs(want_qty - cur_qty) < 1e-6:
                return _record_protective_stop(ticker, existing, want_qty,
                                               want_tif, uncovered, level)
            code, resp = _req("PATCH", f"/v2/orders/{existing['id']}",
                              body={"qty": _fmt_qty(want_qty),
                                    "stop_price": f"{level:.2f}"})
            if code == 200 and isinstance(resp, dict):
                return _record_protective_stop(ticker, resp, want_qty,
                                               want_tif, uncovered, level)
        # A replace the broker will not take (tif has to change, or the order
        # went terminal under us): cancel and rewrite — never at a lower level.
        _req("DELETE", f"/v2/orders/{existing['id']}")
        desired = level

    want_qty, want_tif, uncovered = _stop_shape(qty)
    o = _submit_stop(ticker, want_qty, desired, want_tif)
    if o is None and want_tif == "gtc":
        # GTC refused (the fractional rule, or anything else): a DAY stop on the
        # FULL position beats leaving it naked. Session-only, and it says so.
        o = _submit_stop(ticker, qty, desired, "day")
        want_qty, want_tif, uncovered = round(qty, 6), "day", 0.0
    if o is None:
        return _fail_protective_stop(ticker, desired, qty, reason)
    return _record_protective_stop(ticker, o, want_qty, want_tif,
                                   uncovered, desired)


def unprotected_positions() -> list[dict]:
    """Open broker-held positions with no resting stop. Mirror-only
    `not_at_broker` remnants are excluded — they have no shares at the exchange
    to protect (see reconcile_not_at_broker_ghosts)."""
    return [p for p in simulated_broker._load().get("positions", []) or []
            if not p.get("not_at_broker")
            and not (isinstance(p.get("protective_stop"), dict)
                     and p["protective_stop"].get("status") == "resting")]


def _maintain_protective_stop(ticker: str, action: str) -> None:
    """Post-fill stop maintenance: arm on a BUY, re-size on a partial, retire on
    a full close. Never lets a stop failure lose the fill it followed."""
    try:
        if action == "BUY" or _position_in_mirror(ticker) is not None:
            ensure_protective_stop(ticker, reason=f"{action.lower()}_fill")
        else:
            _retire_protective_stop(ticker, reason="position_closed")
    except Exception as e:
        print(f"  (protective stop maintenance failed for {ticker}: {e})")


# --------------------------------------------------------------------------- #
# not_at_broker ghost repair
# --------------------------------------------------------------------------- #
def _filled_history_ids(state: dict | None = None) -> set[str]:
    """Ids from history rows that actually realized — never rejected_*.

    Rejected rows must not suppress broker-fill ingest: a deterministic
    forced-exit client_order_id can be recorded as rejected_no_position AFTER
    the same id already filled at the broker, and counting that reject as
    'known' permanently orphans the fill.
    """
    state = state if state is not None else simulated_broker._load()
    known: set[str] = set()
    for h in state.get("history", []) or []:
        if str(h.get("status", "")).lower() != "filled":
            continue
        for k in ("client_order_id", "order_id", "alpaca_order_id"):
            if h.get(k):
                known.add(str(h[k]))
    return known


def _try_adopt_filled_client_order(pending: dict) -> dict | None:
    """If this client_order_id already filled at the broker, book it.

    Returns the applied fill dict, or None when there is nothing to adopt.
    Prevents rejected_no_position from being written under an id that already
    realized — the poison that hid HPE's fractional fill from OOB ingest.
    """
    cid = str(pending.get("client_order_id") or pending.get("order_id") or "")
    if not cid or not api_reachable():
        return None
    ao = _get_order_by_client_id(cid)
    if not isinstance(ao, dict) or str(ao.get("status")) != "filled":
        return None
    if _f(ao.get("filled_qty")) <= 0:
        return None
    oid = str(ao.get("id") or "")
    for h in simulated_broker._load().get("history") or []:
        if oid and str(h.get("alpaca_order_id") or "") == oid \
                and str(h.get("status", "")).lower() == "filled":
            return dict(h)
        if cid and str(h.get("client_order_id") or "") == cid \
                and str(h.get("status", "")).lower() == "filled":
            return dict(h)
    print(f"  (adopting already-filled broker order {cid} — not recording reject)")
    try:
        fill = _apply_fill(pending, ao)
    except Exception as e:
        print(f"  (adopt of {cid} failed: {e})")
        return None
    if fill and fill.get("status") == "filled":
        try:
            from execution import reconcile_runner
            reconcile_runner.record_fill(pending, fill)
        except Exception as e:
            print(f"  (adopt journal for {cid} failed: {e})")
        return fill
    return None


def _sweep_fractional_dust(ticker: str, *, parent_reason: str = "") -> dict | None:
    """Market-sell a sub-share leftover a GTC protective stop cannot cover.

    Alpaca refuses fractional GTC stops, so a whole-share stop fill routinely
    leaves 0 < qty < 1 at the broker. That dust used to sit until exit_guard
    fired a deterministic EEXIT id, race with reconcile, and spawn a
    not_at_broker ghost. Sweeping it here — immediately after the stop fill —
    closes the position before that failure mode can start.
    """
    ticker = str(ticker).upper()
    if not api_reachable():
        return None
    bp = _broker_position(ticker)
    if not isinstance(bp, dict):
        return None
    qty = round(_f(bp.get("qty")), 6)
    if qty <= 0 or qty >= 1.0 - 1e-9:
        return None
    # Free shares held by any lingering stop before sizing the sweep.
    try:
        _stand_down_protective_stop(ticker, reason="fractional_dust_sweep")
        _await_shares_released(ticker)
    except Exception as e:
        print(f"  (dust sweep stand-down for {ticker}: {e})")
    bp = _broker_position(ticker)
    if not isinstance(bp, dict):
        return None
    qty = round(_f(bp.get("qty")), 6)
    avail = round(_f(bp.get("qty_available") or bp.get("qty")), 6)
    if qty <= 0 or qty >= 1.0 - 1e-9 or avail <= 0:
        return None
    sell_qty = min(qty, avail)
    oid = f"EEDUST-{ticker}-{uuid.uuid4().hex[:10]}"
    body = {"symbol": ticker, "side": "sell", "type": "market",
            "time_in_force": "day", "qty": _fmt_qty(sell_qty),
            "client_order_id": oid}
    code, resp = _req("POST", "/v2/orders", body=body, timeout=15)
    if not (code == 200 and isinstance(resp, dict) and resp.get("id")):
        print(f"  (fractional dust sweep submit failed for {ticker}: "
              f"{code} {resp})")
        return None
    pending = {
        "ticker": ticker,
        "action": "SELL_TO_CLOSE",
        "order_id": oid,
        "client_order_id": oid,
        "alpaca_order_id": resp.get("id"),
        "proposal_id": "fractional-dust-sweep",
        "forced_exit_reason": "fractional_dust_after_stop",
        "parent_reason": parent_reason or None,
        "reference_price": _f(bp.get("current_price")) or None,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": "submitted",
    }
    # Poll briefly for the fill; reconcile() will finish it if we time out.
    ao = resp
    for _ in range(8):
        time.sleep(0.25)
        got = _get_order_by_client_id(oid)
        if isinstance(got, dict):
            ao = got
            st = str(ao.get("status") or "")
            if st == "filled" or (_f(ao.get("filled_qty")) > 0 and st in (
                    *_TERMINAL_DEAD, "done_for_day", "filled")):
                break
    if _f(ao.get("filled_qty")) <= 0:
        # Park for reconcile — do not lose the working order.
        st = simulated_broker._load()
        st.setdefault("pending_orders", {})[oid] = pending
        simulated_broker._save(st)
        print(f"  (fractional dust sweep for {ticker} resting — "
              f"reconcile will finish)")
        return None
    try:
        fill = _apply_fill(pending, ao)
    except Exception as e:
        print(f"  (fractional dust sweep apply for {ticker} failed: {e})")
        return None
    if fill and fill.get("status") == "filled":
        print(f"  DUST SWEEP {ticker} {fill.get('quantity')} "
              f"@ {fill.get('fill_price')} (after {parent_reason or 'stop'})")
        try:
            from execution import reconcile_runner
            reconcile_runner.record_fill(pending, fill)
        except Exception as e:
            print(f"  (dust sweep journal for {ticker} failed: {e})")
        return fill
    return None


def write_off_not_at_broker(ticker: str, *, reason: str = "",
                            mark_price: float | None = None) -> dict | None:
    """Remove a confirmed mirror-only remnant and book the realization.

    Cash is left untouched: the broker is authoritative and already reflects
    whatever happened to the shares. We only clear the ghost so capability
    probes and exit_guard stop thrashing on it, and we book realized P&L so
    ledger_expected_equity stays honest about the closed cost basis.
    """
    ticker = str(ticker).upper()
    state = simulated_broker._load()
    pos = next((p for p in state.get("positions", []) or []
                if str(p.get("ticker", "")).upper() == ticker), None)
    if pos is None:
        return None
    qty = round(_f(pos.get("quantity")), 6)
    if qty <= 0:
        state["positions"] = [p for p in state["positions"] if p is not pos]
        simulated_broker._save(state)
        return None
    avg_cost = _f(pos.get("avg_cost"))
    px = _f(mark_price)
    if px <= 0:
        px = _f(pos.get("last_price"))
    if px <= 0:
        px = avg_cost
    price_pnl = round((px - avg_cost) * qty, 2) if avg_cost else None
    oid = f"WRITEOFF-{ticker}-{uuid.uuid4().hex[:10]}"
    fill = {
        "ticker": ticker,
        "action": "SELL_TO_CLOSE",
        "order_id": oid,
        "client_order_id": oid,
        "proposal_id": "not-at-broker-writeoff",
        "status": "filled",
        "fill_price": round(px, 4),
        "quantity": qty,
        "notional_usd": round(qty * px, 2),
        "sell_fraction": 1.0,
        "avg_cost": avg_cost or None,
        "position_opened_at": pos.get("opened_at"),
        "entry_plan": pos.get("plan"),
        "entry_proposal_id": pos.get("proposal_id"),
        "realized_pnl_usd": price_pnl,
        "total_realized_pnl_usd": price_pnl,
        "dividends_received_usd": 0.0,
        "write_off": True,
        "not_at_broker_writeoff": True,
        "forced_exit_reason": reason or "not_at_broker_writeoff",
        "filled_at": datetime.now(timezone.utc).isoformat(),
        "fees_usd": {"commission": 0.0, "sec_fee": 0.0, "taf": 0.0,
                     "slippage_bps": 0.0, "effective_slippage_pct": 0.0},
    }
    state["positions"] = [p for p in state["positions"] if p is not pos]
    state.setdefault("history", []).append(fill)
    if state.get("positions"):
        state["total_equity_usd"] = round(
            float(state.get("cash_usd") or 0.0)
            + sum(p.get("market_value_usd", 0.0) for p in state["positions"]), 2)
    else:
        state["total_equity_usd"] = round(float(state.get("cash_usd") or 0.0), 2)
    simulated_broker._save(state)
    try:
        from execution import reconcile_runner
        reconcile_runner.record_fill(
            {k: fill.get(k) for k in
             ("ticker", "action", "order_id", "client_order_id",
              "proposal_id", "forced_exit_reason", "alpaca_order_id")},
            fill)
    except Exception as e:
        print(f"  (write-off journal for {ticker} failed: {e})")
    print(f"  WRITE-OFF {ticker} {qty} @ {px} ({reason or 'not_at_broker'})")
    return fill


def reconcile_not_at_broker_ghosts(
        *, tickers: list[str] | None = None,
        lookback_hours: float = 720.0) -> list[dict]:
    """Repair mirror-only positions whose broker shares already left.

    For each not_at_broker (or broker-flat) remnant:
      1. Sweep closed broker sells over an extended lookback and apply any
         fill the ordinary 96h OOB window / rejected-id poison missed.
      2. If the broker is still flat and the remnant remains, write it off.

    Returns the fills / write-offs applied. Fail-soft; never raises.
    """
    if not api_reachable():
        return []
    from execution import reconcile_runner

    state = simulated_broker._load()
    want = {str(t).upper() for t in (tickers or [])} if tickers else None
    # ONLY positions sync_mirror has already flagged. A lone
    # _broker_position 404/5xx must never write off a live seat.
    ghosts = []
    for p in state.get("positions", []) or []:
        t = str(p.get("ticker", "")).upper()
        if not t or not p.get("not_at_broker"):
            continue
        if want is not None and t not in want:
            continue
        ghosts.append(t)
    seen_t: set[str] = set()
    ghosts = [t for t in ghosts if not (t in seen_t or seen_t.add(t))]
    if not ghosts:
        return []

    after = (datetime.now(timezone.utc)
             - timedelta(hours=max(lookback_hours, 24.0))).isoformat()
    code, orders = _req("GET", "/v2/orders",
                        params={"status": "closed", "limit": 500,
                                "direction": "desc", "after": after})
    if code != 200 or not isinstance(orders, list):
        orders = []

    applied: list[dict] = []
    known = _filled_history_ids(state)
    seen_list = [str(x) for x in (state.get("ingested_broker_orders") or [])]
    seen = set(seen_list)
    pending_ids = set((state.get("pending_orders") or {}).keys())
    touched_memo = False

    for ao in orders:
        if not isinstance(ao, dict) or str(ao.get("status")) != "filled":
            continue
        if str(ao.get("side", "")).lower() != "sell":
            continue
        symbol = str(ao.get("symbol", "")).upper()
        if symbol not in ghosts:
            continue
        qty = _f(ao.get("filled_qty"))
        if qty <= 0:
            continue
        cid = str(ao.get("client_order_id") or "")
        oid = str(ao.get("id") or "")
        key = cid or oid
        if not key:
            continue
        if cid and cid in pending_ids:
            continue
        if (cid and cid in known) or (oid and oid in known):
            continue
        if cid in seen or oid in seen:
            continue
        if reconcile_runner._already_journaled(cid or None, oid or None):
            continue
        # Skip if alpaca_order_id already booked (filled rows only)
        if oid and any(str(h.get("alpaca_order_id") or "") == oid
                       and str(h.get("status", "")).lower() == "filled"
                       for h in (simulated_broker._load().get("history") or [])):
            continue

        price = round(_f(ao.get("filled_avg_price")), 4)
        pending = {
            "ticker": symbol,
            "action": "SELL_TO_CLOSE",
            "order_id": key,
            "client_order_id": key,
            "alpaca_order_id": oid or None,
            "proposal_id": "ghost-reconcile",
            "out_of_band": True,
            "ghost_reconcile": True,
            "forced_exit_reason": ("resting_stop_breached"
                                   if str(ao.get("type")) == "stop"
                                   else "broker_side_fill"),
            "reference_price": price,
            "submitted_at": (ao.get("submitted_at") or ao.get("created_at")
                             or ao.get("filled_at")),
        }
        try:
            fill = _apply_fill(pending, ao)
        except Exception as e:
            print(f"  (ghost reconcile apply {key} failed: {e})")
            continue
        seen.add(key)
        seen_list.append(key)
        touched_memo = True
        if fill and fill.get("status") == "filled":
            print(f"  GHOST REPAIR {symbol} {fill.get('quantity')} "
                  f"@ {fill.get('fill_price')} (missed broker fill adopted)")
            try:
                reconcile_runner.record_fill(pending, fill)
            except Exception as e:
                print(f"  (ghost repair journal failed: {e})")
            applied.append(fill)
            known = _filled_history_ids()

    if touched_memo:
        st = simulated_broker._load()
        st["ingested_broker_orders"] = seen_list[-500:]
        simulated_broker._save(st)

    # Confirm flat via the FULL positions list (same authority sync_mirror
    # uses), then write off anything still mirrored.
    _acct, poss = _fetch_account_positions()
    if poss is None:
        return applied
    at_broker = {str(p.get("symbol", "")).upper()
                 for p in (poss or []) if p.get("symbol")}
    for t in ghosts:
        pos = _position_in_mirror(t)
        if pos is None:
            continue
        if t in at_broker:
            continue
        fill = write_off_not_at_broker(
            t, reason="ghost_reconcile_broker_flat")
        if fill:
            applied.append(fill)

    return applied


# --------------------------------------------------------------------------- #
# out-of-band fill ingestion
# --------------------------------------------------------------------------- #
def ingest_out_of_band_fills() -> list[tuple[dict, dict]]:
    """Absorb fills that happened at the broker with nobody watching.

    A resting stop that fires at 03:00 produces no journal line, no closed
    trade, and a PHANTOM POSITION that exit_guard re-fires on forever. This
    sweeps the closed-order list and replays unseen SELLS through the ordinary
    _apply_fill path, so they become indistinguishable from a synchronous exit
    (same mirror update, same history record, same exit-autopsy grading via
    reconcile_runner.record_fill).

    Idempotency is layered, because double-counting a realization corrupts the
    permanent track record: (1) the order is already in the mirror's history or
    pending set, (2) it is in the persisted ingested-ids set, (3) the JOURNAL
    already carries it — the cross-node guard, since each node has its own
    checkout of the mirror until git syncs them.

    Unknown broker-side BUYS are FLAGGED and never journaled: sync_mirror
    already adopts the stray position, and inventing a trade record for it would
    corrupt cost basis and every closed-trade figure derived from it."""
    if not api_reachable():
        return []
    from execution import reconcile_runner   # local: avoids an import cycle

    lookback = _f(_cfg().get("out_of_band_lookback_hours", 96), 96.0) or 96.0
    after = (datetime.now(timezone.utc) - timedelta(hours=lookback)).isoformat()
    code, orders = _req("GET", "/v2/orders",
                        params={"status": "closed", "limit": 500,
                                "direction": "desc", "after": after})
    if code != 200 or not isinstance(orders, list):
        return []

    state = simulated_broker._load()
    pending_ids = set((state.get("pending_orders") or {}).keys())
    # ONLY filled history rows suppress ingest. A later rejected_no_position
    # reuse of the same deterministic client_order_id (EEXIT-TICKER-day-reason)
    # used to land in `known` and permanently hide the broker fill that had
    # already succeeded under that id — leaving a not_at_broker ghost that
    # poisoned protective_stops_armed (HPE 2026-08-19).
    known = _filled_history_ids(state)
    # Insertion-ordered dedupe memo (fixed 2026-08-05). Kept as a LIST + set
    # pair because the trim below must evict the OLDEST ingested ids first: the
    # previous `sorted(seen)[-500:]` trimmed lexicographically, so once the memo
    # overflowed it could evict the most RECENT ids (exactly the ones still
    # inside the lookback window that the next sweep will see again) while
    # keeping ancient high-sorting ids forever.
    seen_list = [str(x) for x in (state.get("ingested_broker_orders") or [])]
    seen = set(seen_list)

    done: list[tuple[dict, dict]] = []
    flagged: list[tuple[str, str]] = []
    touched = False

    for ao in orders:
        if not isinstance(ao, dict) or str(ao.get("status")) != "filled":
            continue
        qty = _f(ao.get("filled_qty"))
        if qty <= 0:
            continue
        cid = str(ao.get("client_order_id") or "")
        oid = str(ao.get("id") or "")
        key = cid or oid
        if not key:
            continue
        if cid in pending_ids:
            continue                                   # reconcile() owns it
        if (cid and cid in known) or (oid and oid in known):
            continue
        if cid in seen or oid in seen:
            continue
        if reconcile_runner._already_journaled(cid or None, oid or None):
            continue

        side = str(ao.get("side", "")).lower()
        symbol = str(ao.get("symbol", "")).upper()
        if side == "buy":
            flagged.append((symbol, key))
            seen.add(key)
            seen_list.append(key)
            touched = True
            continue
        if side != "sell" or not symbol:
            continue

        price = round(_f(ao.get("filled_avg_price")), 4)
        pending = {
            "ticker": symbol,
            "action": "SELL_TO_CLOSE",
            "order_id": key,
            "client_order_id": key,
            "alpaca_order_id": oid or None,
            "proposal_id": "out-of-band",
            "out_of_band": True,
            "forced_exit_reason": ("resting_stop_breached"
                                   if str(ao.get("type")) == "stop"
                                   else "broker_side_fill"),
            "reference_price": price,
            "submitted_at": (ao.get("submitted_at") or ao.get("created_at")
                             or ao.get("filled_at")),
        }
        seen.add(key)
        seen_list.append(key)
        touched = True
        try:
            fill = _apply_fill(pending, ao)
        except Exception as e:
            print(f"  (out-of-band fill {key} failed to apply: {e})")
            continue
        if fill and fill.get("status") == "filled":
            print(f"  OUT-OF-BAND FILL {symbol} {fill.get('quantity')} "
                  f"@ {fill.get('fill_price')} ({pending['forced_exit_reason']})")
            done.append((pending, fill))

    for symbol, key in flagged:
        try:
            journal.log_rejection(
                {"ticker": symbol, "action": "BUY", "broker_order": key},
                ["RISK_EVENT_unrecognized_broker_side_buy",
                 f"a BUY for {symbol} filled at the broker that this system "
                 f"never placed — flagged, NEVER journaled: fabricating a trade "
                 f"record would corrupt cost basis and the closed-trade math"],
                "out-of-band")
        except Exception:
            pass
        print(f"  RISK EVENT: unrecognized broker-side BUY {symbol} ({key})")

    if touched:
        st = simulated_broker._load()
        st["ingested_broker_orders"] = seen_list[-500:]   # oldest dropped first
        simulated_broker._save(st)

    # Extended repair for any remnant the 96h window / rejected-id poison
    # still left behind. Fail-soft: a ghost repair error must not lose the
    # fills this sweep already booked.
    try:
        for fill in reconcile_not_at_broker_ghosts():
            done.append((
                {k: fill.get(k) for k in
                 ("ticker", "action", "order_id", "client_order_id",
                  "proposal_id", "forced_exit_reason", "alpaca_order_id",
                  "out_of_band", "ghost_reconcile")},
                fill))
    except Exception as e:
        print(f"  (not_at_broker ghost reconcile failed: {e})")
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


def _await_shares_released(ticker: str, timeout_s: float = 6.0,
                           interval_s: float = 0.5) -> bool:
    """Block until a cancelled protective stop has released its shares.

    Returns True when qty_available reaches the full position quantity, False on
    timeout. A False here means the caller is about to size a sell against a
    partially-held position, so it is reported loudly rather than swallowed — an
    under-sized "full close" is the failure mode this exists to prevent.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, ap = _req("GET", f"/v2/positions/{ticker}")
        if code != 200 or not isinstance(ap, dict):
            return False
        try:
            qty = abs(float(ap.get("qty") or 0.0))
            avail = abs(float(ap.get("qty_available") or 0.0))
        except (TypeError, ValueError):
            return False
        if qty <= 0:
            return False
        if avail >= qty - 1e-9:
            return True
        time.sleep(interval_s)
    print(f"  !! {ticker}: shares still held {timeout_s:.0f}s after stop stand-down "
          f"— sell would be UNDERSIZED; refusing to size against a partial")
    return False


# Below this share count the whole-share rounding error is too large to accept, so
# the entry falls back to a notional market order. At a ~$1,400 position (1% risk /
# 7% stop on this book) 4 shares means <=12% granularity, which covers 69% of the
# universe; the remainder are names above ~$350 where 1-3 shares would be 25-50% off
# target, and three names above $1,600 that a whole-share order could not buy at all.
MIN_WHOLE_SHARES_FOR_LIMIT = 4


def _buy_body(ticker: str, notional: float, order: dict, order_id: str) -> dict:
    """Entry order body — a whole-share limit with an attached stop when possible.

    WHY TWO SHAPES. Alpaca's fractional/notional orders are time_in_force=DAY ONLY
    and accept no order_class, so a notional entry can neither carry a
    broker-managed stop nor be limit-priced by whole shares. A WHOLE-SHARE qty
    order can. What the limit+OTO shape actually buys us:

      * this is NOT a resting entry, despite the GTC tif (doc corrected
        2026-08-05 — an earlier version of this comment claimed the entry
        "executes when price reaches the level with no run in progress", which
        readback() has never allowed): the BUY branch of readback() CANCELS any
        unfilled remainder when the ~90s poll window expires, so in practice the
        entry is marketable-within-the-window-or-canceled. The GTC tif exists to
        satisfy the order_class/qty rules and to make the attached stop leg
        durable, not to leave a working entry behind after the run ends;
      * `order_class: "oto"` attaches the protective stop AT ENTRY, armed by the
        exchange — but only a fill INSIDE the poll window enjoys it; an entry
        canceled unfilled leaves nothing behind, stop leg included;
      * for a fill inside the window, GTC means the attached stop survives
        overnight and weekends for the ENTIRE position, with no fractional
        remainder left uncovered and no arming race;
      * entry_price_max is enforced by the exchange rather than by our own guard —
        which is worth noting, because that guard read a key nobody set and had
        never once fired.

    OTO rather than `bracket` deliberately: a bracket demands a take-profit leg, and
    targets in this system are explicitly milestones, not tripwires ("holding past it
    is allowed and encouraged"). A binding take-profit would contradict that and cap
    the right tail the strategy depends on.

    Falls back to today's notional market order below MIN_WHOLE_SHARES_FOR_LIMIT, so
    high-priced names stay tradeable. The chosen shape is recorded on the order so a
    position is never ambiguous about what is protecting it.
    """
    limit = _f(order.get("entry_price_max"))
    stop = _f(((order.get("plan") or {}).get("stop_loss")))
    shares = int(notional // limit) if (limit and limit > 0) else 0

    if (limit and stop and 0 < stop < limit
            and shares >= MIN_WHOLE_SHARES_FOR_LIMIT):
        order["entry_mode"] = "whole_share_limit_oto"
        order["entry_shares"] = shares
        return {
            "symbol": ticker, "side": "buy", "type": "limit",
            "time_in_force": "gtc", "qty": str(shares),
            "limit_price": f"{limit:.2f}",
            "order_class": "oto",
            "stop_loss": {"stop_price": f"{stop:.2f}"},
            "client_order_id": order_id,
        }

    why = ("no entry_price_max" if not limit
           else "no plan stop" if not stop
           else f"only {shares} whole share(s) at {limit:.2f}")
    order["entry_mode"] = "notional_market"
    order["entry_fallback_reason"] = why
    return {"symbol": ticker, "side": "buy", "type": "market",
            "time_in_force": "day", "notional": f"{notional:.2f}",
            "client_order_id": order_id}


def _sell_qty(ticker: str, sell_fraction) -> float | None:
    """Quantity for a sell: live position qty_available pro-rata by fraction."""
    code, ap = _req("GET", f"/v2/positions/{ticker}")
    if code != 200 or not isinstance(ap, dict):
        return None
    try:
        qty_total = abs(float(ap.get("qty") or 0.0))
        avail = float(ap.get("qty_available") or ap.get("qty") or 0.0)
    except (TypeError, ValueError):
        return None
    if avail <= 0:
        return None
    # A FULL close must never be silently converted into a partial. If shares are
    # still held by a working order, selling `avail` books a fraction and reports
    # success. Refuse instead: the caller surfaces it and the position keeps its
    # protection, which is strictly safer than a phantom exit.
    frac_req = float(sell_fraction or 1.0) if sell_fraction is not None else 1.0
    if frac_req >= 1.0 and qty_total > 0 and avail < qty_total - 1e-9:
        print(f"  !! {ticker}: full close requested but only {avail} of {qty_total} "
              f"available (shares still held by a working order) — refusing")
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

    # ALREADY-APPLIED GUARD. Two reconcilers run on schedule against SEPARATE
    # checkouts of this ledger: the Actions executor (*/15 weekdays) on a fresh
    # origin clone, and the local stop_watch tick (every 5 min) on the persistent
    # Mac checkout. Both call run_reconcile(); neither pushes at the same moment.
    # The three documented idempotency layers -- history ids, the persisted
    # ingested_broker_orders set, and reconcile_runner._already_journaled -- are ALL
    # per-checkout, and the module's own comment concedes the journal is only shared
    # "after git syncs them". An overnight stop fill was therefore applied on both
    # nodes and merged on rebase into TWO history records for one broker order.
    #
    # That is not merely cosmetic. ledger_expected_equity() sums realized P&L across
    # SELL_TO_CLOSE rows, so a double-counted realization shifts the reconstruction;
    # past sync_suspect_tolerance it trips broker_sync_suspect and refuses every new
    # BUY until a human intervenes. Closed-trade stats are corrupted either way.
    #
    # The broker's order id is globally unique and identical on both nodes, so it is
    # the one key that survives the merge. Checking it here covers every caller --
    # reconcile, the intent executor, and out-of-band ingestion alike.
    broker_oid = str(ao.get("id") or "")
    if broker_oid:
        for h in (state.get("history") or []):
            if str(h.get("alpaca_order_id") or "") == broker_oid:
                print(f"  (fill {broker_oid[:12]} already applied — not double-booking)")
                state["pending_orders"].pop(pending.get("order_id"), None)
                simulated_broker._save(state)
                return dict(h)

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
        avg_cost_source = None
        if avg_cost <= 0:
            # MIRROR-MISSING POSITION (audited 2026-08-05). An out-of-band sell
            # of a position the mirror lost used to book avg_cost 0 and
            # realized_pnl_usd None — a row ledger_expected_equity can only
            # skip, so the realization went permanently missing from the
            # reconstruction and the expected-vs-broker gap drifted toward a
            # false broker_sync_suspect. The entry usually survives in history
            # even when the position record did not: the most recent filled BUY
            # row carries position_avg_cost_after (blended across scale-ins).
            # Recover the basis from there, LABEL the row so nobody mistakes it
            # for first-class data, and only fall back to None when the entry
            # genuinely never touched this ledger. (A split between that BUY
            # and now would leave the backfilled basis unscaled — accepted:
            # this path only runs on an already-degraded mirror, and a labeled
            # approximate basis beats an invisible realization.)
            for h in reversed(state.get("history") or []):
                if str(h.get("ticker", "")).upper() == ticker \
                        and str(h.get("action", "")).upper() == "BUY" \
                        and h.get("status") == "filled":
                    backfill = _f(h.get("position_avg_cost_after")) \
                        or _f(h.get("fill_price"))
                    if backfill > 0:
                        avg_cost = backfill
                        avg_cost_source = "history_backfill"
                    break
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
        if avg_cost_source:
            fill["avg_cost_source"] = avg_cost_source

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

    state.setdefault("pending_orders", {}).pop(pending["order_id"], None)
    state["history"].append(fill)
    if state.get("positions"):
        state["total_equity_usd"] = round(
            float(state.get("cash_usd") or 0.0)
            + sum(p.get("market_value_usd", 0.0) for p in state["positions"]), 2)
    simulated_broker._save(state)

    # The resting stop follows the position: armed on entry, re-sized on a
    # scale-in or a sell_fraction partial, retired on a full close.
    _maintain_protective_stop(ticker, action)

    # After a protective STOP fill, immediately market-sell any sub-share
    # leftover the GTC stop could not cover. Skipping this is what left the
    # HPE 0.11 remnant to become a not_at_broker ghost.
    if action == "SELL_TO_CLOSE" and fill.get("status") == "filled":
        reason = str(pending.get("forced_exit_reason") or "")
        is_stop = (reason == "resting_stop_breached"
                   or str(ao.get("type") or "").lower() == "stop")
        # Never recurse from a dust sweep into another dust sweep.
        if is_stop and reason != "fractional_dust_after_stop" \
                and not pending.get("dust_sweep"):
            try:
                dust = _sweep_fractional_dust(
                    ticker, parent_reason=reason or "stop_fill")
                if dust:
                    fill["dust_sweep"] = {
                        "quantity": dust.get("quantity"),
                        "fill_price": dust.get("fill_price"),
                        "order_id": dust.get("order_id"),
                    }
            except Exception as e:
                print(f"  (fractional dust sweep after {ticker} stop failed: {e})")
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
