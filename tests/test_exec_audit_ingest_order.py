"""F11b regression: the ingested-broker-orders memo must trim by INSERTION
order, not lexicographically. Pure Python, no network, temp state:

    EE_BROKER=simulation python3 tests/test_exec_audit_ingest_order.py

Regression context (audited 2026-08-05): ingest_out_of_band_fills persisted its
dedupe memo as `sorted(seen)[-500:]`. Sorting a set of client ids is
lexicographic, so once the memo overflowed, which ids survived depended on how
their TEXT sorted — a freshly ingested id that happened to sort low (any
"EE-..." uuid against an old "EESTOP-..." id, for instance) could be evicted
immediately, while ancient high-sorting ids lived forever. An evicted-but-
recent id is exactly the one the next sweep's lookback window sees again, at
which point dedupe rests entirely on the other layers. The memo is now an
insertion-ordered list: oldest ingested ids are dropped first.
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
from execution import reconcile_runner         # noqa: E402
from execution import simulated_broker as sb   # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ee-exec-audit-f11b-"))
sb.STATE_FILE = TMP / "portfolio.json"
ab.INTENTS_FILE = TMP / "order_intents.json"
_journal.JOURNAL = TMP / "journal"
reconcile_runner.ROOT = TMP                    # journal-dedupe reads tmp, not repo

ab._probe_cache["ok"] = True
ab._cfg = lambda: dict(ab._DEFAULTS)
ab._starting_capital = lambda: 0.0
ab.time.sleep = lambda *_: None

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class Fake:
    """Broker double whose closed-order sweep returns two fresh sell fills whose
    ids sort lexicographically BELOW every stored id — the shape the old
    sorted() trim evicted immediately."""

    def __init__(self, closed):
        self.closed = closed

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
            if (params or {}).get("status") == "closed":
                return 200, list(self.closed)
            return 200, []
        if method == "DELETE":
            return 204, None
        return 404, None


def _closed_sell(cid, srv):
    return {"id": srv, "client_order_id": cid, "symbol": "XYZ", "side": "sell",
            "type": "market", "status": "filled", "filled_qty": "1",
            "filled_avg_price": "10.0", "filled_at": "2026-08-05T08:00:00Z",
            "submitted_at": "2026-08-05T07:59:00Z"}


def test_trim_preserves_insertion_order():
    print("memo at capacity: oldest-inserted evicted, newest kept at the end:")
    # 499 stored ids, inserted oldest-first; every one sorts ABOVE "a-...".
    stored = ["z-oldest"] + [f"m-{i:03d}" for i in range(498)]
    (TMP / "portfolio.json").write_text(json.dumps({
        "cash_usd": 10000.0, "total_equity_usd": 10000.0, "positions": [],
        "pending_orders": {}, "history": [],
        "ingested_broker_orders": stored,
    }))
    fake = Fake([_closed_sell("a-new-1", "srv-n1"),
                 _closed_sell("a-new-2", "srv-n2")])
    ab._req = fake

    done = ab.ingest_out_of_band_fills()

    check("both fresh fills ingested", len(done) == 2, str(len(done)))
    memo = json.loads((TMP / "portfolio.json").read_text())[
        "ingested_broker_orders"]
    check("memo capped at 500", len(memo) == 500, str(len(memo)))
    check("OLDEST-inserted id evicted ('z-oldest')", "z-oldest" not in memo)
    # THE REGRESSION: sorted(seen)[-500:] keeps the 500 lexicographically
    # LARGEST ids, so "a-new-1"/"a-new-2" (the smallest) were the ones evicted.
    check("newest ids survived the trim",
          "a-new-1" in memo and "a-new-2" in memo)
    check("newest ids sit at the END (insertion order)",
          memo[-2:] == ["a-new-1", "a-new-2"], str(memo[-2:]))
    check("stored prefix order intact", memo[0] == "m-000", memo[0])


def test_second_sweep_is_still_deduped():
    print("the surviving memo actually dedupes the next sweep:")
    fake = Fake([_closed_sell("a-new-1", "srv-n1"),
                 _closed_sell("a-new-2", "srv-n2")])
    ab._req = fake
    done = ab.ingest_out_of_band_fills()
    check("nothing re-ingested", done == [], str(done))


if __name__ == "__main__":
    test_trim_preserves_insertion_order()
    test_second_sweep_is_still_deduped()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All ingest-order checks passed.")
