"""The audit must be able to FAIL. A probe that cannot fail is decoration.

This module exists because five separate defects over three days shared one shape:
code present, unit tests green, production never invoking it or feeding it an empty
map. Unit tests cannot see that — they supply the inputs themselves. So these tests
assert the opposite property: that each probe genuinely detects its own breakage.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib import capabilities as C  # noqa: E402


def test_every_WIRING_capability_is_live_right_now():
    """The wiring probes must be green at all times.

    book_risk_bundle_blocks is deliberately EXCLUDED here: it inspects the newest
    published BUNDLE, so it legitimately reports dead whenever the freshest bundle
    was produced by a cloud run that predates a wiring change — as happened the
    moment these commits were pushed. That is the probe doing its job (it blocks
    BUYs until a fresh gather proves the inputs are present), but it is a statement
    about DATA RECENCY, not about the code, so asserting it in a unit test makes the
    suite depend on when the cloud last ran.
    """
    out = C.audit_capabilities()
    dead = [d for d in out["dead"] if d != "book_risk_bundle_blocks"]
    assert not dead, {k: out["results"][k] for k in dead}


def test_the_bundle_probe_is_still_registered():
    """Excluding it above must not become a way to quietly retire it."""
    assert "book_risk_bundle_blocks" in C.PROBES


def test_a_probe_that_raises_counts_as_dead():
    """An unverifiable guard and an absent one are the same to the position."""
    def boom():
        raise RuntimeError("probe exploded")
    saved = dict(C.PROBES)
    try:
        C.PROBES["explodes"] = boom
        out = C.audit_capabilities()
        assert out["ok"] is False
        assert "explodes" in out["dead"]
        assert "probe raised" in out["results"]["explodes"]["detail"]
    finally:
        C.PROBES.clear(); C.PROBES.update(saved)


def test_the_audit_blocks_buys_when_anything_is_dead():
    saved = dict(C.PROBES)
    try:
        C.PROBES["fake"] = lambda: (False, "deliberately dead")
        out = C.audit_capabilities()
        assert out["blocks_new_buys"] is True
    finally:
        C.PROBES.clear(); C.PROBES.update(saved)


def test_the_driver_probe_detects_a_broken_crosscheck(monkeypatch):
    """Simulate the exploit reopening: mismatch stops reporting."""
    import tools.demand_drivers as dd
    monkeypatch.setattr(dd, "driver_mismatch", lambda *a, **k: None)
    alive, detail = C._probe_driver_crosscheck()
    assert alive is False and "escape" in detail


def test_the_feed_probe_detects_the_predicate_going_dead(monkeypatch):
    """Simulate feed_is_alive reverting to the version that called everything alive."""
    import orchestrator
    monkeypatch.setattr(orchestrator, "feed_is_alive", lambda f: True)
    alive, detail = C._probe_feed_health_predicate()
    assert alive is False and "dead code again" in detail


def test_the_stop_probe_detects_an_unprotected_position(monkeypatch):
    from execution import simulated_broker
    monkeypatch.setattr(simulated_broker, "_load",
                        lambda: {"positions": [{"ticker": "DELL"}]})
    alive, detail = C._probe_protective_stops()
    assert alive is False and "UNPROTECTED" in detail


def test_the_stop_probe_accepts_a_protected_position(monkeypatch):
    """The mirror — without it, 'always dead' passes the test above."""
    from execution import simulated_broker
    monkeypatch.setattr(simulated_broker, "_load", lambda: {"positions": [
        {"ticker": "DELL", "protective_stop": {"status": "resting"}}]})
    alive, _ = C._probe_protective_stops()
    assert alive is True


def test_orchestrator_actually_runs_the_audit():
    """The wiring of the wiring-checker. Without this line in production, every
    probe above is itself decoration."""
    import inspect
    import orchestrator
    src = inspect.getsource(orchestrator.main)
    assert "audit_capabilities()" in src
    assert "capability_dead" in src
