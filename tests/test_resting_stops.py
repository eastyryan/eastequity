"""Resting broker-side stops, out-of-band fill ingestion, relay validation and
the broker_sync_suspect guard.

Each test names the failure it exists to prevent. The four incidents behind
this file:

  * The single closed stop in the ledger filled at 389.75 against a 408 stop —
    4.5% through — because the "stop" was a float in state/portfolio.json that
    only got evaluated when a cycle ran (scripts/stop_watch.py:10-13 measured
    the blind windows: ~2h intraday, ~17.5h overnight, ~65.5h over a weekend).
  * A broker-side fill was invisible to reconcile(): no journal line, no closed
    trade, and a phantom position exit_guard re-fires on forever.
  * The UNH failure: place_order returned the queued intent BEFORE any guard
    ran, so a cloud-queued order carried zero validation at commit time.
  * 2026-07-17, twice, 103 seconds apart: a bad /v2/account read persisted
    cash_usd = 0 over a $9,819.48 ledger and rejected every BUY for four days.

Everything is offline — the REST layer is a scripted double.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import journal as _journal  # noqa: E402
from execution import alpaca_broker as ab  # noqa: E402
from execution import reconcile_runner  # noqa: E402
from execution import simulated_broker as sb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# scripted Alpaca double — orders, positions, replace/cancel, closed-order list
# --------------------------------------------------------------------------- #
class FakeAlpaca:
    def __init__(self, *, is_open=True, cash=10000.0, positions=None):
        self.is_open = is_open
        self.cash = cash
        self.positions = list(positions or [])
        self.orders: dict[str, dict] = {}     # client_order_id -> order
        self.submits: list[dict] = []
        self.patches: list[tuple[str, dict]] = []
        self.cancels: list[str] = []
        self.reject_stop_submits = False      # simulate a broker refusing stops
        self.reject_gtc = False               # simulate the fractional/GTC rule
        self.autofill_market = None           # {"filled_qty","filled_avg_price"}
        self._seq = 0

    # -- helpers used by tests ------------------------------------------------
    def stop_orders(self):
        return [o for o in self.orders.values() if o.get("type") == "stop"]

    def working_stops(self):
        return [o for o in self.stop_orders() if o["status"] == "new"]

    def fill_stop(self, cid, price):
        o = self.orders[cid]
        o.update({"status": "filled", "filled_qty": o["qty"],
                  "filled_avg_price": str(price),
                  "filled_at": "2026-07-18T20:15:00Z"})
        return o

    def set_position(self, symbol, qty, avg_cost, price):
        self.positions = [p for p in self.positions if p["symbol"] != symbol]
        if qty > 0:
            self.positions.append({
                "symbol": symbol, "qty": str(qty), "qty_available": str(qty),
                "avg_entry_price": str(avg_cost), "current_price": str(price),
                "market_value": str(round(qty * price, 2))})

    # -- transport ------------------------------------------------------------
    def __call__(self, method, path, *, params=None, body=None, timeout=10):
        if path == "/v2/clock":
            return 200, {"is_open": self.is_open}
        if path == "/v2/account":
            eq = self.cash + sum(float(p["market_value"]) for p in self.positions)
            return 200, {"cash": str(self.cash), "equity": str(eq)}
        if path == "/v2/positions" and method == "GET":
            return 200, list(self.positions)
        if path.startswith("/v2/positions/") and method == "GET":
            sym = path.rsplit("/", 1)[1]
            for p in self.positions:
                if p["symbol"] == sym:
                    return 200, p
            return 404, {"message": "position does not exist"}
        if path == "/v2/orders" and method == "GET":
            want = (params or {}).get("status")
            out = [o for o in self.orders.values()
                   if want != "closed" or o["status"] in
                   ("filled", "canceled", "expired", "rejected")]
            return 200, out
        if path == "/v2/orders" and method == "POST":
            return self._submit(body)
        if path == "/v2/orders:by_client_order_id":
            o = self.orders.get((params or {}).get("client_order_id"))
            return (200, o) if o else (404, None)
        if path.startswith("/v2/orders/") and method == "PATCH":
            oid = path.rsplit("/", 1)[1]
            for o in self.orders.values():
                if o["id"] == oid:
                    self.patches.append((oid, dict(body or {})))
                    o.update(body or {})
                    return 200, o
            return 404, None
        if path.startswith("/v2/orders/") and method == "DELETE":
            oid = path.rsplit("/", 1)[1]
            self.cancels.append(oid)
            for o in self.orders.values():
                if o["id"] == oid and o["status"] != "filled":
                    o["status"] = "canceled"
                    sym = o["symbol"]
                    for p in self.positions:  # cancelling releases held shares
                        if p["symbol"] == sym:
                            p["qty_available"] = p["qty"]
            return 204, None
        return 404, None

    def _submit(self, body):
        self.submits.append(dict(body))
        if body.get("type") == "stop":
            if self.reject_stop_submits:
                return 422, {"message": "stop rejected by test"}
            if self.reject_gtc and body.get("time_in_force") == "gtc":
                return 422, {"message": "time_in_force gtc not supported "
                                        "for fractional qty"}
        self._seq += 1
        cid = body["client_order_id"]
        order = {"id": f"srv-{self._seq}", "client_order_id": cid,
                 "status": "new", "filled_qty": "0", **body}
        if body.get("type") == "stop":
            # A resting sell stop HOLDS the shares it covers.
            for p in self.positions:
                if p["symbol"] == body["symbol"]:
                    p["qty_available"] = str(
                        round(float(p["qty"]) - float(body["qty"]), 6))
        elif self.autofill_market:
            order.update({"status": "filled", **self.autofill_market})
        self.orders[cid] = order
        return 200, order


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Wired broker: tmp mirror, tmp journal, resting stops ON, no network."""
    state = tmp_path / "portfolio.json"
    state.write_text(json.dumps({
        "cash_usd": 10000.0, "total_equity_usd": 10000.0,
        "positions": [], "pending_orders": {}, "history": [],
    }))
    monkeypatch.setattr(sb, "STATE_FILE", state)
    monkeypatch.setattr(ab, "INTENTS_FILE", tmp_path / "order_intents.json")
    monkeypatch.setattr(reconcile_runner, "ROOT", tmp_path)
    monkeypatch.setattr(_journal, "JOURNAL", tmp_path / "journal")
    monkeypatch.delenv("EE_BROKER_FORCE_INTENT", raising=False)
    monkeypatch.setenv("EE_BROKER_FORCE_DIRECT", "1")
    ab._probe_cache.clear()
    monkeypatch.setattr(ab, "_cfg", lambda: {**ab._DEFAULTS,
                                             "poll_interval_seconds": 0.01,
                                             "buy_poll_timeout_seconds": 0.05,
                                             "sell_poll_timeout_seconds": 0.05,
                                             "resting_stops": True})
    monkeypatch.setattr(ab.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ab, "_live_last_trade", lambda t: None)
    monkeypatch.setattr(ab, "_starting_capital", lambda: 0.0)  # cross-check off
    fake = FakeAlpaca()
    monkeypatch.setattr(ab, "_req", fake)
    return fake


