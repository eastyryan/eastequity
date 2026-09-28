"""Protective-stop fixes of 2026-09-28 — each test names the incident it pins.

  * NOW, 2026-09-28: the broker held 0.791986698 shares; every stop was sized
    round(qty, 6) = 0.791987 — MORE than held — and Alpaca refused buy_fill,
    manual_rearm and every stop_watch_tick re-arm. The journal said
    'no reason given' because the HTTP status and message were discarded.
  * A refused modify fell through to cancel-then-resubmit, so a refused
    resubmit left the position with ZERO stops.
  * The ledger kept calling an expired DAY stop 'resting', so nothing re-armed
    it and the capability probe reported the book protected.

Offline: the REST layer is the scripted double from test_resting_stops, made
STRICT about quantity the way the real broker is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from execution import alpaca_broker as ab  # noqa: E402
from execution import simulated_broker as sb  # noqa: E402
from test_resting_stops import FakeAlpaca  # noqa: E402
from test_resting_stops import api as _base_api  # noqa: E402,F401

NOW_QTY = "0.791986698"


class StrictAlpaca(FakeAlpaca):
    """FakeAlpaca plus the two broker rules the NOW incident tripped over:
    a sell may not exceed qty_available, and a replace may not exceed the
    position. `stop_filter(body)` can refuse any stop submit (code, message)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.events: list[tuple[str, str]] = []
        self.reject_patch = None          # (code, message) to refuse PATCHes
        self.stop_filter = None

    def _pos(self, sym):
        return next((p for p in self.positions if p["symbol"] == sym), None)

    def __call__(self, method, path, *, params=None, body=None, timeout=10):
        if path.startswith("/v2/orders/") and method == "DELETE":
            self.events.append(("DELETE", path.rsplit("/", 1)[1]))
        if path.startswith("/v2/orders/") and method == "PATCH":
            self.events.append(("PATCH", path.rsplit("/", 1)[1]))
            if self.reject_patch:
                return self.reject_patch
            oid = path.rsplit("/", 1)[1]
            o = next((o for o in self.orders.values() if o["id"] == oid), None)
            p = self._pos(o["symbol"]) if o else None
            if p and float((body or {}).get("qty", 0)) > float(p["qty"]) + 1e-12:
                return 403, {"code": 40310000,
                             "message": "qty must be <= position qty"}
        return super().__call__(method, path, params=params, body=body,
                                timeout=timeout)

    def _submit(self, body):
        if body.get("type") == "stop":
            self.events.append(("POST", f"{body['qty']}/{body['time_in_force']}"))
            if self.stop_filter:
                verdict = self.stop_filter(body)
                if verdict:
                    self.submits.append(dict(body))
                    return verdict
            p = self._pos(body["symbol"])
            if p and float(body["qty"]) > float(p["qty_available"]) + 1e-12:
                self.submits.append(dict(body))
                return 403, {"code": 40310000, "message":
                             f"insufficient qty available for order (requested: "
                             f"{body['qty']}, available: {p['qty_available']})"}
        return super()._submit(body)

    def add_stop(self, symbol, qty, stop_price, tif, cid, status="new"):
        self._seq += 1
        o = {"id": f"srv-{self._seq}", "client_order_id": cid, "status": status,
             "symbol": symbol, "side": "sell", "type": "stop", "qty": str(qty),
             "stop_price": str(stop_price), "time_in_force": tif,
             "filled_qty": "0"}
        self.orders[cid] = o
        if status == "new":
            p = self._pos(symbol)
            p["qty_available"] = str(round(float(p["qty_available"])
                                           - float(qty), 9))
        return o


@pytest.fixture
def api(_base_api, monkeypatch):
    strict = StrictAlpaca()
    monkeypatch.setattr(ab, "_req", strict)
    return strict


