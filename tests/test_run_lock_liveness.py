"""A lock naming a dead process must not block the next run.

REPRODUCED LIVE 2026-07-19. A weekly-market run was killed mid-flight. Its atexit
handler — whose comment claimed it released "on every exit path, crashes included" —
did not fire, because atexit does not run on SIGKILL. state/RUN_LOCK was left naming
a dead PID, and the ONLY recovery was a 45-minute staleness timer.

On a seven-slot trading day that is two lost slots, and stop enforcement is offline
for the same window, since the 5-minute stop watcher takes the same lock. Had it
happened at the 09:00 slot there would have been no trading and no stop checks until
roughly 09:45.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlib.preflight as PF  # noqa: E402


def _use_tmp_lock(monkeypatch, tmp_path):
    lock = tmp_path / "RUN_LOCK"
    monkeypatch.setattr(PF, "LOCK_FILE", lock)
    return lock


def test_a_lock_held_by_a_dead_process_is_reclaimed(monkeypatch, tmp_path):
    """THE regression. A dead holder must not cost 45 minutes of trading."""
    lock = _use_tmp_lock(monkeypatch, tmp_path)
    dead_pid = 999_999            # not a live pid on any sane system
    lock.write_text(f"20260719-dead {dead_pid}")

    assert PF.acquire_run_lock("20260719-new") is True
    assert lock.read_text().split()[0] == "20260719-new"


def test_a_lock_held_by_a_LIVE_process_is_respected(monkeypatch, tmp_path):
    """The mirror. Without it, 'always steal' passes the test above and two cycles
    trade on the same stale portfolio snapshot."""
    lock = _use_tmp_lock(monkeypatch, tmp_path)
    lock.write_text(f"20260719-live {os.getpid()}")   # our own pid: definitely alive

    assert PF.acquire_run_lock("20260719-new") is False
    assert lock.read_text().split()[0] == "20260719-live"


def test_an_old_format_lock_is_respected_until_it_ages_out(monkeypatch, tmp_path):
    """A lock with no PID is of unknown provenance. Unknown must mean ALIVE, so the
    age backstop governs — stealing on ignorance is the dangerous direction."""
    lock = _use_tmp_lock(monkeypatch, tmp_path)
    lock.write_text("20260719-oldformat")             # no pid recorded

    assert PF.acquire_run_lock("20260719-new") is False


def test_release_only_removes_our_own_lock(monkeypatch, tmp_path):
    """If B reclaimed the lock while A was still alive, A's atexit used to delete
    B's lock and admit a third run."""
    lock = _use_tmp_lock(monkeypatch, tmp_path)
    lock.write_text(f"20260719-B {os.getpid()}")

    PF.release_run_lock("20260719-A")                 # A tries to clean up
    assert lock.exists(), "A deleted B's lock"

    PF.release_run_lock("20260719-B")                 # B's own release
    assert not lock.exists()


def test_the_lock_records_a_pid_so_liveness_is_knowable(monkeypatch, tmp_path):
    lock = _use_tmp_lock(monkeypatch, tmp_path)
    assert PF.acquire_run_lock("20260719-x") is True
    run_id, pid = PF._lock_holder()
    assert run_id == "20260719-x"
    assert pid == os.getpid()