def entry_buy(api, monkeypatch, ticker="NVDA", qty=5.37, price=100.0, stop=90.0):
    """One filled BUY end to end, leaving a FRACTIONAL position — which is what
    a notional entry actually produces, and the whole reason the stop leg has to
    reason about whole shares vs GTC. Returns (order, fill)."""
    api.autofill_market = {"filled_qty": str(qty), "filled_avg_price": str(price),
                           "filled_at": "2026-07-17T14:00:00Z"}
    base = FakeAlpaca.__call__

    def wrapper(method, path, **kw):
        code, b = base(api, method, path, **kw)
        if path == "/v2/orders" and method == "POST" \
                and (kw.get("body") or {}).get("side") == "buy":
            api.set_position(ticker, qty, price, price)
            api.cash = round(api.cash - qty * price, 2)
        return code, b

    monkeypatch.setattr(ab, "_req", wrapper)
    order = ab.place_order({
        "ticker": ticker, "action": "BUY", "position_size_usd": round(qty * price, 2),
        "reference_price": price, "entry_price_max": price * 1.05,
        "proposal_id": "run-buy", "plan": {"stop_loss": stop, "target_price": 130.0,
                                           "holding_horizon_days": 30}})
    api.autofill_market = None
    fill = ab.readback(order["order_id"])
    return order, fill


