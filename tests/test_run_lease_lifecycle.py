"""The cross-node lease must expire when the run ends, and must never brick the trader.

autonomy_config states the contract in one line: "Fail-open on any git error - a lease
must never brick the trader." Two holes in that, both found 2026-08-03:

  * NOTHING EVER RELEASED THE LEASE. acquire wrote a 30-minute claim and pushed it; the
    only thing that ever cleared it was the TTL. Measured run length over 52
    breadcrumb->summary pairs (2026-07-21..08-03) is 12.5 min median / 23.4 p90, so every
    run left the shared ledger marked busy for roughly twice as long as it was, and a run
    that died at minute one lied for the full half hour. 94 lease commits are in git
    history and not one of them is a release.

  * THE CONFLICT TEST COULD RAISE. Its try/except covered only the fromisoformat call,
    so the very next line (`exp > now`) threw TypeError on any lease whose expires_at
    parsed NAIVE. preflight() runs before the lock, before the safety layer and before
    stop enforcement, so that exception takes the whole system dark on an open book -
    the exact outcome the config forbids.

The safety property is NOT relaxed here: release is ownership-checked (it only ever
expires a lease still naming this run on this node), it is skipped while order intents
are pending for the async executor, and a lease held by another node is untouched.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlib.preflight as pf  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


# --- fail-open conflict evaluation ------------------------------------------------

def test_a_naive_expiry_does_not_take_the_system_dark():
    """THE REGRESSION. `exp > now` with a naive datetime raised TypeError out of
    preflight, killing the run before stops were enforced."""
    naive = (_now() + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    out = pf._lease_conflict({"expires_at": naive, "run_id": "other",
                              "holder": "some-other-node"}, "mine", _now())
    assert out and "some-other-node" in out


def test_an_unparseable_lease_fails_open():
    assert pf._lease_conflict({"expires_at": "not-a-date", "run_id": "o",
                               "holder": "elsewhere"}, "mine", _now()) is None
    assert pf._lease_conflict({}, "mine", _now()) is None
    assert pf._lease_conflict(None, "mine", _now()) is None


def test_an_expired_lease_is_not_a_conflict():
    old = (_now() - timedelta(minutes=1)).isoformat()
    assert pf._lease_conflict({"expires_at": old, "run_id": "o",
                               "holder": "elsewhere"}, "mine", _now()) is None


def test_our_own_live_lease_is_not_a_conflict_with_ourselves():
    soon = (_now() + timedelta(minutes=20)).isoformat()
    assert pf._lease_conflict({"expires_at": soon, "run_id": "mine",
                               "holder": "elsewhere"}, "mine", _now()) is None


# --- release ----------------------------------------------------------------------

class _Git:
    """Record git invocations instead of running them."""

    def __init__(self, fail_push=False):
        self.calls, self.fail_push = [], fail_push

    def __call__(self, cmd, **kw):
        self.calls.append(tuple(cmd))
        class R:
            returncode = 1 if (self.fail_push and cmd[:2] == ["git", "push"]) else 0
            stdout = stderr = ""
        return R()


def _tree(tmp_path, lease: dict, intents=None, monkeypatch=None, remote="same"):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "RUN_LEASE.json").write_text(json.dumps(lease))
    (tmp_path / "state" / "order_intents.json").write_text(
        json.dumps({"intents": intents or []}))
    if monkeypatch is not None:
        # origin/main is the shared truth; the local file is only ever a hint.
        value = lease if remote == "same" else remote
        monkeypatch.setattr(pf, "_read_remote_lease", lambda: value)


def test_release_expires_our_own_lease(monkeypatch, tmp_path):
    _tree(tmp_path, {"holder": "vm", "run_id": "r1",
                     "acquired_at": _now().isoformat(),
                     "expires_at": (_now() + timedelta(minutes=30)).isoformat()},
          monkeypatch=monkeypatch)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    git = _Git()
    monkeypatch.setattr(pf.subprocess, "run", git)

    assert pf.release_cross_node_lease("r1") is True
    lease = json.loads((tmp_path / "state" / "RUN_LEASE.json").read_text())
    assert lease["released_at"]
    # The released lease must no longer conflict with anybody.
    assert pf._lease_conflict(lease, "someone-else", _now()) is None
    assert ("git", "push", "origin", "main") in git.calls


def test_release_never_touches_another_nodes_lease(monkeypatch, tmp_path):
    """The safety property. A lease we do not hold is not ours to expire."""
    other = {"holder": "MacBook-Pro-2.local", "run_id": "r9",
             "expires_at": (_now() + timedelta(minutes=30)).isoformat()}
    _tree(tmp_path, other, monkeypatch=monkeypatch)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    git = _Git()
    monkeypatch.setattr(pf.subprocess, "run", git)

    assert pf.release_cross_node_lease("r1") is False
    assert json.loads((tmp_path / "state" / "RUN_LEASE.json").read_text()) == other
    assert git.calls == []


def test_release_is_skipped_while_order_intents_are_pending(monkeypatch, tmp_path):
    """Orders are handed to an async executor and can still be working after this
    process exits, so "the process ended" is not "the run is done"."""
    _tree(tmp_path, {"holder": "vm", "run_id": "r1",
                     "expires_at": (_now() + timedelta(minutes=30)).isoformat()},
          intents=[{"ticker": "BKR", "side": "buy"}], monkeypatch=monkeypatch)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    git = _Git()
    monkeypatch.setattr(pf.subprocess, "run", git)

    assert pf.release_cross_node_lease("r1") is False
    assert "released_at" not in json.loads(
        (tmp_path / "state" / "RUN_LEASE.json").read_text())
    assert git.calls == []


def test_release_will_not_clobber_a_claim_that_moved_on(monkeypatch, tmp_path):
    """THE HAZARD RELEASE ITSELF INTRODUCES, and the reason it re-reads origin/main.

    The local RUN_LEASE.json says "ours" forever. If another node claims the lease while
    this run is finishing, a release computed from the local copy commits our record on
    top of theirs — the rebase replays ours last — and wipes a LIVE claim, admitting
    exactly the concurrent trader the lease exists to stop. The shared truth is
    origin/main, and it has to agree."""
    mine = {"holder": "vm", "run_id": "r1",
            "expires_at": (_now() + timedelta(minutes=30)).isoformat()}
    theirs = {"holder": "MacBook-Pro-2.local", "run_id": "r2",
              "expires_at": (_now() + timedelta(minutes=30)).isoformat()}
    _tree(tmp_path, mine, monkeypatch=monkeypatch, remote=theirs)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    git = _Git()
    monkeypatch.setattr(pf.subprocess, "run", git)

    assert pf.release_cross_node_lease("r1") is False
    assert git.calls == []


def test_an_unreadable_remote_leaves_the_lease_to_its_ttl(monkeypatch, tmp_path):
    """Fail-CLOSED on the release side specifically: a remote we cannot read is not
    agreement that the lease is ours. Doing nothing reverts to the pre-2026-08-03
    behaviour (TTL expiry), which was safe if slow — the opposite direction would guess."""
    _tree(tmp_path, {"holder": "vm", "run_id": "r1",
                     "expires_at": (_now() + timedelta(minutes=30)).isoformat()},
          monkeypatch=monkeypatch, remote=None)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    git = _Git()
    monkeypatch.setattr(pf.subprocess, "run", git)

    assert pf.release_cross_node_lease("r1") is False
    assert git.calls == []


def test_release_is_idempotent(monkeypatch, tmp_path):
    _tree(tmp_path, {"holder": "vm", "run_id": "r1",
                     "expires_at": _now().isoformat(),
                     "released_at": _now().isoformat()}, monkeypatch=monkeypatch)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    monkeypatch.setattr(pf.subprocess, "run", _Git())
    assert pf.release_cross_node_lease("r1") is False


def test_release_failing_is_never_an_error_the_caller_sees(monkeypatch, tmp_path):
    """Fail-open: a release that cannot push leaves a lease that expires on TTL —
    the behaviour we had before — never an exception out of an atexit handler."""
    _tree(tmp_path, {"holder": "vm", "run_id": "r1",
                     "expires_at": (_now() + timedelta(minutes=30)).isoformat()},
          monkeypatch=monkeypatch)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")

    def boom(*a, **k):
        raise OSError("git is gone")
    monkeypatch.setattr(pf.subprocess, "run", boom)
    assert pf.release_cross_node_lease("r1") is False

    monkeypatch.setattr(pf, "ROOT", tmp_path / "nowhere")
    assert pf.release_cross_node_lease("r1") is False


def test_a_manual_run_never_registers_a_release(monkeypatch, tmp_path):
    """Manual runs yield but never claim (2026-07-20 policy), so they must never be in
    a position to hand back the scheduled trader's lease either."""
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    monkeypatch.setattr(pf, "_read_remote_lease", lambda: None)
    registered = []
    monkeypatch.setattr("atexit.register", lambda fn, *a: registered.append((fn, a)))

    assert pf.acquire_cross_node_lease("r1", {"risk_controls": {}}, manual=True) is None
    assert registered == []


def test_a_scheduled_run_registers_its_own_release(monkeypatch, tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pf, "ROOT", tmp_path)
    monkeypatch.setattr(pf, "_node_id", lambda: "vm")
    monkeypatch.setattr(pf, "_read_remote_lease", lambda: None)
    monkeypatch.setattr(pf.subprocess, "run", _Git())
    registered = []
    monkeypatch.setattr("atexit.register", lambda fn, *a: registered.append((fn, a)))

    assert pf.acquire_cross_node_lease("r1", {"risk_controls": {}}) is None
    assert registered == [(pf.release_cross_node_lease, ("r1",))]
