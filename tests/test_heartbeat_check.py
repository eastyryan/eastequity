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


def test_one_missed_slot_is_tolerated(monkeypatch):
    _no_capability_noise(monkeypatch)
    _force_trading_day(monkeypatch)
    """GitHub coalesces cron under load (observed ~hourly on 2026-07-15). An alarm
    that fires on routine jitter gets muted, and a muted alarm is worse than none."""
    monkeypatch.setattr("runlib.analytics.build_health",
                        lambda: {"missed": 1, "expected_runs_so_far": 7,
                                 "completed_scheduled_runs": 6,
                                 "bundle_age_hours": 0.5})
    monkeypatch.setattr(hb, "ROOT", Path("/nonexistent"))
    assert hb.assess()["healthy"] is True


def test_a_stale_relay_bundle_trips_the_alarm(monkeypatch):
    _no_capability_noise(monkeypatch)
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