def _pos(ticker="NVDA"):
    return next((p for p in sb._load()["positions"]
                 if p["ticker"] == ticker), None)


# --------------------------------------------------------------------------- #
# 1. resting stops
# --------------------------------------------------------------------------- #
def test_a_notional_entry_never_carries_an_order_class(api, monkeypatch):
    """DESIGN CONTRACT, still binding on the FALLBACK path.

    Alpaca rejects bracket/OCO/OTO alongside `notional`, and cannot PATCH a notional
    order — so a notional entry's stop could never be ratcheted by the chandelier
    trail even if it were accepted. Protection on this path is therefore a SEPARATE
    stop submitted after the fill.

    CONTRACT NARROWED 2026-07-19: this was previously asserted of EVERY entry,
    because every entry was notional. Whole-share entries can and now do carry an
    OTO stop leg — see test_a_whole_share_entry_rests_with_its_stop_attached. The
    assertion below still governs whenever the notional fallback is taken (a name
    priced too high for >=4 whole shares, or a proposal missing its entry ceiling
    or plan stop).
    """
    entry_buy(api, monkeypatch)
    entry = next(b for b in api.submits if b["side"] == "buy")
    if "notional" in entry:
        assert "order_class" not in entry
        assert "take_profit" not in entry and "stop_loss" not in entry
        assert entry["type"] == "market"
    else:
        # took the whole-share path; that shape is asserted in its own test
        assert entry["type"] == "limit" and entry["order_class"] == "oto"


def test_filled_buy_arms_a_resting_stop_at_the_plan_level(api, monkeypatch):
    """THE POINT OF ALL THIS: after a BUY fills there is a REAL stop order at
    the broker, so the 17.5h overnight and 65.5h weekend windows are enforced
    by the exchange instead of by whether a cycle happens to run."""
    entry_buy(api, monkeypatch, qty=5.37, price=100.0, stop=90.0)

    stops = api.working_stops()
    assert len(stops) == 1, "a filled BUY must leave exactly one resting stop"
    s = stops[0]
    assert s["side"] == "sell" and s["type"] == "stop"
    assert float(s["stop_price"]) == 90.0
    rec = _pos()["protective_stop"]
    assert rec["status"] == "resting" and rec["stop_price"] == 90.0


def test_whole_share_portion_rests_gtc_and_the_tail_is_disclosed(api, monkeypatch):
    """Alpaca allows fractional quantities ONLY with time_in_force=day, so a GTC
    stop on the whole 5.37-share position is impossible. Cover the 5 whole
    shares GTC (that is what actually survives the weekend) and DISCLOSE the
    0.37 that no resting order can carry — never silently assume it is safe."""
    entry_buy(api, monkeypatch, qty=5.37, price=100.0, stop=90.0)

    s = api.working_stops()[0]
    assert s["time_in_force"] == "gtc"
    assert float(s["qty"]) == 5.0
    rec = _pos()["protective_stop"]
    assert rec["uncovered_qty"] == pytest.approx(0.37)
    assert rec["session_only"] is False


def test_sub_one_share_position_falls_back_to_a_day_stop(api, monkeypatch):
    """Under one share there is no whole-share leg, so the only order Alpaca
    will accept is DAY. That is session-only protection and must say so."""
    entry_buy(api, monkeypatch, ticker="MELI", qty=0.42, price=2000.0, stop=1800.0)

    s = api.working_stops()[0]
    assert s["time_in_force"] == "day" and float(s["qty"]) == pytest.approx(0.42)
    assert _pos("MELI")["protective_stop"]["session_only"] is True


def test_gtc_refusal_degrades_to_day_rather_than_leaving_it_naked(api, monkeypatch):
    """If the broker refuses GTC for any reason, a DAY stop beats no stop."""
    api.reject_gtc = True
    entry_buy(api, monkeypatch, qty=5.37, price=100.0, stop=90.0)

    assert [b["time_in_force"] for b in api.submits if b.get("type") == "stop"] \
        == ["gtc", "day"]
    assert _pos()["protective_stop"]["time_in_force"] == "day"


