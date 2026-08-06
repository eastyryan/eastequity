"""F1 regression: a broker-side TERMINAL rejection must not kill forced-exit
retry for the rest of the day. Pure Python, no network, temp state:

    EE_BROKER=simulation python3 tests/test_exec_audit_forced_exit_retry.py

Regression context (audited 2026-08-05): forced_exit_client_order_id keys a
protective sell on ticker+ET-day+reason, and place_order's duplicate-id path
treated ANY existing order under that id as "submitted" — regardless of its
status. Alpaca client ids are unique forever, including for orders that died
unfilled, so after one terminal rejection (halted name, wash-trade block, DAY
sell expired at the close) every subsequent tick resolved to the same corpse
and never re-submitted: the position sat STOPLESS until the ET day rolled the
id. The fix re-submits dead unfilled SELL duplicates under deterministic -rN
suffixes (cross-node collision preserved per attempt) while live/filled/partial
duplicates still short-circuit exactly as before, and BUYs keep the old
resolve-as-submitted behaviour (re-submitting a stale BUY would OPEN risk).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Simulation pin + fake transport: nothing here may ever reach a real broker.
os.environ["EE_BROKER"] = "simulation"
os.environ["EE_BROKER_FORCE_DIRECT"] = "1"   # module thinks it has egress; _req is a fake
os.environ.pop("EE_BROKER_FORCE_INTENT", None)

import journal as _journal                     # noqa: E402
from execution import alpaca_broker as ab      # noqa: E402
from execution import simulated_broker as sb   # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ee-exec-audit-f1-"))
sb.STATE_FILE = TMP / "portfolio.json"
ab.INTENTS_FILE = TMP / "order_intents.json"
_journal.JOURNAL = TMP / "journal"

ab._probe_cache["ok"] = True
ab._cfg = lambda: {**ab._DEFAULTS, "poll_interval_seconds": 0.01,
                   "buy_poll_timeout_seconds": 0.02,
                   "sell_poll_timeout_seconds": 0.02,
                   "resting_stops": False}
ab._starting_capital = lambda: 0.0        # ledger cross-check off
ab._live_last_trade = lambda t: None
ab.time.sleep = lambda *_: None

BASE = "EEXIT-NVDA-2026-08-05-stop-loss-breached"

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class Fake:
    """Minimal scripted Alpaca double: orders keyed by client id, 409 on reuse."""

    def __init__(self):
        self.orders = {}
        self.submits = []
        self.autofill_sells = False
        self._seq = 0

    def seed(self, cid, status, filled_qty="0", **kw):
        self._seq += 1
        o = {"id": f"srv-{self._seq}", "client_order_id": cid,
             "symbol": kw.pop("symbol", "NVDA"), "side": kw.pop("side", "sell"),
             "type": kw.pop("type", "market"), "status": status,
             "filled_qty": filled_qty}
        o.update(kw)
        self.orders[cid] = o
        return o

    def __call__(self, method, path, *, params=None, body=None, timeout=10):
        if path == "/v2/clock":
            return 200, {"is_open": True}
        if path == "/v2/account":
            return 200, {"cash": "10000", "equity": "10500"}
        if path == "/v2/positions" and method == "GET":
            return 200, [{"symbol": "NVDA", "qty": "5", "qty_available": "5",
                          "avg_entry_price": "100", "current_price": "95",
                          "market_value": "475"}]
        if path.startswith("/v2/positions/") and method == "GET":
            return 200, {"symbol": path.rsplit("/", 1)[1], "qty": "5",
                         "qty_available": "5", "avg_entry_price": "100",
                         "current_price": "95", "market_value": "475"}
        if path == "/v2/orders" and method == "GET":
            return 200, []
        if path == "/v2/orders" and method == "POST":
            self.submits.append(dict(body))
            cid = body["client_order_id"]
            if cid in self.orders:      # Alpaca: client ids are unique FOREVER
                return 409, {"message": "client_order_id must be unique",
                             "client_order_id": cid}
            self._seq += 1
            o = {"id": f"srv-{self._seq}", "client_order_id": cid,
                 "status": "new", "filled_qty": "0"}
            o.update(body)
            if self.autofill_sells and body.get("side") == "sell":
                o.update({"status": "filled", "filled_qty": body.get("qty", "5"),
                          "filled_avg_price": "95.0",
                          "filled_at": "2026-08-05T14:00:00Z"})
            self.orders[cid] = o
            return 200, o
        if path == "/v2/orders:by_client_order_id":
            o = self.orders.get((params or {}).get("client_order_id"))
            return (200, o) if o else (404, None)
        if method == "DELETE":
            return 204, None
        return 404, None


def _reset_state():
    (TMP / "portfolio.json").write_text(json.dumps({
        "cash_usd": 10000.0, "total_equity_usd": 10475.0,
        "positions": [{"ticker": "NVDA", "quantity": 5.0, "avg_cost": 100.0,
                       "market_value_usd": 475.0}],
        "pending_orders": {}, "history": [],
    }))


def _sell_order():
    return {"ticker": "NVDA", "action": "SELL_TO_CLOSE",
            "reference_price": 95.0, "proposal_id": "tick-1",
            "forced_exit_reason": "stop_loss_breached",
            "client_order_id": BASE}


def test_dead_duplicate_resubmits_under_r2():
    print("THE REGRESSION: terminally rejected forced exit must retry, not wedge:")
    _reset_state()
    fake = Fake()
    ab._req = fake
    fake.seed(BASE, "rejected")               # the corpse squatting on the id
    fake.autofill_sells = True
    order = ab.place_order(_sell_order())
    check("status is submitted", order["status"] == "submitted", order["status"])
    check("retry id is base-r2", order["order_id"] == BASE + "-r2",
          str(order["order_id"]))
    check("client_order_id moved with it",
          order["client_order_id"] == BASE + "-r2")
    check("retry provenance recorded",
          order.get("retry_of_client_order_id") == BASE)
    check("a real second submit reached the broker",
          any(b.get("client_order_id") == BASE + "-r2" for b in fake.submits))
    st = json.loads((TMP / "portfolio.json").read_text())
    check("pending stashed under the RETRY id (readback polls what it is handed)",
          BASE + "-r2" in (st.get("pending_orders") or {}),
          str(list((st.get("pending_orders") or {}).keys())))
    fill = ab.readback(order["order_id"])
    check("readback resolves the retry to a fill",
          (fill or {}).get("status") == "filled", str((fill or {}).get("status")))
    check("the fill is the retry order, not the corpse",
          (fill or {}).get("client_order_id") == BASE + "-r2")


def test_walk_skips_already_dead_retries():
    print("a dead -r2 (yesterday's expired retry attempt) walks on to -r3:")
    _reset_state()
    fake = Fake()
    ab._req = fake
    fake.seed(BASE, "rejected")
    fake.seed(BASE + "-r2", "expired")
    fake.autofill_sells = True
    order = ab.place_order(_sell_order())
    check("resolves to -r3", order["order_id"] == BASE + "-r3",
          str(order["order_id"]))
    check("status is submitted", order["status"] == "submitted", order["status"])


def test_filled_duplicate_still_short_circuits():
    print("TRUE IDEMPOTENCY KEPT: a FILLED duplicate must never sell again:")
    _reset_state()
    fake = Fake()
    ab._req = fake
    fake.seed(BASE, "filled", filled_qty="5", filled_avg_price="95.0",
              filled_at="2026-08-05T13:00:00Z")
    order = ab.place_order(_sell_order())
    check("resolves to the base id", order["order_id"] == BASE)
    check("status is submitted", order["status"] == "submitted", order["status"])
    check("no retry submit was sent",
          not any(str(b.get("client_order_id", "")).startswith(BASE + "-r")
                  for b in fake.submits))


def test_live_duplicate_still_short_circuits():
    print("a LIVE duplicate (other node's working sell) short-circuits:")
    _reset_state()
    fake = Fake()
    ab._req = fake
    fake.seed(BASE, "new")
    order = ab.place_order(_sell_order())
    check("resolves to the base id", order["order_id"] == BASE)
    check("no retry submit was sent",
          not any(str(b.get("client_order_id", "")).startswith(BASE + "-r")
                  for b in fake.submits))


def test_partial_fill_on_dead_order_short_circuits():
    print("canceled WITH a partial fill is not retryable (would over-sell):")
    _reset_state()
    fake = Fake()
    ab._req = fake
    fake.seed(BASE, "canceled", filled_qty="2", filled_avg_price="95.0")
    order = ab.place_order(_sell_order())
    check("resolves to the base id", order["order_id"] == BASE)
    check("no retry submit was sent",
          not any(str(b.get("client_order_id", "")).startswith(BASE + "-r")
                  for b in fake.submits))


def test_other_nodes_live_retry_is_adopted_not_duplicated():
    print("cross-node collision preserved on the retry id itself:")
    _reset_state()
    fake = Fake()
    ab._req = fake
    fake.seed(BASE, "rejected")
    fake.seed(BASE + "-r2", "new")            # the other node already retried
    order = ab.place_order(_sell_order())
    check("adopts the other node's -r2", order["order_id"] == BASE + "-r2")
    check("does NOT submit a second order under -r2",
          not any(str(b.get("client_order_id", "")).startswith(BASE + "-r")
                  for b in fake.submits))


def test_buy_duplicates_keep_old_semantics():
    print("BUYs never retry (re-submitting a stale BUY would OPEN risk):")
    _reset_state()
    fake = Fake()
    ab._req = fake
    buy_base = "EE-buy-idempotent-1"
    fake.seed(buy_base, "canceled", side="buy")
    order = ab.place_order({"ticker": "NVDA", "action": "BUY",
                            "position_size_usd": 500.0,
                            "reference_price": 100.0, "proposal_id": "run-x",
                            "client_order_id": buy_base})
    check("resolves to the dead order as before",
          order["order_id"] == buy_base and order["status"] == "submitted",
          f"{order.get('order_id')} {order.get('status')}")
    check("no retry submit was sent",
          not any(str(b.get("client_order_id", "")).startswith(buy_base + "-r")
                  for b in fake.submits))
    fill = ab.readback(order["order_id"])
    check("readback reports the honest terminal status",
          (fill or {}).get("status") == "rejected_unfilled_canceled",
          str((fill or {}).get("status")))


def test_exhausted_walk_goes_loud_not_silent():
    print("all retry ids dead -> recorded dead order, not an infinite loop:")
    _reset_state()
    fake = Fake()
    ab._req = fake
    fake.seed(BASE, "rejected")
    for n in range(2, ab._MAX_DUP_RETRY_SUFFIX + 1):
        fake.seed(f"{BASE}-r{n}", "rejected")
    order = ab.place_order(_sell_order())
    check("returns a rejected_submit status",
          str(order["status"]).startswith("rejected_submit"), order["status"])


if __name__ == "__main__":
    for fn in (test_dead_duplicate_resubmits_under_r2,
               test_walk_skips_already_dead_retries,
               test_filled_duplicate_still_short_circuits,
               test_live_duplicate_still_short_circuits,
               test_partial_fill_on_dead_order_short_circuits,
               test_other_nodes_live_retry_is_adopted_not_duplicated,
               test_buy_duplicates_keep_old_semantics,
               test_exhausted_walk_goes_loud_not_silent):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All forced-exit-retry checks passed.")
