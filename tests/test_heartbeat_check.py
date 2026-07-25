"""The alarm must actually fire. Key-name drift would make it silently never fire.

scripts/heartbeat_check.py reads runlib.analytics.build_health(). During development
this script was written against GUESSED key names (`missed_runs_today`) that
build_health does not emit, so the missed-run branch was dead on arrival — the exact
failure mode the script exists to end, reproduced inside the fix for it. These tests
pin the contract between the two modules in both directions.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import heartbeat_check as hb  # noqa: E402


def test_build_health_emits_the_keys_the_alarm_reads():
    """The contract. If build_health renames a key, this fails LOUDLY rather than
    the alarm quietly never firing again."""
    from runlib.analytics import build_health
    health = build_health()
    for key in ("missed", "expected_runs_so_far", "completed_scheduled_runs",
                "bundle_age_hours"):
        assert key in health, (key, sorted(health))


def _no_capability_noise(monkeypatch):
    """These tests assert the missed-run and bundle-age thresholds.

    assess() also runs the real capability audit, which reports on LIVE state — so
    without this, a bundle that predates a wiring change makes every threshold test
    fail for a reason that has nothing to do with what it is testing.
    """
    monkeypatch.setattr("runlib.capabilities.audit_capabilities",
                        lambda: {"ok": True, "dead": [], "results": {}})


def _force_trading_day(monkeypatch):
    """These tests are about the missed-run THRESHOLD, not the calendar.

    Without this they pass or fail depending on the day they are run: the alarm now
    zeroes missed runs on a non-session day, correctly, so on a weekend or holiday a
    mocked missed=5 can never trip. Pinning the calendar keeps the assertion about
    the thing it is actually testing.
    """
    monkeypatch.setattr("tools.market_calendar.session",
                        lambda *a, **k: {"is_trading_day": True, "source": "calendar",
                                         "open": None, "close": None,
                                         "is_half_day": False})


def test_missed_runs_trip_the_alarm(monkeypatch):
    _no_capability_noise(monkeypatch)
    _force_trading_day(monkeypatch)
    monkeypatch.setattr(hb, "build_health", lambda: {}, raising=False)
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 5, "expected_runs_so_far": 7,
                                 "completed_scheduled_runs": 2,
                                 "bundle_age_hours": 0.5})
    monkeypatch.setattr(hb, "ROOT", Path("/nonexistent"))  # no KILL_SWITCH
    out = hb.assess()
    assert out["healthy"] is False
    assert any("missed" in r for r in out["reasons"])


def test_a_holiday_is_not_a_missed_run(monkeypatch):
    """A holiday has no scheduled slots. Counting them as missed would page you every
    Thanksgiving and train you to ignore the alarm."""
    _no_capability_noise(monkeypatch)
    monkeypatch.setattr("tools.market_calendar.session",
                        lambda *a, **k: {"is_trading_day": False, "source": "calendar",
                                         "open": None, "close": None,
                                         "is_half_day": False})
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 7, "expected_runs_so_far": 7,
                                 "completed_scheduled_runs": 0,
                                 "bundle_age_hours": 0.5})
    monkeypatch.setattr(hb, "ROOT", Path("/nonexistent"))
    assert hb.assess()["healthy"] is True


def test_one_missed_slot_now_pages(monkeypatch):
    """POLICY REVERSED 2026-07-20. This test previously asserted that one missed slot
    was TOLERATED, on the reasoning that GitHub coalesces cron under load and an alarm
    firing on jitter gets muted. The reasoning was right and the remedy was in the wrong
    dimension: a count threshold cannot distinguish a slot that is 20 minutes late from
    one that never ran, so tolerating a count meant tolerating a death.

    Jitter absorption moved to where it belongs — a per-slot one-hour grace window in
    runlib.analytics.slot_report, pinned by tests/test_slot_report.py. A late slot now
    reads 'pending' and never reaches this check at all. What arrives here as `missed`
    has already had a full hour to land, and per user policy every missed slot is a lost
    chance to enter, exit or learn.
    """
    _no_capability_noise(monkeypatch)
    _force_trading_day(monkeypatch)
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 1, "expected_runs_so_far": 7,
                                 "completed_scheduled_runs": 6,
                                 "missed_slots": ["14:00"],
                                 "bundle_age_hours": 0.5})
    monkeypatch.setattr(hb, "ROOT", Path("/nonexistent"))
    out = hb.assess()
    assert out["healthy"] is False
    assert any("14:00" in r for r in out["reasons"]), \
        "the alert must name WHICH slot died, not just how many"


def test_a_stale_relay_bundle_trips_the_alarm(monkeypatch):
    # Pin the calendar for the same reason _force_trading_day exists: the stale
    # threshold is now a BAND (3h in market hours / 6h weekday off-hours / 26h on
    # a non-trading day, added 2026-07-25 because a flat 6h paged every weekend
    # for a pipeline with nothing to do). Without pinning, a 12h bundle is
    # correctly healthy on a Saturday and this assertion flips with the calendar.
    _no_capability_noise(monkeypatch)
    _force_trading_day(monkeypatch)
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 0, "expected_runs_so_far": 7,
                                 "completed_scheduled_runs": 7,
                                 "bundle_age_hours": 12.0})
    monkeypatch.setattr(hb, "ROOT", Path("/nonexistent"))
    out = hb.assess()
    assert out["healthy"] is False
    assert any("bundle" in r for r in out["reasons"])


def test_engaged_kill_switch_is_surfaced(monkeypatch, tmp_path):
    """A halted system looks identical to a healthy idle one from outside — which is
    how a halt outlives the reason it was engaged for."""
    _no_capability_noise(monkeypatch)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "KILL_SWITCH").write_text("halted")
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 0, "expected_runs_so_far": 7,
                                 "completed_scheduled_runs": 7,
                                 "bundle_age_hours": 0.5})
    monkeypatch.setattr(hb, "ROOT", tmp_path)
    out = hb.assess()
    assert out["healthy"] is False
    assert any("KILL_SWITCH" in r for r in out["reasons"])


def test_a_healthy_pipeline_is_silent(monkeypatch):
    """The mirror — without it, 'always unhealthy' passes every test above."""
    _no_capability_noise(monkeypatch)
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 0, "expected_runs_so_far": 7,
                                 "completed_scheduled_runs": 7,
                                 "bundle_age_hours": 0.5})
    monkeypatch.setattr(hb, "ROOT", Path("/nonexistent"))
    out = hb.assess()
    assert out["healthy"] is True and out["reasons"] == []


def test_webhook_failure_never_raises():
    """An alarm that crashes on a bad webhook URL reports nothing at all."""
    assert hb.post_webhook("http://127.0.0.1:9/nope", {"text": "x"}) is False


def test_the_alarm_reports_slots_against_slots(monkeypatch):
    """THE REGRESSION (2026-07-25). The alarm paged with arithmetic that cannot
    be true, because `expected`/`missed` counted SLOTS while `completed` counted
    RUNS:

        "1 scheduled run(s) missed today [10:00 ET] (expected 3, completed 3)"
        "1 scheduled run(s) missed today [16:00 ET] (expected 7, completed 9)"

    Both were right underneath and both read as broken, which is how a real
    signal (a specific slot never ran) trains an operator to ignore the alarm.
    The message must now compare like with like and keep the raw run count as a
    separate, labelled number.
    """
    _no_capability_noise(monkeypatch)
    _force_trading_day(monkeypatch)
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 1, "expected_runs_so_far": 7,
                                 "slots_covered": 6, "runs_journaled": 9,
                                 "completed_scheduled_runs": 9,
                                 "max_drift_min": 29,
                                 "missed_slots": ["16:00"],
                                 "bundle_age_hours": 0.5})
    monkeypatch.setattr(hb, "ROOT", Path("/nonexistent"))
    out = hb.assess()
    assert out["healthy"] is False
    reason = next(r for r in out["reasons"] if "16:00" in r)
    assert "6/7" in reason, "slots covered must be reported against elapsed SLOTS"
    assert "9 journaled run(s)" in reason, "the raw run count must stay, but labelled"
    assert "expected 7, completed 9" not in reason, "the contradictory pairing is back"
    assert "+29min" in reason, "drift is the diagnosis for a slot eaten by its neighbour"