def test_trail_ratchet_raises_the_resting_stop(api, monkeypatch):
    """exit_guard._chandelier_stop (execution/exit_guard.py:40-53) ratchets the
    trail and persists via broker.update_trailing_stops. Before this, the new
    level lived ONLY in the ledger while the broker still held the original —
    the trail was fiction to the exchange."""
    entry_buy(api, monkeypatch, qty=5.37, price=100.0, stop=90.0)

    assert ab.update_trailing_stops({"NVDA": 104.25}) == 1

    assert api.patches, "the resting order must be replaced, not just the ledger"
    assert float(api.patches[-1][1]["stop_price"]) == 104.25
    assert _pos()["protective_stop"]["stop_price"] == 104.25
    assert float(api.working_stops()[0]["stop_price"]) == 104.25


def test_a_resting_stop_is_never_moved_down(api, monkeypatch):
    """RATCHET-ONLY, ENFORCED TWICE. simulated_broker.update_trailing_stops
    already refuses to lower a stored level, so this drives the SECOND guard
    directly: the mirror is edited behind its back to ask for a lower stop, and
    the order resting at the broker must still not widen. Widening a live stop
    is the one thing a trail must never do."""
    entry_buy(api, monkeypatch, qty=5.37, price=100.0, stop=90.0)
    ab.update_trailing_stops({"NVDA": 104.25})
    assert float(api.working_stops()[0]["stop_price"]) == 104.25
    patches_before = len(api.patches)
    cancels_before = len(api.cancels)

    # Corrupt the ledger's trail downward (what a bad write / stale metadata
    # replay looks like) and re-arm: the desired level is now the plan's 90.
    state = sb._load()
    state["positions"][0]["trailing_stop"] = 91.0
    sb._save(state)
    ab.ensure_protective_stop("NVDA", reason="test_downward")

    assert len(api.patches) == patches_before, "must not PATCH a stop lower"
    assert len(api.cancels) == cancels_before, "must not cancel-and-rewrite lower"
    assert float(api.working_stops()[0]["stop_price"]) == 104.25
    assert _pos()["protective_stop"]["stop_price"] == 104.25


def test_failure_to_arm_is_a_loud_risk_event(api, monkeypatch, tmp_path):
    """A position with NO resting stop must be visible, not silent: journaled,
    stamped FAILED on the position, and listed by unprotected_positions()."""
    api.reject_stop_submits = True
    entry_buy(api, monkeypatch, qty=5.37, price=100.0, stop=90.0)

    rec = _pos()["protective_stop"]
    assert rec["status"] == "FAILED"
    assert ab.unprotected_positions()[0]["ticker"] == "NVDA"

    lines = (tmp_path / "journal" / "rejected").glob("*.jsonl")
    blob = "".join(f.read_text() for f in lines)
    assert "RISK_EVENT_protective_stop_not_armed" in blob


def test_scale_in_resizes_the_resting_stop(api, monkeypatch):
    """A scale-in merges at blended cost and grows the position; a stop still
    sized to the original lot would leave the add unprotected."""
    entry_buy(api, monkeypatch, qty=5.0, price=100.0, stop=90.0)
    assert float(api.working_stops()[0]["qty"]) == 5.0

    entry_buy(api, monkeypatch, qty=9.0, price=110.0, stop=90.0)  # now 9 shares

    working = api.working_stops()
    assert len(working) == 1, "the superseded stop must be cancelled, not orphaned"
    assert float(working[0]["qty"]) == 9.0


def test_partial_exit_resizes_the_resting_stop(api, monkeypatch):
    """sell_fraction banks part of a runner. The resting stop must shrink to
    what is left — an oversized stop would try to sell shares we no longer own."""
    entry_buy(api, monkeypatch, qty=10.0, price=100.0, stop=90.0)
    base = FakeAlpaca.__call__

    def wrapper(method, path, **kw):
        code, b = base(api, method, path, **kw)
        body = kw.get("body") or {}
        if path == "/v2/orders" and method == "POST" and body.get("side") == "sell" \
                and body.get("type") == "market":
            api.orders[body["client_order_id"]].update(
                {"status": "filled", "filled_qty": body["qty"],
                 "filled_avg_price": "120.0"})
            api.set_position("NVDA", 5.0, 100.0, 120.0)
        return code, b

    monkeypatch.setattr(ab, "_req", wrapper)
    order = ab.place_order({"ticker": "NVDA", "action": "SELL_TO_CLOSE",
                            "reference_price": 120.0, "sell_fraction": 0.5,
                            "proposal_id": "run-partial"})
    fill = ab.readback(order["order_id"])

    assert fill["status"] == "filled" and fill["quantity"] == 5.0
    working = api.working_stops()
    assert len(working) == 1 and float(working[0]["qty"]) == 5.0


