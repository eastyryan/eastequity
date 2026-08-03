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


def _lease_conflict(lease: dict | None, run_id: str,
                    now: datetime) -> str | None:
    """A halt reason if `lease` is another node's live claim, else None.

    LIFTED OUT OF acquire_cross_node_lease AND MADE TOTAL (2026-08-03). It used to be a
    closure whose try/except covered only the `fromisoformat` call, so the very next
    line - `exp > now` - raised TypeError on any lease whose `expires_at` parsed NAIVE
    (an older-format file, a hand-edited one, a clock without an offset). That exception
    propagated out of preflight() and killed the run before the lock, the safety layer
    or stop enforcement, which is precisely the outcome autonomy_config forbids: "a lease
    must never brick the trader". Every failure path now returns None, i.e. FAIL-OPEN.

    A lease whose `released_at` is set is a lease its holder handed back early (see
    release_cross_node_lease); the expiry it carries is the release time, so the normal
    expiry test already lets it through - no special case needed.
    """
    if not lease:
        return None
    try:
        exp = datetime.fromisoformat(str(lease["expires_at"]))
        if exp.tzinfo is None:
            # A naive stamp is UTC by construction everywhere we write one; assuming so
            # keeps the comparison meaningful instead of throwing.
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now and lease.get("run_id") not in (None, run_id) \
                and lease.get("holder") != _node_id():
            return (f"cross-node lease held by {lease.get('holder')} "
                    f"(run {lease.get('run_id')}) until {exp.isoformat()}")
    except Exception as e:
        print(f"  (unreadable lease, proceeding fail-open: {e})")
    return None


def acquire_cross_node_lease(run_id: str, cfg: dict, manual: bool = False) -> str | None:
    """Advisory cross-node lease over the shared ledger, arbitrated through git.

    Returns a halt reason if another NODE holds an unexpired lease, else None. Fail-OPEN
    on any git/parse error - the lease only prevents the clear double-trade case, it
    must never prevent trading because of an infra hiccup.

    ASYMMETRIC BY DESIGN (2026-07-20, user policy): the CLOUD routine is the only
    scheduled trader; local runs are hand-fired by the operator. So a manual run YIELDS
    but never BLOCKS - it stands down when another node holds the lease, and it never
    claims or pushes one of its own.

    Before this, `manual` only skipped the stand-down, and then EVERY run fell through
    to write/commit/push a 30-minute lease. A hand-fired local run therefore suppressed
    the next scheduled cloud run - exactly backwards, and observed on 2026-07-20 when a
    local catch-up run took the lease out from under the cloud trader mid-session. The
    scheduled trader must always win. Scheduled runs keep the original claim-and-push
    behaviour, which is what makes the stand-down meaningful in the first place."""
    rc = cfg.get("risk_controls", {})
    if not rc.get("cross_node_lease_enabled", True):
        return None
    ttl_min = rc.get("cross_node_lease_ttl_minutes", 30)
    now = datetime.now(timezone.utc)
    lease_path = ROOT / "state" / "RUN_LEASE.json"

    conflict = _lease_conflict(_read_remote_lease(), run_id, now)
    if conflict:
        if manual:
            return (f"{conflict} - the scheduled trader holds the ledger; "
                    f"re-run once it finishes")
        return f"{conflict} - standing down to avoid double-trading the ledger"

    if manual:
        # Deliberately NO claim. A hand-fired run must never be able to make the
        # scheduled cloud trader stand down. The conflict check above is the whole
        # of a manual run's participation in the lease: it reads, it yields, it
        # never writes.
        return None

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
                conflict = _lease_conflict(_read_remote_lease(), run_id, now)
                if conflict:  # manual runs never reach here - they return above
                    return f"lost the lease race: {conflict} - standing down"
                subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                               capture_output=True, timeout=120)
    except Exception as e:
        print(f"  (cross-node lease claim failed, proceeding fail-open: {e})")
    # HAND THE LEASE BACK WHEN THIS RUN ENDS (see release_cross_node_lease). Registered
    # here, in the only branch that CLAIMS, so a manual run - which never claims - can
    # never release the scheduled trader's lease.
    import atexit
    atexit.register(release_cross_node_lease, run_id)
    return None


