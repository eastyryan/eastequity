"""Offline tests for execution/alpaca_broker.py + the broker router.

No network: the REST layer (_req), the clock, and the data helper are all
monkeypatched. State files are redirected to tmp_path (and conftest's autouse
snapshot restores simulated_broker attributes after each test).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import alpaca_broker as ab
from execution import broker as router
from execution import simulated_broker as sb
from execution import reconcile_runner


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Fresh mirror + intents file per test; force 'reachable' by default."""
    state = tmp_path / "portfolio.json"
    state.write_text(json.dumps({
        "cash_usd": 10000.0, "total_equity_usd": 10000.0,
        "positions": [], "pending_orders": {}, "history": [],
    }))
    monkeypatch.setattr(sb, "STATE_FILE", state)
    monkeypatch.setattr(ab, "INTENTS_FILE", tmp_path / "order_intents.json")
    monkeypatch.delenv("EE_BROKER_FORCE_INTENT", raising=False)
    monkeypatch.setenv("EE_BROKER_FORCE_DIRECT", "1")
    ab._probe_cache.clear()
    monkeypatch.setattr(ab, "_cfg", lambda: {**ab._DEFAULTS,
                                             "poll_interval_seconds": 0.01,
                                             "buy_poll_timeout_seconds": 0.05,
                                             "sell_poll_timeout_seconds": 0.05})
    monkeypatch.setattr(ab.time, "sleep", lambda *_: None)
    yield


class FakeAPI:
    """Scripted Alpaca REST double. Orders keyed by client_order_id."""

    def __init__(self, *, is_open=True, cash=10000.0, positions=None):
        self.is_open = is_open
        self.cash = cash
        self.positions = positions or []   # list of alpaca position dicts
        self.orders: dict[str, dict] = {}
        self.submits: list[dict] = []
        self.fill_next_with: dict = {}     # {status, filled_qty, filled_avg_price}

    def __call__(self, method, path, *, params=None, body=None, timeout=10):
        if path == "/v2/clock":
            return 200, {"is_open": self.is_open}
        if path == "/v2/account":
            equity = self.cash + sum(float(p["market_value"]) for p in self.positions)
            return 200, {"cash": str(self.cash), "equity": str(equity)}
        if path == "/v2/positions" and method == "GET":
            return 200, list(self.positions)
        if path.startswith("/v2/positions/") and method == "GET":
            sym = path.rsplit("/", 1)[1]
            for p in self.positions:
                if p["symbol"] == sym:
                    return 200, p
            return 404, {"message": "position does not exist"}
        if path == "/v2/orders" and method == "POST":
            cid = body["client_order_id"]
            self.submits.append(body)
            order = {"id": f"srv-{cid}", "client_order_id": cid,
                     "status": "new", "filled_qty": "0", **self.fill_next_with}
            self.orders[cid] = {**order, **self.fill_next_with}
            return 200, self.orders[cid]
        if path == "/v2/orders:by_client_order_id":
            cid = (params or {}).get("client_order_id")
            o = self.orders.get(cid)
            return (200, o) if o else (404, None)
        if path.startswith("/v2/orders/") and method == "DELETE":
            oid = path.rsplit("/", 1)[1]
            for o in self.orders.values():
                if o["id"] == oid and o["status"] not in ("filled",):
                    o["status"] = "canceled"
            return 204, None
        return 404, None


def _wire(monkeypatch, fake: FakeAPI):
    monkeypatch.setattr(ab, "_req", fake)
    monkeypatch.setattr(ab, "_live_last_trade", lambda t: None)  # guard fail-open


# --------------------------------------------------------------------------- #
def test_intent_mode_queues_and_dedupes(monkeypatch):
    monkeypatch.setenv("EE_BROKER_FORCE_INTENT", "1")
    monkeypatch.delenv("EE_BROKER_FORCE_DIRECT", raising=False)
    ab._probe_cache.clear()
    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 500, "reference_price": 100.0,
                            "proposal_id": "run-1"})
    assert order["status"] == "queued_intent"
    rb = ab.readback(order["order_id"])
    assert rb["status"] == "queued_intent"
    assert len(ab.queued_intents()) == 1
    # idempotent re-place with the same client id
    ab.place_order({**order})
    assert len(ab.queued_intents()) == 1
    ab.clear_intents([order["order_id"]])
    assert ab.queued_intents() == []