def test_a_discretionary_sell_stands_the_resting_stop_down_first(api, monkeypatch):
    """A resting sell stop HOLDS the shares it covers, so qty_available drops to
    the uncovered tail. Without cancelling it first, every exit would be sized
    at a fraction of the position — or rejected as rejected_no_position."""
    entry_buy(api, monkeypatch, qty=10.0, price=100.0, stop=90.0)
    held = next(p for p in api.positions if p["symbol"] == "NVDA")
    assert float(held["qty_available"]) == 0.0, "stop is holding the shares"

    base = FakeAlpaca.__call__

    def wrapper(method, path, **kw):
        code, b = base(api, method, path, **kw)
        body = kw.get("body") or {}
        if path == "/v2/orders" and method == "POST" and body.get("side") == "sell" \
                and body.get("type") == "market":
            api.orders[body["client_order_id"]].update(
                {"status": "filled", "filled_qty": body["qty"],
                 "filled_avg_price": "88.0"})
            api.set_position("NVDA", 0, 100.0, 88.0)
        return code, b

    monkeypatch.setattr(ab, "_req", wrapper)
    order = ab.place_order({"ticker": "NVDA", "action": "SELL_TO_CLOSE",
                            "reference_price": 88.0, "proposal_id": "run-exit",
                            "forced_exit_reason": "stop_loss_breached"})
    fill = ab.readback(order["order_id"])

    assert fill["status"] == "filled"
    assert fill["quantity"] == 10.0, "the whole position must be sellable"
    assert api.working_stops() == [], "a closed position keeps no resting stop"


# --------------------------------------------------------------------------- #
# 2. out-of-band fill ingestion
# --------------------------------------------------------------------------- #
def _fire_the_resting_stop(api, price=89.4):
    """The overnight event this whole subsystem exists for."""
    cid = next(o["client_order_id"] for o in api.working_stops())
    api.fill_stop(cid, price)
    sym = api.orders[cid]["symbol"]
    api.set_position(sym, 0, 0, 0)
    api.cash = round(api.cash + float(api.orders[cid]["qty"]) * price, 2)
    return cid


def test_broker_side_stop_fill_is_absorbed(api, monkeypatch):
    """A stop that fires at 03:00 produces no journal line, no closed trade, and
    a PHANTOM POSITION that exit_guard re-fires on forever. Ingestion turns it
    into an ordinary recorded exit."""
    entry_buy(api, monkeypatch, qty=10.0, price=100.0, stop=90.0)
    cid = _fire_the_resting_stop(api, price=89.4)

    done = ab.ingest_out_of_band_fills()

    assert len(done) == 1
    order, fill = done[0]
    assert order["action"] == "SELL_TO_CLOSE"
    assert order["forced_exit_reason"] == "resting_stop_breached"
    assert order["out_of_band"] is True
    assert fill["status"] == "filled" and fill["quantity"] == 10.0
    assert fill["fill_price"] == 89.4
    assert fill["realized_pnl_usd"] == pytest.approx((89.4 - 100.0) * 10)
    # ...and the phantom position is gone
    assert _pos() is None
    assert any(f.get("client_order_id") == cid for f in sb._load()["history"])


def test_ingestion_is_idempotent(api, monkeypatch):
    """Ingesting the same fill twice would double-count realized P&L into the
    permanent track record. Three independent guards must make it impossible."""
    entry_buy(api, monkeypatch, qty=10.0, price=100.0, stop=90.0)
    _fire_the_resting_stop(api, price=89.4)

    first = ab.ingest_out_of_band_fills()
    second = ab.ingest_out_of_band_fills()
    third = ab.ingest_out_of_band_fills()

    assert len(first) == 1
    assert second == [] and third == []
    sells = [f for f in sb._load()["history"]
             if f.get("action") == "SELL_TO_CLOSE" and f.get("status") == "filled"]
    assert len(sells) == 1


