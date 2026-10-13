"""2026-10-13 fixes.

1. The NOW stop: trail 125.3915 against a working 0.791986698 @ 125.39 DAY stop.
   The raw-float compare PATCHed a no-op, and Alpaca answered 422 "qty must be an
   integer" (it cannot PATCH fractional orders) on every tick.
2. A real ratchet on a fractional stop (COST) goes through the replacement path,
   as a tif change does, instead of a PATCH that can never succeed.
3. The lease retry rebases with --autostash, so a dirty act-time tree no longer
   leaves the lease unpushed and the act on a stale ledger base (the dead 06:00 run).
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from execution import alpaca_broker as ab  # noqa: E402
from test_stop_qty_and_replace_safety import (  # noqa: E402,F401
    NOW_QTY, StrictAlpaca, _base_api, _journal_blob, _seed, api)

COST_QTY = "0.167365847"


def test_now_sub_cent_trail_is_a_no_op_and_keeps_125_39(api, tmp_path):
    api.set_position("NOW", float(NOW_QTY), 131.0, 140.93)
    api.positions[0]["qty"] = api.positions[0]["qty_available"] = NOW_QTY
    live = api.add_stop("NOW", NOW_QTY, 125.39, "day", "EESTOP-NOW-a678aee6dd")
    _seed(trail=125.3915, protective_stop={
        "status": "resting", "stop_price": 125.39, "qty": float(NOW_QTY),
        "time_in_force": "day", "client_order_id": "EESTOP-NOW-a678aee6dd",
        "alpaca_order_id": live["id"]})
    api.reject_patch = (422, {"code": 42210000, "message": "qty must be an integer"})

    rec = ab.ensure_protective_stop("NOW", reason="stop_watch_tick")

    assert api.events == [], api.events
    assert rec["status"] == "resting" and rec["stop_price"] == 125.39
    assert rec["client_order_id"] == "EESTOP-NOW-a678aee6dd"
    assert not rec.get("replace_refused")
    assert "REPLACE_REFUSED" not in _journal_blob(tmp_path)


def test_a_fractional_ratchet_is_replaced_not_patched(api, tmp_path):
    api.set_position("COST", float(COST_QTY), 914.4, 963.76)
    api.positions[0]["qty"] = api.positions[0]["qty_available"] = COST_QTY
    old = api.add_stop("COST", COST_QTY, 906.26, "day", "EESTOP-COST-old")
    _seed("COST", qty=0.167366, plan_stop=888.0, trail=916.9321, avg=914.4,
          protective_stop={"status": "resting", "stop_price": 906.26,
                           "qty": float(COST_QTY), "time_in_force": "day",
                           "client_order_id": "EESTOP-COST-old",
                           "alpaca_order_id": old["id"]})
    api.reject_patch = (422, {"message": "qty must be an integer"})

    rec = ab.ensure_protective_stop("COST", reason="trail_ratchet")

    assert not [e for e in api.events if e[0] == "PATCH"], api.events
    working = api.working_stops()
    assert len(working) == 1
    assert working[0]["stop_price"] == "916.93" and working[0]["qty"] == COST_QTY
    assert rec["status"] == "resting" and rec["stop_price"] == 916.9321


def test_whole_share_stops_still_patch(api):
    api.set_position("NVDA", 5.0, 100.0, 110.0)
    old = api.add_stop("NVDA", 5, 90.0, "gtc", "EESTOP-NVDA-old")
    _seed("NVDA", qty=5.0, plan_stop=90.0, trail=104.25, avg=100.0,
          protective_stop={"status": "resting", "stop_price": 90.0, "qty": 5.0,
                           "time_in_force": "gtc", "client_order_id": "EESTOP-NVDA-old",
                           "alpaca_order_id": old["id"]})
    ab.ensure_protective_stop("NVDA", reason="trail_ratchet")
    assert ("PATCH", old["id"]) in api.events


# ---- lease rebase on a dirty tree ------------------------------------------------

def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def test_lease_rebase_survives_a_dirty_tree(tmp_path, monkeypatch):
    from runlib import preflight
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    a, b = tmp_path / "run", tmp_path / "stopwatch"
    for d in (a, b):
        _git(tmp_path, "clone", "-q", str(origin), str(d))
        _git(d, "config", "user.email", "t@t"); _git(d, "config", "user.name", "t")
    (a / "universe.json").write_text("v1"); (a / "portfolio.json").write_text("p1")
    _git(a, "add", "."); _git(a, "commit", "-qm", "seed"); _git(a, "push", "-q", "origin", "main")
    _git(b, "pull", "-q", "origin", "main")
    (b / "portfolio.json").write_text("p2-stopwatch")          # stop-watch lands first
    _git(b, "commit", "-qam", "stop watch"); _git(b, "push", "-q", "origin", "main")
    (a / "universe.json").write_text("v2-gather")               # gather leaves the tree dirty
    (a / "RUN_LEASE.json").write_text("{}")
    _git(a, "add", "RUN_LEASE.json"); _git(a, "commit", "-qm", "Acquire run lease")

    monkeypatch.setattr(preflight, "ROOT", a)
    rb = preflight._pull_rebase_autostash()

    assert rb.returncode == 0, rb.stderr
    assert (a / "portfolio.json").read_text() == "p2-stopwatch"  # act sees origin's ledger
    assert (a / "universe.json").read_text() == "v2-gather"      # gather output kept
    _git(a, "push", "-q", "origin", "main")                      # lease now publishes