def test_buy_fill_stamps_metadata(monkeypatch):
    fake = FakeAPI(is_open=True, cash=10000.0)
    fake.fill_next_with = {"status": "filled", "filled_qty": "5",
                           "filled_avg_price": "100.5",
                           "filled_at": "2026-07-17T14:00:00Z"}
    _wire(monkeypatch, fake)

    def submit_side_effect(method, path, **kw):
        code, body = fake(method, path, **kw)
        if path == "/v2/orders" and method == "POST":
            fake.positions = [{"symbol": "NVDA", "qty": "5",
                               "avg_entry_price": "100.5",
                               "market_value": "502.5", "current_price": "100.5",
                               "qty_available": "5"}]
            fake.cash = 10000.0 - 502.5
        return code, body
    monkeypatch.setattr(ab, "_req", submit_side_effect)

    plan = {"stop_loss": 90.0, "target_price": 130.0, "holding_horizon_days": 30}
    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 502.5, "reference_price": 100.0,
                            "entry_price_max": 105.0, "proposal_id": "run-2",
                            "plan": plan, "demand_driver": "ai_compute_gpu"})
    assert order["status"] == "submitted"
    fill = ab.readback(order["order_id"])
    assert fill["status"] == "filled"
    assert fill["quantity"] == 5.0 and fill["fill_price"] == 100.5
    assert fill["fees_usd"]["commission"] == 0.0
    state = sb._load()
    assert state["cash_usd"] == pytest.approx(9497.5)
    pos = state["positions"][0]
    assert pos["ticker"] == "NVDA" and pos["plan"] == plan
    assert pos["demand_driver"] == "ai_compute_gpu"
    assert pos["high_water"] >= 100.5 and pos["opened_at"]
    assert state["pending_orders"] == {}


def test_buy_insufficient_cash_rejected(monkeypatch):
    fake = FakeAPI(is_open=True, cash=100.0)
    _wire(monkeypatch, fake)
    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 500, "reference_price": 100.0})
    assert order["status"] == "rejected_insufficient_cash"
    fill = ab.readback(order["order_id"])
    assert fill["status"] == "rejected_insufficient_cash"
    assert fake.submits == []  # never reached the broker


def test_buy_rejected_when_market_closed(monkeypatch):
    fake = FakeAPI(is_open=False)
    _wire(monkeypatch, fake)
    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 500, "reference_price": 100.0})
    assert order["status"] == "rejected_market_closed"
    assert fake.submits == []


def test_sell_partial_carries_entry_facts(monkeypatch):
    pos = {"symbol": "DELL", "qty": "10", "avg_entry_price": "400.0",
           "market_value": "4500.0", "current_price": "450.0",
           "qty_available": "10"}
    fake = FakeAPI(is_open=True, cash=1000.0, positions=[pos])
    _wire(monkeypatch, fake)
    # seed mirror metadata (as a prior BUY would have)
    st = sb._load()
    st["positions"] = [{"ticker": "DELL", "quantity": 10.0, "avg_cost": 400.0,
                        "market_value_usd": 4500.0, "opened_at": "2026-07-01T14:00:00+00:00",
                        "proposal_id": "run-0", "plan": {"stop_loss": 380.0},
                        "high_water": 460.0, "dividends_received_usd": 4.0}]
    sb._save(st)

    def submit_side_effect(method, path, **kw):
        code, body = fake(method, path, **kw)
        if path == "/v2/orders" and method == "POST":
            fake.fill_next_with = {}
            cid = kw["body"]["client_order_id"]
            fake.orders[cid].update({"status": "filled", "filled_qty": kw["body"]["qty"],
                                     "filled_avg_price": "450.0"})
            fake.positions = [{**pos, "qty": "5", "qty_available": "5",
                               "market_value": "2250.0"}]
            fake.cash = 1000.0 + 2250.0
        return code, body
    monkeypatch.setattr(ab, "_req", submit_side_effect)

    order = ab.place_order({"ticker": "DELL", "action": "SELL_TO_CLOSE",
                            "reference_price": 450.0, "sell_fraction": 0.5,
                            "proposal_id": "run-3"})
    fill = ab.readback(order["order_id"])
    assert fill["status"] == "filled"
    assert fill["quantity"] == 5.0
    assert fill["avg_cost"] == 400.0
    assert fill["realized_pnl_usd"] == pytest.approx(250.0)
    assert fill["dividends_received_usd"] == pytest.approx(2.0)
    assert fill["entry_plan"] == {"stop_loss": 380.0}
    state = sb._load()
    remaining = state["positions"][0]
    assert remaining["quantity"] == 5.0
    assert remaining["dividends_received_usd"] == pytest.approx(2.0)
    assert remaining["opened_at"] == "2026-07-01T14:00:00+00:00"