def release_cross_node_lease(run_id: str) -> bool:
    """Expire OUR lease now that this run is finished. Returns whether it was released.

    WHY THIS EXISTS (2026-08-03). Nothing ever released the lease: acquire wrote a
    30-minute claim, pushed it, and the only way it ever cleared was the TTL running out.
    So every run left the shared ledger marked "busy" for a flat 30 minutes regardless of
    what it actually did - and the measured run length is 12.5 min at the median and 23.4
    at p90 (n=52 breadcrumb->summary pairs, 2026-07-21..08-03). Every scheduled run
    therefore spent roughly half its lease lying, and a run that DIED at minute one lied
    for the full half hour.

    Two concrete costs. A hand-fired local run inside that window is told "the scheduled
    trader holds the ledger; re-run once it finishes" about a trader that finished ten
    minutes ago. And a stale lease is a slot-eater waiting for a schedule change: the
    live slot gaps are 90-105 minutes so nothing collides today, but the 08:45 recovery
    that landed at 10:29 on 2026-07-29 and 2026-08-03 sat 15 minutes from the 10:30 run -
    inside a stale 30-minute lease, on a node whose id happened to match. Release removes
    the lie rather than relying on the gaps staying wide.

    THE SAFETY PROPERTY IS PRESERVED, not weakened. Release only ever happens when the
    lease still names THIS run (ownership-checked, exactly like release_run_lock), and it
    marks the lease expired rather than deleting it, so the record of who ran stays
    auditable. Releasing our own finished run's lease cannot admit two concurrent
    traders; it admits the NEXT one, which is the whole point.

    ONE DELIBERATE EXCEPTION: pending order intents. Orders are handed to an async
    executor (journal.log_intent -> scripts/execute_order_intents.py) and can still be
    working after this process exits, so while state/order_intents.json is non-empty the
    lease is LEFT ALONE and the TTL does the work. That is the one case where the process
    ending is not the same thing as the run being done.

    Fail-OPEN in every branch, per autonomy_config: "a lease must never brick the
    trader". A release that cannot commit or push is a lease that expires on TTL - the
    behaviour we had before - never an error the caller sees.
    """
    lease_path = ROOT / "state" / "RUN_LEASE.json"
    try:
        lease = json.loads(lease_path.read_text())
    except Exception:
        return False
    if lease.get("run_id") != run_id or lease.get("holder") != _node_id():
        return False  # not ours any more - leave it alone
    if lease.get("released_at"):
        return False  # already released (atexit can fire once per process, but be safe)
    # THE LOCAL FILE IS NOT AUTHORITATIVE, AND TRUSTING IT WOULD HAVE MADE THIS UNSAFE.
    # The local copy still says "ours" forever; the shared truth is origin/main. If
    # another node claimed the lease while this run was finishing, a release computed
    # from the local file would commit OUR record on top of THEIRS - the rebase below
    # replays it last - and wipe a live claim, admitting exactly the concurrent trader
    # the lease exists to stop. So the remote must agree that the lease is still ours.
    # A remote we cannot read is not agreement: we leave the lease alone and let the TTL
    # expire it, which is precisely the behaviour that existed before release did.
    remote = _read_remote_lease()
    if not remote or remote.get("run_id") != run_id \
            or remote.get("holder") != _node_id():
        return False
    try:
        intents = json.loads((ROOT / "state" / "order_intents.json").read_text())
        if intents.get("intents"):
            print("  (lease kept: order intents still pending for the async executor)")
            return False
    except Exception:
        pass  # no intents file / unreadable -> nothing pending to protect
    try:
        now = datetime.now(timezone.utc)
        lease["expires_at"] = now.isoformat()
        lease["released_at"] = now.isoformat()
        lease_path.write_text(json.dumps(lease, indent=2))
        subprocess.run(["git", "add", "state/RUN_LEASE.json"], cwd=ROOT,
                       capture_output=True, timeout=30)
        c = subprocess.run(["git", "commit", "-m", "Release run lease [vercel skip]"],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        if c.returncode != 0:
            return False
        p = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            rb = subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
            if rb.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], cwd=ROOT,
                               capture_output=True, timeout=30)
                return False
            subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                           capture_output=True, timeout=120)
        return True
    except Exception as e:
        print(f"  (cross-node lease release failed, TTL will expire it: {e})")
        return False


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
