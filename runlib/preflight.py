"""Safety preflight + run locks (extracted from orchestrator).

Keeps the supervisor entrypoint thinner. Behavior is identical to the prior
inline implementation (fail-open lease, stale lock reclaim, daily budget).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import validator

ROOT = Path(__file__).resolve().parent.parent

LOCK_FILE = ROOT / "state" / "RUN_LOCK"
LOCK_STALE_SECONDS = 45 * 60


def _lock_holder() -> tuple[str | None, int | None]:
    """(run_id, pid) recorded in the lock file. Either may be None on an old-format
    or unreadable lock, which callers must treat as "unknown", never as "mine"."""
    try:
        parts = LOCK_FILE.read_text().strip().split()
    except OSError:
        return None, None
    run_id = parts[0] if parts else None
    pid = None
    if len(parts) > 1:
        try:
            pid = int(parts[1])
        except ValueError:
            pid = None
    return run_id, pid


def _pid_alive(pid: int | None) -> bool:
    """Is a process with this pid running? Unknown pid -> assume ALIVE.

    Erring toward alive means a lock of unknown provenance is respected rather than
    stolen, which is the safe direction: two concurrent cycles trade on stale
    portfolio snapshots.
    """
    if pid is None:
        return True
    try:
        os.kill(pid, 0)          # signal 0 = liveness probe, no signal delivered
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # exists, owned by another user
    except OSError:
        return True


def acquire_run_lock(run_id: str) -> bool:
    """One run at a time: concurrent cycles trade on stale portfolio snapshots.

    Uses O_EXCL, which is atomic at the filesystem level. The previous version was
    check-then-write — exists() at one line, write_text() four lines later — a
    textbook TOCTOU window in which two processes microseconds apart both saw no
    lock and both proceeded. That matters more now that the 5-minute stop watcher
    can run concurrently with a scheduled trading cycle.

    LIVENESS (added 2026-07-19, after reproducing the failure live). The lock was
    released only by an atexit handler, whose comment claimed it fired "on every exit
    path, crashes included". atexit does NOT run on SIGKILL, and a weekly-market run
    killed mid-flight left a lock naming a dead process. The only recovery was the
    45-minute staleness timer, so a run killed at the 09:00 slot would have blocked
    every cycle until ~09:45 — two lost trading slots on a seven-slot day, with stop
    enforcement offline for the same window.

    The lock now records the PID alongside the run id, and a lock whose holder is
    gone is reclaimed immediately. The age-based steal is kept as a backstop for
    cross-node and PID-reuse cases, where liveness cannot be established.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        held_run, held_pid = _lock_holder()
        if not _pid_alive(held_pid):
            print(f"  (reclaiming run lock from dead process {held_pid}"
                  f" [run {held_run}])")
        else:
            try:
                age = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
            except OSError:
                return False
            if age < LOCK_STALE_SECONDS:
                return False
            print(f"  (clearing stale run lock, {age/60:.0f} min old)")
        LOCK_FILE.unlink(missing_ok=True)
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False  # lost the race to another reaper — stand down
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{run_id} {os.getpid()}")
    import atexit
    atexit.register(release_run_lock, run_id)
    return True


def release_run_lock(run_id: str | None = None) -> None:
    """Release the lock, but ONLY if it is still ours.

    The unconditional unlink was a real hazard: if process B reclaimed a lock while
    A was still alive, A's atexit handler would delete B's lock and admit a third
    run. Passing the run id makes release idempotent and ownership-checked.
    """
    if run_id is not None:
        held_run, _ = _lock_holder()
        if held_run is not None and held_run != run_id:
            return  # not ours any more — leave it alone
    LOCK_FILE.unlink(missing_ok=True)


def _node_id() -> str:
    """Best-effort stable id for this execution node (cloud sandbox vs a Mac)."""
    import socket
    if os.environ.get("EE_NODE"):
        return os.environ["EE_NODE"]
    try:
        return socket.gethostname() or "unknown-node"
    except Exception:
        return "unknown-node"


