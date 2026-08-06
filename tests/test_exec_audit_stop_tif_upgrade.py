"""F6 regression: a DAY protective stop must upgrade to GTC when the position
grows a whole-share leg. Pure Python, no network, temp state:

    EE_BROKER=simulation python3 tests/test_exec_audit_stop_tif_upgrade.py

Regression context (audited 2026-08-05): _stop_shape's first branch pinned the
time_in_force to the EXISTING order's "day" forever, and the cancel-and-rewrite
path in ensure_protective_stop only fires when the wanted tif differs from the
current one — but wanted was computed AS the current one. A position opened
under one share (the live book's BKR, 0.995 sh) that later scaled past a whole
share would have kept session-only protection for life: no overnight cover, no
weekend cover, re-armed daily instead of resting GTC. The desired stop shape is
now a function of the CURRENT quantity alone.
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

TMP = Path(tempfile.mkdtemp(prefix="ee-exec-audit-f6-"))
sb.STATE_FILE = TMP / "portfolio.json"
ab.INTENTS_FILE = TMP / "order_intents.json"
_journal.JOURNAL = TMP / "journal"

ab._probe_cache["ok"] = True
ab._cfg = lambda: {**ab._DEFAULTS, "resting_stops": True}
ab._starting_capital = lambda: 0.0
ab.time.sleep = lambda *_: None

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class Fake:
    """Alpaca double for the stop lifecycle: submit/patch/cancel/lookup."""

    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty
        self.orders = {}
        self.submits = []
        self.patches = []
        self.cancels = []
        self._seq = 0

    def seed_stop(self, cid, qty, stop_price, tif):
        self._seq += 1
        o = {"id": f"srv-{self._seq}", "client_order_id": cid,
             "symbol": self.symbol, "side": "sell", "type": "stop",
             "status": "new", "filled_qty": "0", "qty": str(qty),
             "stop_price": str(stop_price), "time_in_force": tif}
        self.orders[cid] = o
        return o

    def __call__(self, method, path, *, params=None, body=None, timeout=10):
        if path == "/v2/clock":
            return 200, {"is_open": True}
        if path == f"/v2/positions/{self.symbol}" and method == "GET":
            return 200, {"symbol": self.symbol, "qty": str(self.qty),
                         "qty_available": "0",
                         "avg_entry_price": "100", "current_price": "100",
                         "market_value": str(round(self.qty * 100, 2))}
        if path == "/v2/orders" and method == "GET":
            return 200, [o for o in self.orders.values() if o["status"] == "new"]
        if path == "/v2/orders:by_client_order_id":
            o = self.orders.get((params or {}).get("client_order_id"))
            return (200, o) if o else (404, None)
        if path == "/v2/orders" and method == "POST":
            self.submits.append(dict(body))
            self._seq += 1
            o = {"id": f"srv-{self._seq}", "client_order_id": body["client_order_id"],
                 "status": "new", "filled_qty": "0"}
            o.update(body)
            self.orders[body["client_order_id"]] = o
            return 200, o
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
                if o["id"] == oid:
                    o["status"] = "canceled"
            return 204, None
        return 404, None


def _seed_mirror(ticker, qty, stop, rec):
    (TMP / "portfolio.json").write_text(json.dumps({
        "cash_usd": 1000.0, "total_equity_usd": 1000.0 + qty * 100,
        "positions": [{"ticker": ticker, "quantity": qty, "avg_cost": 100.0,
                       "market_value_usd": round(qty * 100, 2),
                       "plan": {"stop_loss": stop, "target_price": 130.0,
                                "holding_horizon_days": 30},
                       "protective_stop": rec}],
        "pending_orders": {}, "history": [],
    }))


def test_stop_shape_is_a_function_of_quantity_alone():
    print("_stop_shape derives from the current quantity, nothing else:")
    check("5.37 sh -> 5 whole GTC + 0.37 disclosed",
          ab._stop_shape(5.37) == (5.0, "gtc", 0.37), str(ab._stop_shape(5.37)))
    check("0.995 sh -> full-qty DAY (no whole-share leg exists)",
          ab._stop_shape(0.995) == (0.995, "day", 0.0), str(ab._stop_shape(0.995)))
    check("exactly 1.0 sh -> GTC with no tail",
          ab._stop_shape(1.0) == (1.0, "gtc", 0.0), str(ab._stop_shape(1.0)))


def test_day_stop_upgrades_to_gtc_when_position_grows():
    print("THE REGRESSION: sub-share DAY stop, position scales to 5.37 sh:")
    fake = Fake("NVDA", 5.37)
    ab._req = fake
    old = fake.seed_stop("EESTOP-NVDA-old", 0.995, 90.0, "day")
    _seed_mirror("NVDA", 5.37, 90.0, {
        "status": "resting", "stop_price": 90.0, "qty": 0.995,
        "uncovered_qty": 0.0, "session_only": True, "time_in_force": "day",
        "client_order_id": "EESTOP-NVDA-old", "alpaca_order_id": old["id"],
        "armed_at": "2026-08-04T14:00:00Z"})

    rec = ab.ensure_protective_stop("NVDA", reason="test_upgrade")

    check("old DAY order canceled", old["id"] in fake.cancels, str(fake.cancels))
    gtc = [b for b in fake.submits
           if b.get("type") == "stop" and b.get("time_in_force") == "gtc"]
    check("a GTC stop was submitted", len(gtc) == 1, str(fake.submits))
    check("GTC leg covers the 5 whole shares",
          gtc and float(gtc[0]["qty"]) == 5.0, str(gtc))
    check("level preserved (never lowered)",
          gtc and float(gtc[0]["stop_price"]) == 90.0, str(gtc))
    check("record says gtc / not session_only",
          (rec or {}).get("time_in_force") == "gtc"
          and (rec or {}).get("session_only") is False, str(rec))
    check("0.37 tail disclosed as uncovered",
          abs(float((rec or {}).get("uncovered_qty", 0)) - 0.37) < 1e-9, str(rec))
    pos = sb._load()["positions"][0]
    check("mirror carries the upgraded record",
          pos["protective_stop"]["time_in_force"] == "gtc")


def test_sub_share_day_stop_does_not_churn():
    print("still sub-one-share: DAY stop left alone (no cancel/rewrite loop):")
    fake = Fake("BKR", 0.995)
    ab._req = fake
    old = fake.seed_stop("EESTOP-BKR-old", 0.995, 41.0, "day")
    _seed_mirror("BKR", 0.995, 41.0, {
        "status": "resting", "stop_price": 41.0, "qty": 0.995,
        "uncovered_qty": 0.0, "session_only": True, "time_in_force": "day",
        "client_order_id": "EESTOP-BKR-old", "alpaca_order_id": old["id"],
        "armed_at": "2026-08-05T10:00:00Z"})

    rec = ab.ensure_protective_stop("BKR", reason="stop_watch_tick")

    check("no cancel", fake.cancels == [], str(fake.cancels))
    check("no patch", fake.patches == [], str(fake.patches))
    check("no fresh submit", fake.submits == [], str(fake.submits))
    check("record still session-only DAY",
          (rec or {}).get("time_in_force") == "day"
          and (rec or {}).get("session_only") is True, str(rec))


if __name__ == "__main__":
    for fn in (test_stop_shape_is_a_function_of_quantity_alone,
               test_day_stop_upgrades_to_gtc_when_position_grows,
               test_sub_share_day_stop_does_not_churn):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All stop-tif-upgrade checks passed.")