def test_ingestion_defers_to_a_journal_line_from_another_node(api, monkeypatch, tmp_path):
    """Cross-node dedupe: each node has its own checkout of the mirror until git
    syncs them, so the journal is the shared record. A fill another node already
    journaled must not be absorbed again here."""
    entry_buy(api, monkeypatch, qty=10.0, price=100.0, stop=90.0)
    cid = _fire_the_resting_stop(api, price=89.4)

    tf = tmp_path / "journal" / "trades" / "2026-07-18.jsonl"
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(json.dumps({"order": {"client_order_id": cid}}) + "\n")

    assert ab.ingest_out_of_band_fills() == []


def test_already_journaled_matches_the_alpaca_order_id_too(tmp_path, monkeypatch):
    """Extended dedupe: an out-of-band fill is discovered from the ORDER LIST,
    so it can arrive with a client id this system never issued. The broker's own
    order id has to count as a match."""
    monkeypatch.setattr(reconcile_runner, "ROOT", tmp_path)
    tf = tmp_path / "journal" / "trades" / "2026-07-18.jsonl"
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(json.dumps(
        {"order": {"client_order_id": "EESTOP-X-1", "alpaca_order_id": "srv-77"}}) + "\n")

    assert reconcile_runner._already_journaled("EESTOP-X-1")
    assert reconcile_runner._already_journaled(None, "srv-77")
    assert not reconcile_runner._already_journaled("EESTOP-X-2", "srv-99")


def test_partial_broker_side_fill_leaves_a_correct_remainder(api, monkeypatch):
    """The GTC leg only covers whole shares, so a fired stop can leave a
    fractional remainder. That remainder must survive with its entry metadata
    and get a fresh stop — not be dropped, and not become a phantom."""
    entry_buy(api, monkeypatch, qty=5.37, price=100.0, stop=90.0)
    cid = next(o["client_order_id"] for o in api.working_stops())
    api.fill_stop(cid, 89.4)                      # 5 whole shares sold
    api.set_position("NVDA", 0.37, 100.0, 89.4)
    api.cash = round(api.cash + 5 * 89.4, 2)

    done = ab.ingest_out_of_band_fills()

    assert len(done) == 1 and done[0][1]["quantity"] == 5.0
    remaining = _pos()
    assert remaining is not None
    assert remaining["quantity"] == pytest.approx(0.37)
    assert remaining["opened_at"] and remaining["plan"]["stop_loss"] == 90.0
    assert float(api.working_stops()[0]["qty"]) == pytest.approx(0.37)


def test_reconcile_sweeps_out_of_band_fills(api, monkeypatch):
    """Wiring: every existing reconcile() call site must absorb broker-side
    fills without changing — that is how scripts/execute_order_intents.py and
    the 5-minute stop_watch tick pick them up."""
    entry_buy(api, monkeypatch, qty=10.0, price=100.0, stop=90.0)
    _fire_the_resting_stop(api, price=89.4)

    done = ab.reconcile()

    assert len(done) == 1
    assert done[0][0]["forced_exit_reason"] == "resting_stop_breached"


def test_an_unknown_broker_buy_is_flagged_not_journaled(api, monkeypatch, tmp_path):
    """sync_mirror already adopts a stray position; inventing a trade record for
    it would corrupt cost basis and the closed-trade math. Flag, never absorb."""
    api.orders["MANUAL-1"] = {
        "id": "srv-manual", "client_order_id": "MANUAL-1", "symbol": "AAPL",
        "side": "buy", "type": "market", "status": "filled",
        "filled_qty": "3", "filled_avg_price": "200.0",
        "filled_at": "2026-07-18T15:00:00Z"}

    assert ab.ingest_out_of_band_fills() == []
    blob = "".join(f.read_text() for f in
                   (tmp_path / "journal" / "rejected").glob("*.jsonl"))
    assert "RISK_EVENT_unrecognized_broker_side_buy" in blob