def _read_remote_lease() -> dict | None:
    """The lease as committed on origin/main (the shared source of truth). Fail-open:
    returns None on any git/parse error so a lease can never brick the trader."""
    try:
        subprocess.run(["git", "fetch", "origin", "main"], cwd=ROOT,
                       capture_output=True, timeout=60)
        r = subprocess.run(["git", "show", "origin/main:state/RUN_LEASE.json"],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception as e:
        print(f"  (cross-node lease read failed, proceeding fail-open: {e})")
    return None


def acquire_cross_node_lease(run_id: str, cfg: dict, manual: bool = False) -> str | None:
    """Advisory cross-node lease over the shared ledger, arbitrated through git.

    Returns a halt reason if another NODE holds an unexpired lease (scheduled runs
    stand down; manual runs only warn), else None after claiming the lease. Fail-OPEN
    on any git/parse error - the lease only prevents the clear double-trade case, it
    must never prevent trading because of an infra hiccup."""
    rc = cfg.get("risk_controls", {})
    if not rc.get("cross_node_lease_enabled", True):
        return None
    ttl_min = rc.get("cross_node_lease_ttl_minutes", 30)
    now = datetime.now(timezone.utc)
    lease_path = ROOT / "state" / "RUN_LEASE.json"

    def _conflict(lease: dict | None) -> str | None:
        if not lease:
            return None
        try:
            exp = datetime.fromisoformat(lease["expires_at"])
        except Exception:
            return None  # unparseable -> fail open
        if exp > now and lease.get("run_id") not in (None, run_id) \
                and lease.get("holder") != _node_id():
            return (f"cross-node lease held by {lease.get('holder')} "
                    f"(run {lease.get('run_id')}) until {exp.isoformat()}")

    conflict = _conflict(_read_remote_lease())
    if conflict:
        if manual:
            print(f"  WARNING: {conflict} - proceeding anyway (manual run).")
        else:
            return f"{conflict} - standing down to avoid double-trading the ledger"

    # Claim the lease and push it so the other node can see it before it trades.
    lease = {"holder": _node_id(), "run_id": run_id, "acquired_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=ttl_min)).isoformat()}
    try:
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps(lease, indent=2))
        subprocess.run(["git", "add", "state/RUN_LEASE.json"], cwd=ROOT, capture_output=True)
        c = subprocess.run(["git", "commit", "-m", "Acquire run lease [vercel skip]"],
                           cwd=ROOT, capture_output=True, text=True)
        if c.returncode == 0:
            p = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                               capture_output=True, text=True, timeout=120)
            if p.returncode != 0:  # lost the push race - rebase and re-check the holder
                rb = subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=120)
                if rb.returncode != 0:
                    subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, capture_output=True)
                    print("  (lease push race, rebase failed - proceeding fail-open)")
                    return None
                conflict = _conflict(_read_remote_lease())
                if conflict and not manual:
                    return f"lost the lease race: {conflict} - standing down"
                subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                               capture_output=True, timeout=120)
    except Exception as e:
        print(f"  (cross-node lease claim failed, proceeding fail-open: {e})")
    return None


def preflight(cfg: dict, run_id: str, news_only: bool = False,
              manual: bool = False) -> str | None:
    """Return a halt reason string, or None if clear to proceed."""
    if validator.kill_switch_active(cfg):
        return "KILL_SWITCH file present — no runs until it is removed"
    if not news_only and datetime.now().strftime("%a") not in cfg["schedule"]["run_days"]:
        return "not a configured run day (weekend/holiday guard)"
    # The weekday check above cannot see HOLIDAYS -- its own message claimed a holiday
    # guard that did not exist. On Christmas or Thanksgiving a full trading cycle ran,
    # burned budget and researched against the prior session. Fail-SAFE: a calendar we
    # could not fetch answers "assumed", and an assumption never halts a run.
    if not news_only:
        try:
            from tools.market_calendar import session as _session
            _s = _session()
            if _s["source"] == "calendar" and not _s["is_trading_day"]:
                return "market holiday (NYSE calendar) - no trading session today"
        except Exception:
            pass
    # Usage budget: hard cap on SCHEDULED runs per day so the automation can never
    # drain the subscription's usage pool. Manual (user-initiated) runs are marked
    # in the journal and neither count toward nor are blocked by the budget.
    if not manual:
        cap = cfg["schedule"].get("max_completed_runs_per_day", 12)
        runs_file = ROOT / "journal" / "runs" / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"
        if runs_file.exists():
            completed = 0
            for line in runs_file.read_text().splitlines():
                # journal.py appends non-atomically, so a crash mid-write leaves a
                # truncated line. An unguarded json.loads here took the ENTIRE system
                # dark: this runs before the lock and before the safety layer, so one
                # bad byte meant every subsequent run died in preflight with stops
                # unenforced. Count an unparseable line as a completed run — it is
                # almost certainly a partial record of one, and erring toward the cap
                # protects the usage budget rather than spending it.
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    completed += 1
                    continue
                if "halted" not in rec and not rec.get("manual"):
                    completed += 1
            if completed >= cap:
                return f"daily run budget exhausted ({completed}/{cap}) - protecting usage limits"
    if not acquire_run_lock(run_id):
        return "another run is already in progress (RUN_LOCK held)"
    # Cross-node lease: a scheduled run that trades the shared ledger stands down if
    # another node (cloud vs local) already holds it. Skipped for news-only (never trades).
    if not news_only:
        lease_halt = acquire_cross_node_lease(run_id, cfg, manual=manual)
        if lease_halt:
            return lease_halt
    return None


# ---------------------------------------------------------------------------
# Steps 2 — Context gathering (all deterministic Python, all fail-soft)
# ---------------------------------------------------------------------------
