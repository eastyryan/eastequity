"""F8 + F11a regressions: the ledger cross-check must not count cash the broker
never credited, and a mirror-missing sell must not vanish from it. Pure Python,
no network, temp state:

    EE_BROKER=simulation python3 tests/test_exec_audit_ledger_dividends.py

F8 (audited 2026-08-05): corporate_actions makes dividends METADATA-ONLY on the
alpaca backend (Alpaca paper credits no dividend cash), recording the decision
on each event row as `cash_credited` — yet ledger_expected_equity added
open-position dividend metadata and the dividend-inclusive
total_realized_pnl_usd unconditionally. Every alpaca dividend therefore biased
`expected` ABOVE broker equity by cash that never existed, walking the
cross-check toward a false broker_sync_suspect (which refuses all new BUYs).
The reconstruction now sums price-only realized P&L plus ONLY cash_credited
dividend events.

F11a (audited 2026-08-05): an out-of-band sell of a mirror-missing position
booked avg_cost 0 -> realized_pnl_usd None, a row the reconstruction can only
skip, so the realization went permanently missing from `expected`. The basis is
now backfilled from the most recent filled BUY history row
(position_avg_cost_after) and labeled avg_cost_source="history_backfill"; the
original audit suggestion of crediting raw PROCEEDS was rejected — see the
inline note in test_raw_proceeds_would_have_been_wrong.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EE_BROKER"] = "simulation"
os.environ["EE_BROKER_FORCE_DIRECT"] = "1"
os.environ.pop("EE_BROKER_FORCE_INTENT", None)

import journal as _journal                     # noqa: E402
from execution import alpaca_broker as ab      # noqa: E402
from execution import simulated_broker as sb   # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ee-exec-audit-f8-"))
sb.STATE_FILE = TMP / "portfolio.json"
ab.INTENTS_FILE = TMP / "order_intents.json"
_journal.JOURNAL = TMP / "journal"

ab._probe_cache["ok"] = True
ab._cfg = lambda: dict(ab._DEFAULTS)
ab.time.sleep = lambda *_: None

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _write_state(history, positions):
    (TMP / "portfolio.json").write_text(json.dumps({
        "cash_usd": 1000.0, "total_equity_usd": 1000.0,
        "positions": positions, "pending_orders": {}, "history": history,
    }))


def test_expected_equity_counts_only_credited_dividends():
    print("mixed history: credited, metadata-only, legacy dividends + sells:")
    ab._starting_capital = lambda: 10000.0
    _write_state(
        history=[
            {"action": "SELL_TO_CLOSE", "status": "filled", "ticker": "DELL",
             "realized_pnl_usd": -100.0, "total_realized_pnl_usd": -100.0},
            # alpaca-era: attributed but NEVER credited by the broker
            {"type": "dividend", "ticker": "BKR", "amount_usd": 5.0,
             "cash_credited": False, "date": "2026-08-01"},
            # sim-era: genuinely credited to cash
            {"type": "dividend", "ticker": "OLD", "amount_usd": 7.0,
             "cash_credited": True, "date": "2026-05-01"},
            # pre-backend-split row (no flag) -> written when sim owned cash
            {"type": "dividend", "ticker": "LEG", "amount_usd": 3.0,
             "date": "2026-04-01"},
            # legacy sell with only the dividend-inclusive total: strip divs
            {"action": "SELL_TO_CLOSE", "status": "filled", "ticker": "HPE",
             "realized_pnl_usd": None, "total_realized_pnl_usd": 12.0,
             "dividends_received_usd": 2.0},
        ],
        positions=[{"ticker": "BKR", "quantity": 1.0, "avg_cost": 40.0,
                    "market_value_usd": 41.0,
                    # open-position metadata the old code added unconditionally
                    "dividends_received_usd": 9.0}],
    )
    got = ab.ledger_expected_equity()
    # 10000 - 100 (price P&L) + 7 (credited) + 3 (legacy=credited)
    # + (12 - 2) (legacy total minus its dividend component) = 9920
    check("expected == 9920.00 (no uncredited cash counted)",
          got == 9920.0, str(got))


def test_metadata_only_dividend_adds_zero_bias():
    print("a lone alpaca metadata dividend must not move expected at all:")
    ab._starting_capital = lambda: 10000.0
    _write_state(
        history=[{"type": "dividend", "ticker": "BKR", "amount_usd": 5.0,
                  "cash_credited": False, "date": "2026-08-01"}],
        positions=[{"ticker": "BKR", "quantity": 1.0, "avg_cost": 40.0,
                    "market_value_usd": 41.0, "dividends_received_usd": 5.0}],
    )
    check("expected == starting capital", ab.ledger_expected_equity() == 10000.0,
          str(ab.ledger_expected_equity()))


def test_unknown_baseline_still_disables_the_check():
    print("no starting capital -> cross-check stays off (unchanged):")
    ab._starting_capital = lambda: 0.0
    _write_state(history=[], positions=[])
    check("returns None", ab.ledger_expected_equity() is None)


class FakeFlat:
    """Broker double: funded account, NO positions (the mirror-missing shape)."""

    def __call__(self, method, path, *, params=None, body=None, timeout=10):
        if path == "/v2/clock":
            return 200, {"is_open": True}
        if path == "/v2/account":
            return 200, {"cash": "10000", "equity": "10000"}
        if path == "/v2/positions" and method == "GET":
            return 200, []
        if path.startswith("/v2/positions/") and method == "GET":
            return 404, {"message": "position does not exist"}
        if path == "/v2/orders" and method == "GET":
            return 200, []
        if method == "DELETE":
            return 204, None
        return 404, None


def _oob_sell_fill(order_key="oob-1", srv="srv-s1"):
    pending = {"ticker": "XYZ", "action": "SELL_TO_CLOSE", "order_id": order_key,
               "client_order_id": order_key, "proposal_id": "out-of-band",
               "out_of_band": True, "reference_price": 60.0}
    ao = {"id": srv, "status": "filled", "filled_qty": "2",
          "filled_avg_price": "60.0", "filled_at": "2026-08-05T08:00:00Z"}
    return ab._apply_fill(pending, ao)


def test_mirror_missing_sell_backfills_basis_from_history():
    print("F11a: mirror-missing sell recovers its basis from the BUY row:")
    ab._starting_capital = lambda: 0.0
    ab._req = FakeFlat()
    _write_state(
        history=[{"ticker": "XYZ", "action": "BUY", "status": "filled",
                  "fill_price": 48.0, "position_avg_cost_after": 50.0,
                  "alpaca_order_id": "srv-b1"}],
        positions=[],   # the mirror lost the position
    )
    fill = _oob_sell_fill()
    check("filled", fill.get("status") == "filled", str(fill.get("status")))
    check("basis backfilled to 50.0", fill.get("avg_cost") == 50.0,
          str(fill.get("avg_cost")))
    check("row labeled history_backfill",
          fill.get("avg_cost_source") == "history_backfill", str(fill))
    check("realized computed: (60-50)*2 == 20",
          fill.get("realized_pnl_usd") == 20.0, str(fill.get("realized_pnl_usd")))
    check("total realized carries it too",
          fill.get("total_realized_pnl_usd") == 20.0,
          str(fill.get("total_realized_pnl_usd")))
    # ...and the realization is now VISIBLE to the reconstruction
    ab._starting_capital = lambda: 10000.0
    check("ledger_expected_equity includes it",
          ab.ledger_expected_equity() == 10020.0,
          str(ab.ledger_expected_equity()))


def test_no_entry_anywhere_keeps_pnl_honestly_none():
    print("F11a control: no BUY row anywhere -> P&L stays None, unlabeled:")
    ab._starting_capital = lambda: 0.0
    ab._req = FakeFlat()
    _write_state(history=[], positions=[])
    fill = _oob_sell_fill(order_key="oob-2", srv="srv-s2")
    check("filled", fill.get("status") == "filled", str(fill.get("status")))
    check("realized honestly None", fill.get("realized_pnl_usd") is None)
    check("no fabricated source label", "avg_cost_source" not in fill, str(fill))


def test_raw_proceeds_would_have_been_wrong():
    """Documents WHY the audit's literal suggestion was not implemented.

    The audit proposed crediting the fill's raw PROCEEDS to expected equity
    when the basis is unknown ("cash did change"). But expected equity is
    start + realized P&L: every BUY is deliberately absent from it because a
    buy converts cash to stock at equal value. Crediting a sell's full
    proceeds therefore assumes the shares were acquired for FREE and
    overstates expected by the position's entire cost basis — a $200 position
    sold at $210 would inject a $200 error instead of the $10 truth, actively
    walking the cross-check toward the false sync-suspect it exists to
    prevent. The history backfill recovers the true basis instead; only when
    the entry genuinely never touched this ledger does the row stay (honestly)
    invisible.
    """
    print("proceeds-crediting rejection is documented and basis-backfill wins:")
    # With backfill: expected moves by the TRUE realized (+20), not by the
    # proceeds (+120) the audit suggestion would have injected.
    ab._starting_capital = lambda: 10000.0
    ab._req = FakeFlat()
    _write_state(
        history=[{"ticker": "XYZ", "action": "BUY", "status": "filled",
                  "fill_price": 50.0, "position_avg_cost_after": 50.0,
                  "alpaca_order_id": "srv-b9"}],
        positions=[],
    )
    _oob_sell_fill(order_key="oob-3", srv="srv-s3")
    got = ab.ledger_expected_equity()
    check("expected moved by realized P&L (+20), not proceeds (+120)",
          got == 10020.0, str(got))


if __name__ == "__main__":
    for fn in (test_expected_equity_counts_only_credited_dividends,
               test_metadata_only_dividend_adds_zero_bias,
               test_unknown_baseline_still_disables_the_check,
               test_mirror_missing_sell_backfills_basis_from_history,
               test_no_entry_anywhere_keeps_pnl_honestly_none,
               test_raw_proceeds_would_have_been_wrong):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All ledger-dividend / basis-backfill checks passed.")