# --------------------------------------------------------------------------- #
# 3. guards above the intent short-circuit (the UNH failure)
# --------------------------------------------------------------------------- #
def test_intent_mode_rejects_an_unfundable_buy_instead_of_queueing_it(api, monkeypatch):
    """THE UNH SHAPE: place_order returned the queued intent at :311-318 BEFORE
    the notional/cash/price guards at :320+, so a cloud-queued order carried
    ZERO validation at commit time and the broker rejected it hours later for
    insufficient cash on a fully funded account."""
    monkeypatch.setenv("EE_BROKER_FORCE_INTENT", "1")
    monkeypatch.delenv("EE_BROKER_FORCE_DIRECT", raising=False)
    ab._probe_cache.clear()

    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 999999.0,
                            "reference_price": 100.0, "proposal_id": "run-x"})

    assert order["status"] == "rejected_insufficient_cash"
    assert ab.queued_intents() == [], "an invalid order must never reach the queue"


def test_intent_mode_rejects_a_nonsense_notional(api, monkeypatch):
    monkeypatch.setenv("EE_BROKER_FORCE_INTENT", "1")
    monkeypatch.delenv("EE_BROKER_FORCE_DIRECT", raising=False)
    ab._probe_cache.clear()

    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 0.0, "reference_price": 100.0})

    assert order["status"] == "rejected_bad_notional"
    assert ab.queued_intents() == []


def test_intent_mode_still_queues_a_valid_order(api, monkeypatch):
    """The guards must not break the cloud relay for legitimate orders."""
    monkeypatch.setenv("EE_BROKER_FORCE_INTENT", "1")
    monkeypatch.delenv("EE_BROKER_FORCE_DIRECT", raising=False)
    ab._probe_cache.clear()

    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 800.0, "reference_price": 100.0})

    assert order["status"] == "queued_intent"
    assert len(ab.queued_intents()) == 1


def test_protective_sells_are_never_blocked_by_the_static_guards(api):
    """A halt must stop the system OPENING risk, never CLOSING it."""
    sb._save({**sb._load(), "cash_usd": 0.0,
              "broker_sync_suspect": {"active": True, "reason": "test"}})
    assert ab.validate_order_static(
        {"ticker": "NVDA", "action": "SELL_TO_CLOSE"}) is None


def test_executor_revalidates_before_replacing_an_intent(api, monkeypatch, tmp_path):
    """VALIDATE AT BOTH ENDS OF THE RELAY. The queueing node validated against
    its view of the ledger, which is hours old by the time the Actions runner
    executes: cash has moved and another intent may have consumed it."""
    sys.path.insert(0, str(ROOT))
    from scripts import execute_order_intents as ex

    monkeypatch.setattr(ex, "ROOT", tmp_path)
    monkeypatch.setattr(ex.alpaca_broker, "queued_intents", lambda: [{
        "order_id": "EE-stale", "client_order_id": "EE-stale", "ticker": "UNH",
        "action": "BUY", "position_size_usd": 50000.0, "proposal_id": "run-old",
        "status": "queued_intent"}])
    monkeypatch.setattr(ex.alpaca_broker, "clear_intents", lambda ids: None)
    monkeypatch.setattr(ex.alpaca_broker, "sync_mirror", lambda: None)
    placed = []
    monkeypatch.setattr(ex.alpaca_broker, "place_order",
                        lambda o: placed.append(o) or {"order_id": "x"})
    monkeypatch.setattr(ex.journal, "log_rejection", lambda *a, **k: None)

    summary = ex.execute_intents()

    assert placed == [], "a stale unfundable intent must never reach the broker"
    assert summary["rejected"] == ["BUY UNH (rejected_insufficient_cash)"]


# --------------------------------------------------------------------------- #
# 4. broker_sync_suspect (2026-07-17, twice, 103 seconds apart)
# --------------------------------------------------------------------------- #
@pytest.fixture
def ledger_check(api, monkeypatch):
    """Enable the ledger cross-check with a $10,000 starting capital and a
    history that mirrors the real incident: DELL -135.19, HPE -45.33, so the
    ledger's own math says the account holds $9,819.48."""
    monkeypatch.setattr(ab, "_starting_capital", lambda: 10000.0)
    state = sb._load()
    state["history"] = [
        {"action": "SELL_TO_CLOSE", "status": "filled", "ticker": "DELL",
         "realized_pnl_usd": -135.19},
        {"action": "SELL_TO_CLOSE", "status": "filled", "ticker": "HPE",
         "realized_pnl_usd": -45.33},
    ]
    state["cash_usd"] = 9819.48
    state["total_equity_usd"] = 9819.48
    sb._save(state)
    return api


