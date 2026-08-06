"""F2 + F3 regressions: stop_watch's ledger-writing maintenance must run under
the run lock, and must best-effort sync from origin first. Pure Python, temp
lock file and temp git repos, no network, no state/ writes:

    EE_BROKER=simulation python3 tests/test_exec_audit_stop_watch_lock.py

F2 (audited 2026-08-05): the module docstring claimed "TAKES THE RUN LOCK",
but _broker_maintenance — which WRITES state/portfolio.json via run_reconcile
and ensure_protective_stop — ran before the lock was ever acquired; only
forced-exit execution locked. A trading cycle's apply_corporate_actions
read-modify-write could load before the tick's write and save after it,
silently reverting a tick-ingested fill or a fresh protective_stop record.
Maintenance now locks first and DEFERS when a cycle holds the lock.

F3 (audited 2026-08-05): every fill-ingestion idempotency layer is
per-checkout; the Actions executor (fresh clone) and this local watchdog could
both apply the same overnight stop fill before either pushed. Mitigation: a
best-effort `git fetch` + `--ff-only` merge BEFORE reconciling, strictly
fail-open — a sync failure must never brick the safety tick.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EE_BROKER"] = "simulation"

from runlib import preflight                   # noqa: E402
from scripts import stop_watch as sw           # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ee-exec-audit-f2f3-"))

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ---- F2: the run lock around maintenance ---------------------------------- #
preflight.LOCK_FILE = TMP / "RUN_LOCK"
sw._cfg = lambda: {"mode": {"trading_mode": "paper"}, "schedule": {}}
sw.get_portfolio_state = lambda: {"positions": []}   # flat book: exits skipped

CALLS = {}


def _spy_maintenance(out):
    CALLS["ran"] = True
    CALLS["lock_exists"] = preflight.LOCK_FILE.exists()
    CALLS["holder"] = (preflight.LOCK_FILE.read_text()
                       if CALLS["lock_exists"] else None)


sw._broker_maintenance = _spy_maintenance


def test_maintenance_runs_under_the_lock():
    print("THE REGRESSION: maintenance must hold the run lock while it writes:")
    CALLS.clear()
    res = sw.run(dry_run=False)
    check("maintenance ran", CALLS.get("ran") is True)
    check("run lock HELD during maintenance", CALLS.get("lock_exists") is True)
    check("held by a stopwatch-maint id",
          "stopwatch-maint-" in (CALLS.get("holder") or ""),
          str(CALLS.get("holder")))
    check("lock released after the tick", not preflight.LOCK_FILE.exists())
    check("flat-book short-circuit intact",
          res.get("status") == "skipped" and res.get("note") == "flat book",
          str(res))
    check("no deferral reported", "maintenance" not in res, str(res))


def test_maintenance_defers_to_a_live_cycle():
    print("a trading cycle holds the lock -> maintenance defers, lock untouched:")
    CALLS.clear()
    # A live holder: our own pid is definitionally alive, mtime is fresh, so
    # neither the liveness reclaim nor the staleness reclaim may fire.
    preflight.LOCK_FILE.write_text(f"cycle-run {os.getpid()}")
    res = sw.run(dry_run=False)
    check("maintenance did NOT run", "ran" not in CALLS, str(CALLS))
    check("deferral reported", res.get("maintenance") == "deferred_run_lock_held",
          str(res))
    check("the cycle's lock was not touched",
          preflight.LOCK_FILE.exists()
          and preflight.LOCK_FILE.read_text().startswith("cycle-run"))
    preflight.LOCK_FILE.unlink()


def test_dry_run_takes_no_lock_and_no_maintenance():
    print("dry-run stays read-only (no lock, no maintenance):")
    CALLS.clear()
    sw.run(dry_run=True)
    check("maintenance skipped", "ran" not in CALLS, str(CALLS))
    check("no lock left behind", not preflight.LOCK_FILE.exists())


# ---- F3: fetch + ff-only sync, fail-open ---------------------------------- #
def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
         *args], capture_output=True, text=True, timeout=45)


def _make_repo_pair():
    remote = TMP / "remote"
    remote.mkdir()
    _git(TMP, "init", "-q", str(remote))
    (remote / "a.txt").write_text("one\n")
    _git(remote, "add", "a.txt")
    _git(remote, "commit", "-q", "-m", "one")
    clone = TMP / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)],
                   capture_output=True, text=True, timeout=45)
    return remote, clone


def test_sync_fast_forwards_executor_pushed_state():
    print("origin has the executor's newer commit -> local fast-forwards:")
    remote, clone = _make_repo_pair()
    (remote / "portfolio-ish.json").write_text(json.dumps({"fill": "applied"}))
    _git(remote, "add", "portfolio-ish.json")
    _git(remote, "commit", "-q", "-m", "executor fill")
    sw.ROOT = clone
    out = {}
    sw._sync_repo_ff(out)
    check("sync reported ok", out.get("git_sync") == "ok", str(out))
    check("executor's file arrived", (clone / "portfolio-ish.json").exists())


def test_divergence_fails_open_and_keeps_local_work():
    print("diverged histories -> ff refused, local commit intact, no raise:")
    remote = TMP / "remote"
    clone = TMP / "clone"
    (clone / "local.txt").write_text("local work\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-q", "-m", "local commit")
    (remote / "b.txt").write_text("remote work\n")
    _git(remote, "add", "b.txt")
    _git(remote, "commit", "-q", "-m", "remote commit")
    sw.ROOT = clone
    out = {}
    sw._sync_repo_ff(out)                      # must not raise
    check("refusal reported, not raised",
          str(out.get("git_sync", "")).startswith("ff_refused"), str(out))
    check("local commit untouched", (clone / "local.txt").exists())


def test_no_origin_fails_open():
    print("no origin remote at all -> fetch failure reported, no raise:")
    lonely = TMP / "lonely"
    lonely.mkdir()
    _git(TMP, "init", "-q", str(lonely))
    sw.ROOT = lonely
    out = {}
    sw._sync_repo_ff(out)
    check("fetch failure reported",
          str(out.get("git_sync", "")).startswith(("fetch_failed", "error")),
          str(out))


def test_sync_runs_before_reconcile_in_maintenance():
    print("wiring: the sync precedes run_reconcile inside _broker_maintenance:")
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "stop_watch.py").read_text()
    body = src[src.index("def _broker_maintenance"):src.index("def run(")]
    check("_broker_maintenance calls _sync_repo_ff", "_sync_repo_ff(" in body)
    check("...BEFORE run_reconcile",
          body.index("_sync_repo_ff(") < body.index("run_reconcile("))


if __name__ == "__main__":
    for fn in (test_maintenance_runs_under_the_lock,
               test_maintenance_defers_to_a_live_cycle,
               test_dry_run_takes_no_lock_and_no_maintenance,
               test_sync_fast_forwards_executor_pushed_state,
               test_divergence_fails_open_and_keeps_local_work,
               test_no_origin_fails_open,
               test_sync_runs_before_reconcile_in_maintenance):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All stop-watch lock/sync checks passed.")