def _seed(ticker="NOW", qty=0.791987, plan_stop=125.0, trail=125.0579,
          protective_stop=None, avg=131.0):
    state = sb._load()
    pos = {"ticker": ticker, "quantity": qty, "avg_cost": avg,
           "opened_at": "2026-09-25T15:00:00+00:00",
           "plan": {"stop_loss": plan_stop, "target_price": avg * 1.3,
                    "holding_horizon_days": 30},
           "trailing_stop": trail}
    if protective_stop:
        pos["protective_stop"] = protective_stop
    state["positions"] = [pos]
    sb._save(state)


def _pos(ticker="NOW"):
    return next(p for p in sb._load()["positions"] if p["ticker"] == ticker)


def _journal_blob(tmp_path):
    return "".join(f.read_text() for f in
                   (tmp_path / "journal" / "rejected").glob("*.jsonl"))


# --------------------------------------------------------------------------- #
# 1. quantity: never more shares than the broker holds
# --------------------------------------------------------------------------- #
def test_floor_qty_never_rounds_up():
    assert ab._floor_qty("0.791986698") == 0.791986698
    assert ab._floor_qty(0.791986698) == 0.791986698
    assert ab._floor_qty("0.7919866989") == 0.791986698     # truncated, not rounded
    assert ab._floor_qty(round(0.791986698, 6)) == 0.791987  # the old bug, for contrast
    assert ab._floor_qty(None) == 0.0 and ab._floor_qty("garbage") == 0.0
    assert ab._fmt_qty(ab._floor_qty(NOW_QTY)) == NOW_QTY


def test_stop_shape_for_the_now_position_does_not_exceed_holdings():
    q, tif, uncovered = ab._stop_shape(float(NOW_QTY))
    assert tif == "day" and uncovered == 0.0
    assert q <= float(NOW_QTY)
    assert ab._fmt_qty(q) == NOW_QTY                         # exact, not 0.791987
    assert ab._stop_shape(5.37) == (5.0, "gtc", 0.37)        # unchanged contract


def test_new_stop_is_sized_to_the_exact_broker_quantity(api, tmp_path):
    """THE NOW INCIDENT. The strict double refuses 0.791987 exactly as Alpaca
    did; the stop must go in at 0.791986698 and rest."""
    api.set_position("NOW", float(NOW_QTY), 131.0, 131.0)
    api.positions[0]["qty"] = api.positions[0]["qty_available"] = NOW_QTY
    _seed()

    rec = ab.ensure_protective_stop("NOW", reason="stop_watch_tick")

    stops = [b for b in api.submits if b.get("type") == "stop"]
    assert [b["qty"] for b in stops] == [NOW_QTY]
    assert all(float(b["qty"]) <= float(NOW_QTY) for b in stops)
    assert rec["status"] == "resting" and rec["time_in_force"] == "day"
    assert rec["qty"] == float(NOW_QTY)
    assert len(api.working_stops()) == 1


def test_the_live_now_stop_is_recognised_as_already_armed(api):
    """The production state at the time of the fix: cba5b806, sell stop
    0.791986698 @ 125.09 DAY, recorded 'resting' (with the old 6-dp qty) in the
    ledger. A stop_watch tick must touch NOTHING — no PATCH, no cancel, no
    duplicate — and just re-record it."""
    api.set_position("NOW", float(NOW_QTY), 131.0, 131.0)
    api.positions[0]["qty"] = api.positions[0]["qty_available"] = NOW_QTY
    live = api.add_stop("NOW", NOW_QTY, 125.09, "day", "EESTOP-NOW-08716806ef")
    _seed(protective_stop={"status": "resting", "stop_price": 125.09,
                           "qty": 0.791987, "time_in_force": "day",
                           "client_order_id": "EESTOP-NOW-08716806ef",
                           "alpaca_order_id": live["id"]})

    rec = ab.ensure_protective_stop("NOW", reason="stop_watch_tick")

    assert api.events == [], f"must not modify the working stop: {api.events}"
    assert rec["status"] == "resting"
    assert rec["client_order_id"] == "EESTOP-NOW-08716806ef"
    assert rec["qty"] == float(NOW_QTY) and rec["stop_price"] == 125.09
    assert len(api.working_stops()) == 1