def test_sell_when_closed_rests_then_reconciles(monkeypatch):
    pos = {"symbol": "HPE", "qty": "3", "avg_entry_price": "50.0",
           "market_value": "141.0", "current_price": "47.0",
           "qty_available": "3"}
    fake = FakeAPI(is_open=False, cash=1000.0, positions=[pos])
    _wire(monkeypatch, fake)
    st = sb._load()
    st["positions"] = [{"ticker": "HPE", "quantity": 3.0, "avg_cost": 50.0,
                        "market_value_usd": 141.0,
                        "opened_at": "2026-07-01T14:00:00+00:00"}]
    sb._save(st)

    order = ab.place_order({"ticker": "HPE", "action": "SELL_TO_CLOSE",
                            "reference_price": 47.0, "proposal_id": "run-4",
                            "forced_exit_reason": "stop_loss_breached"})
    assert order["status"] == "submitted"
    rb = ab.readback(order["order_id"])
    assert rb["status"] == "resting_market_closed"
    assert order["order_id"] in sb._load()["pending_orders"]

    # ...market opens, the order fills; reconcile picks it up
    cid = order["order_id"]
    fake.orders[cid].update({"status": "filled", "filled_qty": "3",
                             "filled_avg_price": "46.5"})
    fake.positions = []
    fake.cash = 1000.0 + 139.5
    fake.is_open = True
    done = ab.reconcile()
    assert len(done) == 1
    o, f = done[0]
    assert f["status"] == "filled" and f["quantity"] == 3.0
    assert f["realized_pnl_usd"] == pytest.approx((46.5 - 50.0) * 3)
    assert sb._load()["pending_orders"] == {}
    assert not sb._load()["positions"]


def test_router_dispch_and_reconcile_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("EE_BROKER", raising=False)  # conftest pins simulation
    cfg = {"mode": {"broker": "alpaca_paper", "trading_mode": "paper"}}
    (tmp_path / "autonomy_config.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(router, "ROOT", tmp_path)
    assert router.backend_name() == "alpaca_paper"
    cfg["mode"]["broker"] = "simulation"
    (tmp_path / "autonomy_config.json").write_text(json.dumps(cfg))
    assert router.backend_name() == "simulation"
    assert router.reconcile() == []  # simulation has no async lifecycle
    cfg["mode"]["broker"] = "martian_broker"
    (tmp_path / "autonomy_config.json").write_text(json.dumps(cfg))
    assert router.backend_name() == "simulation"  # unknown -> safe default


def test_record_fill_dedupes_by_client_id(monkeypatch, tmp_path):
    monkeypatch.setattr(reconcile_runner, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(reconcile_runner.journal, "log_trade",
                        lambda o, f, r: calls.append(r))
    order = {"client_order_id": "EE-abc123", "proposal_id": "run-9"}
    fill = {"action": "HOLDNOOP", "ticker": "X"}
    reconcile_runner.record_fill(order, fill)
    assert calls == ["run-9"]
    # simulate the journal line existing (journal.log_trade was stubbed, so write it)
    tf = tmp_path / "journal" / "trades" / "2026-07-17.jsonl"
    tf.parent.mkdir(parents=True)
    tf.write_text(json.dumps({"order": {"client_order_id": "EE-abc123"}}) + "\n")
    reconcile_runner.record_fill(order, fill)
    assert calls == ["run-9"]  # second call skipped


if __name__ == "__main__":
    print("run under pytest")
