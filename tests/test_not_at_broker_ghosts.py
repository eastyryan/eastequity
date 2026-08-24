"""not_at_broker ghosts must not permanently block trading.

Regression (HPE 2026-08-19):
  1. Whole-share resting stop filled out-of-band (1.0 of ~1.11 shares).
  2. Forced-exit market order filled the fractional remnant at the broker.
  3. A later cycle recorded rejected_no_position under the SAME deterministic
     client_order_id (broker already flat).
  4. OOB ingest treated that reject as 'known' and never adopted the fill.
  5. Ghost remnant → ensure_protective_stop FAILED → protective_stops_armed
     DEAD → every new BUY rejected as capability_dead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import alpaca_broker as ab  # noqa: E402
from execution import simulated_broker as sb  # noqa: E402
import journal as _journal  # noqa: E402
from execution import reconcile_runner  # noqa: E402


CID = "EEXIT-HPE-2026-08-19-trailing-stop-breach"
OID = "8602c0b8-bc58-4b1a-b7d7-56e8fe419017"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    state = tmp_path / "portfolio.json"
    state.write_text(json.dumps({
        "cash_usd": 700.0, "total_equity_usd": 706.0,
        "positions": [{
            "ticker": "HPE", "quantity": 0.114718, "avg_cost": 55.144,
            "market_value_usd": 6.0, "last_price": 52.37,
            "not_at_broker": True,
            "opened_at": "2026-08-10T15:13:03+00:00",
            "proposal_id": "20260810-6708d8",
            "plan": {"stop_loss": 51.25, "confidence": 0.58},
            "protective_stop": {"status": "FAILED", "stop_price": 53.26},
        }],
        "pending_orders": {},
        "history": [{
            "ticker": "HPE", "action": "SELL_TO_CLOSE",
            "client_order_id": CID, "order_id": CID,
            "status": "rejected_no_position",
            "forced_exit_reason": "trailing_stop_breached",
            "filled_at": "2026-08-19T14:57:18+00:00",
        }],
        "ingested_broker_orders": [],
    }))
    monkeypatch.setattr(sb, "STATE_FILE", state)
    monkeypatch.setattr(ab, "INTENTS_FILE", tmp_path / "order_intents.json")
    jdir = tmp_path / "journal"
    jdir.mkdir()
    (jdir / "trades").mkdir()
    (jdir / "rejected").mkdir()
    (jdir / "exit_autopsies").mkdir()
    monkeypatch.setattr(_journal, "JOURNAL", jdir)
    monkeypatch.setattr(reconcile_runner, "ROOT", tmp_path)
    # record_fill chains into learning stores — keep them off the real tree.
    import tools.concept_memory as CM
    import tools.exit_autopsy as EA
    import tools.post_exit_runners as PR
    monkeypatch.setattr(CM, "MEM_DIR", tmp_path / "concept_memory")
    monkeypatch.setattr(CM, "ROOT", tmp_path)
    monkeypatch.setattr(EA, "AUTOPSY_DIR", jdir / "exit_autopsies")
    monkeypatch.setattr(EA, "ROOT", tmp_path)
    monkeypatch.setattr(PR, "RUNNERS_FILE", tmp_path / "post_exit_runners.json")
    monkeypatch.setattr(PR, "ROOT", tmp_path)
    monkeypatch.setattr(ab, "_cfg", lambda: {**ab._DEFAULTS, "resting_stops": True})
    monkeypatch.setattr(ab, "_starting_capital", lambda: 0.0)
    monkeypatch.setattr(ab.time, "sleep", lambda *_: None)
    # x_poster promote writes state/x_draft_* — stub it.
    monkeypatch.setattr(reconcile_runner, "record_fill",
                        lambda order, fill: _journal.log_trade(order, fill, "test"))
    ab._probe_cache.clear()
    ab._probe_cache["ok"] = True
    return state


class FakeBroker:
    def __init__(self, *, closed=None, positions=None):
        self.closed = closed or []
        self.positions = positions or []
        self.submits = []

    def __call__(self, method, path, *, params=None, body=None, timeout=10):
        if path == "/v2/clock":
            return 200, {"is_open": True}
        if path == "/v2/account":
            return 200, {"cash": "700", "equity": "700"}
        if path == "/v2/positions" and method == "GET":
            return 200, list(self.positions)
        if path.startswith("/v2/positions/") and method == "GET":
            sym = path.rsplit("/", 1)[1]
            for p in self.positions:
                if p["symbol"] == sym:
                    return 200, p
            return 404, {"message": "position does not exist"}
        if path == "/v2/orders" and method == "GET":
            if (params or {}).get("status") == "closed":
                return 200, list(self.closed)
            return 200, []
        if path == "/v2/orders" and method == "POST":
            self.submits.append(body)
            return 422, {"message": "insufficient qty available"}
        if method == "DELETE":
            return 204, None
        return 404, None


def _missed_fill():
    return {
        "id": OID, "client_order_id": CID, "symbol": "HPE", "side": "sell",
        "type": "market", "status": "filled", "filled_qty": "0.114717829",
        "filled_avg_price": "51.836",
        "filled_at": "2026-08-19T14:22:07.396945Z",
        "submitted_at": "2026-08-19T14:20:00Z",
    }


def test_rejected_history_does_not_poison_filled_id_set(isolated):
    known = ab._filled_history_ids(sb._load())
    assert CID not in known


def test_ghost_reconcile_adopts_missed_fill_despite_reject(isolated, monkeypatch):
    fake = FakeBroker(closed=[_missed_fill()], positions=[])
    monkeypatch.setattr(ab, "_req", fake)
    # sync_mirror inside _apply_fill must not trip the ledger cross-check
    monkeypatch.setattr(ab, "sync_mirror", lambda: sb._load())

    done = ab.reconcile_not_at_broker_ghosts(tickers=["HPE"])

    assert done, "missed broker fill was not adopted"
    assert done[0]["status"] == "filled"
    assert abs(float(done[0]["quantity"]) - 0.114718) < 1e-3
    st = sb._load()
    assert not any(p.get("ticker") == "HPE" for p in st["positions"])
    filled = [h for h in st["history"]
              if h.get("status") == "filled" and h.get("alpaca_order_id") == OID]
    assert len(filled) == 1


def test_ghost_reconcile_writes_off_when_no_broker_fill(isolated, monkeypatch):
    fake = FakeBroker(closed=[], positions=[])
    monkeypatch.setattr(ab, "_req", fake)

    done = ab.reconcile_not_at_broker_ghosts(tickers=["HPE"])

    assert done and done[0].get("write_off") is True
    st = sb._load()
    assert not any(p.get("ticker") == "HPE" for p in st["positions"])


def test_ensure_protective_stop_does_not_fail_ghosts(isolated, monkeypatch):
    fake = FakeBroker(closed=[], positions=[])
    monkeypatch.setattr(ab, "_req", fake)
    # Write-off path runs inside ensure; after it the position is gone.
    rec = ab.ensure_protective_stop("HPE", reason="test")
    assert rec is None
    st = sb._load()
    # Either written off, or FAILED stamp cleared on a remaining ghost.
    hpe = next((p for p in st["positions"] if p.get("ticker") == "HPE"), None)
    if hpe is not None:
        assert hpe.get("protective_stop") in (None, {})
    else:
        assert any(h.get("write_off") for h in st["history"])


def test_unprotected_positions_skips_ghosts(isolated):
    naked = ab.unprotected_positions()
    assert naked == []