# --------------------------------------------------------------------------- #
# 2. refusals are journaled with the broker's own words
# --------------------------------------------------------------------------- #
def test_refusal_http_status_and_message_reach_the_journal(api, tmp_path):
    api.set_position("NOW", float(NOW_QTY), 131.0, 131.0)
    api.stop_filter = lambda body: (422, {"code": 42210000,
                                          "message": "stop price too close"})
    _seed()

    rec = ab.ensure_protective_stop("NOW", reason="manual_rearm")

    assert rec["status"] == "FAILED"
    assert rec["broker_http_status"] == 422
    assert rec["broker_message"] == "stop price too close"
    blob = _journal_blob(tmp_path)
    assert "RISK_EVENT_protective_stop_not_armed" in blob
    assert "alpaca HTTP 422: stop price too close" in blob
    line = json.loads(blob.strip().splitlines()[-1])
    assert line["proposal"]["broker_refusals"][0]["http_status"] == 422


def test_a_stop_armed_by_the_other_node_is_adopted_not_failed(api, tmp_path):
    """Box and Actions stop-watch can race to re-arm the same expired DAY stop.
    The loser's submit is refused because the winner's stop HOLDS the shares —
    that is a protected position, not a risk event."""
    api.set_position("NOW", float(NOW_QTY), 131.0, 131.0)

    def other_node_wins(body):
        api.stop_filter = None
        api.add_stop("NOW", NOW_QTY, 125.09, "day", "EESTOP-NOW-othernode")
        return 403, {"message": "insufficient qty available for order"}
    api.stop_filter = other_node_wins
    _seed()

    rec = ab.ensure_protective_stop("NOW", reason="stop_watch_tick")

    assert rec["status"] == "resting"
    assert rec["client_order_id"] == "EESTOP-NOW-othernode"
    assert len(api.working_stops()) == 1
    assert "RISK_EVENT" not in _journal_blob(tmp_path)


# --------------------------------------------------------------------------- #
# 3. replace safety: never zero stops because a replace was refused
# --------------------------------------------------------------------------- #
def test_refused_replace_keeps_the_old_stop(api, tmp_path):
    api.set_position("NVDA", 5.37, 100.0, 110.0)
    old = api.add_stop("NVDA", 5, 90.0, "gtc", "EESTOP-NVDA-old")
    _seed("NVDA", qty=5.37, plan_stop=90.0, trail=104.25, avg=100.0,
          protective_stop={"status": "resting", "stop_price": 90.0, "qty": 5.0,
                           "time_in_force": "gtc",
                           "client_order_id": "EESTOP-NVDA-old",
                           "alpaca_order_id": old["id"]})
    api.reject_patch = (422, {"message": "replace rejected by test"})

    rec = ab.ensure_protective_stop("NVDA", reason="trail_ratchet")

    assert ("PATCH", old["id"]) in api.events
    assert not [e for e in api.events if e[0] == "DELETE"], \
        "a refused replace must NOT cancel the working stop"
    assert not [e for e in api.events if e[0] == "POST"]
    working = api.working_stops()
    assert len(working) == 1 and working[0]["client_order_id"] == "EESTOP-NVDA-old"
    assert rec["status"] == "resting"
    assert rec["stop_price"] == 90.0          # the truth, not the wished-for 104.25
    assert rec["replace_refused"]["http_status"] == 422
    blob = _journal_blob(tmp_path)
    assert "PROTECTIVE_STOP_REPLACE_REFUSED" in blob
    assert "replace rejected by test" in blob


def test_tif_change_submits_the_replacement_before_cancelling(api):
    """DAY -> GTC upgrade with free shares: the new stop is accepted FIRST and
    only then is the old one cancelled."""
    api.set_position("BKR", 1.5, 40.0, 42.0)
    old = api.add_stop("BKR", 0.5, 38.0, "day", "EESTOP-BKR-old")
    _seed("BKR", qty=1.5, plan_stop=38.0, trail=None, avg=40.0,
          protective_stop={"status": "resting", "stop_price": 38.0, "qty": 0.5,
                           "time_in_force": "day",
                           "client_order_id": "EESTOP-BKR-old",
                           "alpaca_order_id": old["id"]})

    rec = ab.ensure_protective_stop("BKR", reason="scale_in")

    kinds = [e[0] for e in api.events]
    assert kinds == ["POST", "DELETE"], api.events
    assert rec["status"] == "resting" and rec["time_in_force"] == "gtc"
    assert [o["client_order_id"] for o in api.working_stops()] \
        == [rec["client_order_id"]]


