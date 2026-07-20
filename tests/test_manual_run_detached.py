"""A hand-fired run must never suppress the scheduled cloud trader.

THE BUG (observed 2026-07-20). `manual` only skipped the stand-down branch, and then
EVERY run — manual included — fell through to write, commit and push a 30-minute
cross-node lease. So a local run typed by the operator took the ledger out from under
the cloud routine, which is the only scheduled trader. The suppression was invisible:
the cloud run simply stood down and journaled a halt, exactly like a quiet day.

The policy is asymmetric on purpose: SCHEDULED WINS, MANUAL YIELDS.
  * manual + conflict  -> abort (do not trade alongside the cloud)
  * manual + no conflict -> run, but claim NOTHING
  * scheduled           -> unchanged: stand down on conflict, else claim and push

The last case is load-bearing. If scheduled runs stopped claiming, the manual
stand-down above would have nothing to detect and the whole guard would be decorative.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib import preflight  # noqa: E402

CFG = {"risk_controls": {"cross_node_lease_enabled": True,
                         "cross_node_lease_ttl_minutes": 30}}


def _lease(holder="cloud-runner", minutes=15):
    """A lease held by ANOTHER node, expiring `minutes` from now."""
    now = datetime.now(timezone.utc)
    return {"holder": holder, "run_id": "20260720-cloud1",
            "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=minutes)).isoformat()}


def _spy_git(monkeypatch):
    """Record every subprocess call so we can assert nothing was committed/pushed."""
    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _R()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    return calls


def test_manual_run_aborts_when_the_cloud_holds_the_lease(monkeypatch):
    """Was: printed a warning and traded anyway, concurrently with the cloud."""
    monkeypatch.setattr(preflight, "_read_remote_lease", lambda: _lease())
    calls = _spy_git(monkeypatch)

    halt = preflight.acquire_cross_node_lease("20260720-manual", CFG, manual=True)

    assert halt is not None, "manual run proceeded while the cloud held the lease"
    assert "scheduled trader holds the ledger" in halt
    assert not any("commit" in c or "push" in c for c in calls)


def test_manual_run_never_claims_the_lease(monkeypatch, tmp_path):
    """THE CORE FIX. No conflict, so the run proceeds — but it must claim NOTHING.

    If it wrote a lease here, the next scheduled cloud run would stand down.
    """
    monkeypatch.setattr(preflight, "_read_remote_lease", lambda: None)
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    calls = _spy_git(monkeypatch)

    halt = preflight.acquire_cross_node_lease("20260720-manual", CFG, manual=True)

    assert halt is None, "manual run was blocked with no lease outstanding"
    assert not (tmp_path / "state" / "RUN_LEASE.json").exists(), \
        "manual run wrote a lease file"
    assert calls == [], f"manual run touched git: {calls}"


def test_scheduled_run_still_claims_and_pushes(monkeypatch, tmp_path):
    """The mirror. Without this, deleting the claim entirely would pass every test
    above while quietly removing the double-trade guard the manual path relies on."""
    monkeypatch.setattr(preflight, "_read_remote_lease", lambda: None)
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    calls = _spy_git(monkeypatch)

    halt = preflight.acquire_cross_node_lease("20260720-cloud", CFG, manual=False)

    assert halt is None
    written = tmp_path / "state" / "RUN_LEASE.json"
    assert written.exists(), "scheduled run did not write the lease"
    assert json.loads(written.read_text())["run_id"] == "20260720-cloud"
    assert any("commit" in c for c in calls), "scheduled run did not commit the lease"
    assert any("push" in c for c in calls), "scheduled run did not push the lease"


def test_scheduled_run_stands_down_on_conflict(monkeypatch):
    """Unchanged behaviour, pinned so the refactor above cannot have moved it."""
    monkeypatch.setattr(preflight, "_read_remote_lease", lambda: _lease())
    _spy_git(monkeypatch)

    halt = preflight.acquire_cross_node_lease("20260720-other", CFG, manual=False)

    assert halt is not None and "double-trading" in halt


def test_an_expired_lease_blocks_nobody(monkeypatch, tmp_path):
    """A dead node must not wedge the system forever — the TTL is the release."""
    monkeypatch.setattr(preflight, "_read_remote_lease",
                        lambda: _lease(minutes=-5))  # already expired
    monkeypatch.setattr(preflight, "ROOT", tmp_path)
    _spy_git(monkeypatch)

    assert preflight.acquire_cross_node_lease("m", CFG, manual=True) is None
    assert preflight.acquire_cross_node_lease("s", CFG, manual=False) is None
