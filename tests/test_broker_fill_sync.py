"""Broker-side fills must reach the trading context (ILMN 2026-10-07).

The resting ILMN stop filled at the broker at 09:39 ET; the 10:30 slot still
reviewed ILMN as held because (1) the trading cycle never ran the out-of-band
ingest and (2) the stop-watch job's commit step staged nothing. These tests pin
both fixes plus the loud broker_reconciliation block. Offline: _req is faked.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution import alpaca_broker as ab
from execution import reconcile_runner
from execution import simulated_broker as sb


def _pos(t, q, cost=100.0, **kw):
    return {"ticker": t, "quantity": q, "avg_cost": cost,
            "market_value_usd": q * cost, **kw}


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    state = tmp_path / "portfolio.json"
    monkeypatch.setattr(sb, "STATE_FILE", state)

    def write(positions, history=None):
        state.write_text(json.dumps({
            "cash_usd": 500.0, "total_equity_usd": 1000.0,
            "positions": positions, "pending_orders": {},
            "history": history or []}))
    return write


def _bp(sym, qty):
    return {"symbol": sym, "qty": str(qty), "avg_entry_price": "100",
            "market_value": str(qty * 100), "current_price": "100"}


# ----------------------------------------------------------------- mismatch
def test_mismatch_ok_when_books_agree(mirror):
    mirror([_pos("CLS", 0.139601), _pos("NOW", 0.791987)])
    rec = ab.broker_position_mismatches(
        broker_positions=[_bp("CLS", 0.139600916), _bp("NOW", 0.791986698)])
    assert rec["status"] == "ok"
    assert rec["mismatches"] == []


def test_mismatch_flags_mirror_only_broker_only_and_qty(mirror):
    mirror([_pos("ILMN", 0.264418, not_at_broker=True), _pos("CLS", 0.5),
            _pos("NOW", 0.79)])
    rec = ab.broker_position_mismatches(
        broker_positions=[_bp("CLS", 0.25), _bp("NOW", 0.79), _bp("MU", 1.0)])
    kinds = {m["ticker"]: m["kind"] for m in rec["mismatches"]}
    assert rec["status"] == "mismatch"
    assert kinds == {"ILMN": "mirror_only", "CLS": "qty_mismatch",
                     "MU": "broker_only"}


def test_mismatch_unavailable_is_not_ok(mirror, monkeypatch):
    mirror([_pos("CLS", 0.5)])
    monkeypatch.setattr(ab, "api_reachable", lambda: False)
    rec = ab.broker_position_mismatches()
    assert rec["status"] == "unavailable"


# ------------------------------------------------------------- context block
def test_block_is_loud_on_ingested_fill_and_mismatch():
    block = reconcile_runner.reconciliation_block({
        "status": "mismatch",
        "mismatches": [{"ticker": "CLS", "kind": "qty_mismatch",
                        "mirror_qty": 0.5, "broker_qty": 0.25}],
        "ingested_fills": [{"ticker": "ILMN", "quantity": 0.264418,
                            "fill_price": 261.792, "reason": "resting_stop_breached",
                            "filled_at": "2026-10-07T13:39:06Z",
                            "realized_pnl_usd": -4.8}]})
    assert "ILMN was CLOSED AT THE BROKER" in block["ALERT"]
    assert "NOT held" in block["ALERT"]
    assert "BROKER/LEDGER MISMATCH CLS" in block["ALERT"]


def test_block_ok_and_unavailable_wording():
    assert reconcile_runner.reconciliation_block(
        {"status": "ok", "mismatches": [], "ingested_fills": []}
    )["ALERT"].startswith("ok")
    un = reconcile_runner.reconciliation_block(
        {"status": "unavailable", "mismatches": [], "reason": "no keys"})
    assert "DID NOT RUN" in un["ALERT"]
    assert reconcile_runner.reconciliation_block({"status": "not_applicable"}) is None


def test_annotate_portfolio_stamps_mismatched_rows():
    pf = {"positions": [{"ticker": "ILMN"}, {"ticker": "CLS"}]}
    reconcile_runner.annotate_portfolio(pf, {"mismatches": [
        {"ticker": "ILMN", "kind": "mirror_only", "mirror_qty": 0.26,
         "broker_qty": None}]})
    assert "mirror_only" in pf["positions"][0]["BROKER_MISMATCH"]
    assert "BROKER_MISMATCH" not in pf["positions"][1]


# ------------------------------------------- sync ingests BEFORE the compare
def test_sync_broker_state_ingests_then_compares(monkeypatch):
    calls = []
    monkeypatch.setattr(reconcile_runner.broker, "backend_name",
                        lambda: "alpaca_paper")
    monkeypatch.setattr(reconcile_runner, "run_reconcile",
                        lambda: calls.append("ingest") or [{
                            "ticker": "ILMN", "action": "SELL_TO_CLOSE",
                            "quantity": 0.264418, "fill_price": 261.792,
                            "client_order_id": "EESTOP-ILMN-x",
                            "realized_pnl_usd": -4.8}])
    monkeypatch.setattr(ab, "broker_position_mismatches",
                        lambda: calls.append("compare") or
                        {"status": "ok", "mismatches": []})
    rec = reconcile_runner.sync_broker_state(ingest=True)
    assert calls == ["ingest", "compare"]
    assert rec["ingested_fills"][0]["ticker"] == "ILMN"
    assert rec["ingested_fills"][0]["reason"] == "resting_stop_breached"


def test_sync_broker_state_read_only_and_simulation(monkeypatch):
    monkeypatch.setattr(reconcile_runner.broker, "backend_name",
                        lambda: "alpaca_paper")
    monkeypatch.setattr(reconcile_runner, "run_reconcile",
                        lambda: pytest.fail("must not ingest when ingest=False"))
    monkeypatch.setattr(ab, "broker_position_mismatches",
                        lambda: {"status": "ok", "mismatches": []})
    assert reconcile_runner.sync_broker_state(ingest=False)["ingest_ran"] is False
    monkeypatch.setattr(reconcile_runner.broker, "backend_name",
                        lambda: "simulation")
    assert reconcile_runner.sync_broker_state()["status"] == "not_applicable"


def test_safety_layer_runs_broker_sync_before_stops(monkeypatch):
    """The act path must book broker-side fills and surface the block."""
    from runlib import brain_io
    order = []
    monkeypatch.setattr(reconcile_runner, "sync_broker_state",
                        lambda ingest=True: order.append("sync") or {
                            "status": "ok", "mismatches": [],
                            "ingested_fills": [{"ticker": "ILMN", "quantity": 0.26,
                                                "fill_price": 261.792,
                                                "reason": "resting_stop_breached"}]})
    monkeypatch.setattr(brain_io, "get_portfolio_state",
                        lambda: order.append("portfolio") or {"positions": []})
    monkeypatch.setattr(brain_io.corporate_actions, "apply_corporate_actions",
                        lambda: order.append("ca") or {})
    monkeypatch.setattr(brain_io.broker, "rearm_protective_stops",
                        lambda reason="": order.append("rearm") or {})
    monkeypatch.setattr(brain_io.exit_guard, "check_forced_exits",
                        lambda *a, **k: [])
    ctx = {"portfolio": {"positions": [{"ticker": "ILMN"}]},
           "universe_scan": {"prices": {}}}
    cfg = {"mode": {"trading_mode": "paper"}}
    assert brain_io.apply_safety_layer(ctx, cfg, "test-run") == []
    assert order[:2] == ["sync", "portfolio"]
    assert "ILMN was CLOSED AT THE BROKER" in ctx["broker_reconciliation"]["ALERT"]
    assert ctx["portfolio"] == {"positions": []}


def test_broker_reconciliation_is_pinned_in_read_window():
    from runlib import context_tiers
    keys = context_tiers.BLOCKING_KEYS
    assert "broker_reconciliation" in keys
    assert keys.index("broker_reconciliation") < keys.index("portfolio")
    assert "broker_reconciliation" in context_tiers.ALWAYS_KEYS


# ------------------------------------------------- stop-watch commit step
def test_stop_watch_ledger_add_not_coupled_to_ignored_paths():
    """A gitignored/absent path in the same `git add` aborts the whole add."""
    wf = (ROOT / ".github" / "workflows" / "stop-watch.yml").read_text()
    adds = [ln.strip() for ln in wf.splitlines()
            if re.match(r"\s*git add\b", ln) and "state/portfolio.json" in ln]
    assert adds, "stop-watch must stage the ledger"
    for ln in adds:
        assert "data/cache" not in ln
        assert "|| true" not in ln, "a failed ledger add must not be silenced"
