"""One broker fill must produce exactly one ledger record, on any node.

Two reconcilers run on schedule against SEPARATE checkouts of this ledger: the
Actions executor (*/15 weekdays) on a fresh origin clone, and the local stop_watch
tick (every 5 min) on the persistent Mac checkout. Both call run_reconcile().

The three documented idempotency layers -- history ids, the persisted
ingested_broker_orders set, and reconcile_runner._already_journaled -- are ALL
per-checkout, and the module's own comment concedes the journal is only shared "after
git syncs them". So an overnight stop fill was applied on both nodes and merged on
rebase into TWO history records for one broker order.

Not cosmetic: ledger_expected_equity() sums realized P&L across SELL_TO_CLOSE rows, so
a double-counted realization shifts the reconstruction and, past
sync_suspect_tolerance, trips broker_sync_suspect and refuses every new BUY until a
human intervenes.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import alpaca_broker as ab  # noqa: E402
from execution import simulated_broker as sb  # noqa: E402

BROKER_OID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    f = tmp_path / "portfolio.json"
    f.write_text(json.dumps({
        "cash_usd": 10_000.0, "total_equity_usd": 10_000.0,
        "positions": [{"ticker": "DELL", "quantity": 10.0, "avg_cost": 400.0,
                       "market_value_usd": 4000.0, "last_price": 400.0,
                       "opened_at": "2026-07-10T14:00:00+00:00",
                       "plan": {"stop_loss": 380.0}}],
        "history": [], "pending_orders": {},
    }))
    monkeypatch.setattr(sb, "STATE_FILE", f)
    monkeypatch.setattr(ab, "sync_mirror", lambda: sb._load())
    monkeypatch.setattr(ab, "_maintain_protective_stop", lambda *a, **k: None)
    return f


def _order():
    return {"id": BROKER_OID, "filled_qty": "10", "filled_avg_price": "380.00",
            "status": "filled"}


def _pending():
    return {"ticker": "DELL", "action": "SELL_TO_CLOSE", "order_id": "EE-node-a",
            "alpaca_order_id": BROKER_OID, "proposal_id": "run-a"}


def test_the_same_broker_fill_is_applied_only_once(ledger):
    """THE regression: node A applies it, node B replays the identical broker id."""
    ab._apply_fill(_pending(), _order())
    after_first = json.loads(ledger.read_text())
    assert len(after_first["history"]) == 1

    # Node B, different local order id, SAME broker order.
    second = dict(_pending(), order_id="EE-node-b")
    ab._apply_fill(second, _order())

    after_second = json.loads(ledger.read_text())
    assert len(after_second["history"]) == 1, "the fill was double-booked"


def test_realized_pnl_is_not_double_counted(ledger):
    """The consequence that matters: a doubled realization drifts
    ledger_expected_equity and can trip broker_sync_suspect, refusing every BUY."""
    ab._apply_fill(_pending(), _order())
    pnl_once = sum(h.get("total_realized_pnl_usd") or 0
                   for h in json.loads(ledger.read_text())["history"])
    ab._apply_fill(dict(_pending(), order_id="EE-node-b"), _order())
    pnl_twice = sum(h.get("total_realized_pnl_usd") or 0
                    for h in json.loads(ledger.read_text())["history"])
    assert pnl_once == pnl_twice


def test_a_genuinely_different_fill_still_applies(ledger):
    """The mirror. Without it, 'never apply anything twice' degenerates into
    'never apply a second trade', and the ledger silently stops recording."""
    ab._apply_fill(_pending(), _order())
    other = {"id": "ffffffff-0000-1111-2222-333333333333",
             "filled_qty": "5", "filled_avg_price": "390.00", "status": "filled"}
    ab._apply_fill({"ticker": "DELL", "action": "BUY", "order_id": "EE-2",
                    "alpaca_order_id": other["id"], "position_size_usd": 1950.0,
                    "plan": {"stop_loss": 370.0}}, other)
    assert len(json.loads(ledger.read_text())["history"]) == 2


def test_the_replay_returns_the_original_record(ledger):
    """Callers journal what _apply_fill returns, so a replay must hand back the
    record that already exists rather than a fresh one."""
    first = ab._apply_fill(_pending(), _order())
    replay = ab._apply_fill(dict(_pending(), order_id="EE-node-b"), _order())
    assert replay.get("alpaca_order_id") == first.get("alpaca_order_id")
    assert replay.get("fill_price") == first.get("fill_price")