def test_ledger_expected_equity_reconstructs_the_account(ledger_check):
    assert ab.ledger_expected_equity() == pytest.approx(9819.48)


def test_zero_cash_read_does_not_overwrite_a_known_good_ledger(ledger_check):
    """THE INCIDENT. A reachable /v2/account returning nothing is a bad read,
    not a wipeout — a paper account cannot drop to $0 with no closing trade.
    The mirror must be left EXACTLY as it was."""
    ledger_check.cash = 0.0
    ledger_check.positions = []

    assert ab.sync_mirror() is None

    state = sb._load()
    assert state["cash_usd"] == pytest.approx(9819.48)
    assert state["total_equity_usd"] == pytest.approx(9819.48)
    assert state["broker_sync_suspect"]["active"] is True
    assert state["broker_sync_suspect"]["allows_new_buys"] is False


def test_the_second_bad_read_is_caught_too(ledger_check):
    """WHY THE OLD GUARD WAS NOT ENOUGH. It compared against total_equity_usd —
    a field a bad read corrupts. Once the mirror had been zeroed, prior_equity
    was 0 and the next read 103 seconds later sailed straight through. The
    ledger's closed-trade math is independent of that field, so it still fires."""
    state = sb._load()
    state["cash_usd"] = 0.0            # mirror ALREADY corrupted by read #1
    state["total_equity_usd"] = 0.0
    sb._save(state)
    ledger_check.cash = 0.0

    assert ab.sync_mirror() is None
    assert sb._load()["broker_sync_suspect"]["active"] is True


def test_zero_cash_with_live_positions_is_caught(ledger_check):
    """The shape the old guard could never catch: cash reads 0 but positions
    come back non-empty, so equity stays positive and the wipeout test passes —
    yet cash_usd = 0 is persisted and every BUY is rejected for four days."""
    ledger_check.cash = 0.0
    ledger_check.set_position("NVDA", 10, 100.0, 100.0)   # equity = 1000, not 0

    assert ab.sync_mirror() is None
    assert sb._load()["cash_usd"] == pytest.approx(9819.48)
    assert sb._load()["broker_sync_suspect"]["active"] is True


def test_a_suspect_mirror_refuses_new_buys_but_not_exits(ledger_check):
    ledger_check.cash = 0.0
    ab.sync_mirror()

    buy = ab.place_order({"ticker": "NVDA", "action": "BUY",
                          "position_size_usd": 800.0, "reference_price": 100.0})
    assert buy["status"] == "rejected_broker_sync_suspect"
    assert ab.validate_order_static(
        {"ticker": "NVDA", "action": "SELL_TO_CLOSE"}) is None


def test_an_agreeing_read_syncs_and_clears_the_flag(ledger_check):
    """The flag is a circuit breaker, not a latch: the next honest read resumes
    normal operation without an operator touching anything."""
    ledger_check.cash = 0.0
    ab.sync_mirror()
    assert ab.sync_suspect() is not None

    ledger_check.cash = 9819.48
    synced = ab.sync_mirror()

    assert synced is not None
    assert synced["cash_usd"] == pytest.approx(9819.48)
    assert ab.sync_suspect() is None


def test_an_ordinary_drawdown_still_syncs(ledger_check):
    """Not a hair trigger: open positions marked below cost are normal. Portfolio
    heat is capped at 8%, so a read a few percent under the ledger's cost-basis
    expectation is real, and must be persisted."""
    ledger_check.cash = 1000.0
    ledger_check.set_position("NVDA", 90, 100.0, 96.0)     # equity ~9,640

    synced = ab.sync_mirror()

    assert synced is not None and synced["cash_usd"] == pytest.approx(1000.0)
    assert ab.sync_suspect() is None