def test_refused_rewrite_rolls_back_to_the_old_stop(api, tmp_path):
    """The old stop holds every share, so the replacement can only go in after a
    cancel; the broker then refuses every new shape. The old shape is re-armed —
    the position is never left with zero stops."""
    api.set_position("BKR", 1.5, 40.0, 42.0)
    old = api.add_stop("BKR", 1.5, 38.0, "day", "EESTOP-BKR-old")
    _seed("BKR", qty=1.5, plan_stop=38.0, trail=39.0, avg=40.0,
          protective_stop={"status": "resting", "stop_price": 38.0, "qty": 1.5,
                           "time_in_force": "day",
                           "client_order_id": "EESTOP-BKR-old",
                           "alpaca_order_id": old["id"]})
    # Refuse anything that is not the old shape (1.5 DAY @ 38.00).
    api.stop_filter = lambda b: None if (b["qty"] == "1.5"
                                         and b["time_in_force"] == "day"
                                         and b["stop_price"] == "38.00") \
        else (422, {"message": "refused by test"})

    rec = ab.ensure_protective_stop("BKR", reason="scale_in")

    working = api.working_stops()
    assert len(working) == 1, "exactly one stop must be working after the rollback"
    assert working[0]["qty"] == "1.5" and working[0]["stop_price"] == "38.00"
    assert rec["status"] == "resting" and rec["stop_price"] == 38.0
    assert "PROTECTIVE_STOP_REPLACE_ROLLED_BACK" in _journal_blob(tmp_path)


# --------------------------------------------------------------------------- #
# 4. ledger truth: 'resting' in the ledger is not a stop at the broker
# --------------------------------------------------------------------------- #
def _expired_setup(api):
    api.set_position("NOW", float(NOW_QTY), 131.0, 131.0)
    api.positions[0]["qty"] = api.positions[0]["qty_available"] = NOW_QTY
    dead = api.add_stop("NOW", NOW_QTY, 125.09, "day", "EESTOP-NOW-yesterday",
                        status="expired")
    _seed(protective_stop={"status": "resting", "stop_price": 125.09,
                           "qty": float(NOW_QTY), "time_in_force": "day",
                           "client_order_id": "EESTOP-NOW-yesterday",
                           "alpaca_order_id": dead["id"]})


def test_expired_resting_stop_is_rearmed(api):
    _expired_setup(api)

    rec = ab.ensure_protective_stop("NOW", reason="stop_watch_tick")

    assert rec["status"] == "resting"
    assert rec["client_order_id"] != "EESTOP-NOW-yesterday"
    assert rec["rearmed_from"]["broker_status"] == "expired"
    working = api.working_stops()
    assert len(working) == 1 and working[0]["qty"] == NOW_QTY


def test_verify_downgrades_an_expired_resting_record(api):
    _expired_setup(api)
    assert ab.unprotected_positions() == [], "precondition: ledger claims armed"

    naked = ab.unprotected_positions(verify_broker=True)

    assert [p["ticker"] for p in naked] == ["NOW"]
    assert _pos()["protective_stop"]["status"] == "expired"
    assert _pos()["protective_stop"]["broker_status"] == "expired"


def test_rearm_protective_stops_repairs_every_position(api):
    _expired_setup(api)

    out = ab.rearm_protective_stops(reason="slot_run")

    assert out["checked"] == ["NOW"] and not out["failed"]
    assert len(api.working_stops()) == 1
    assert _pos()["protective_stop"]["status"] == "resting"


def test_broker_facade_rearm_is_a_noop_on_simulation(monkeypatch):
    from execution import broker
    monkeypatch.setenv("EE_BROKER", "simulation")
    assert broker.rearm_protective_stops(reason="slot_run") == {}
