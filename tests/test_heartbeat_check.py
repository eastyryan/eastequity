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


# --------------------------------------------------------------------------- #
# 2026-09-28 (issue #4): the heartbeat grades the schedule the box ACTUALLY runs.
# The 14:00 slot is paused and the 15:30 full pre-close run moved to 15:00, so
# every afternoon paged "14:00 missed" against a slot nobody fires.
# --------------------------------------------------------------------------- #
# 2026-10-01: 14:00 restored as a full buy-capable slot (user: trade more).
BOX_SLOTS = [6, 8.75, 10.5, 12, 14, 15, 17.5]   # 06:00 08:45 10:30 12:00 14:00 15:00 17:30 ET


def test_heartbeat_expected_slots_match_the_box_schedule():
    from runlib.analytics import expected_slots
    assert expected_slots(True) == BOX_SLOTS
    assert expected_slots(False) == [0, 23.98]


def test_config_and_depth_defaults_name_the_same_seven_slots():
    import json as _json
    from runlib.depths import DEFAULT_SLOT_DEPTHS, slot_depth_from_hhmm
    cfg = _json.loads((Path(__file__).resolve().parent.parent
                       / "autonomy_config.json").read_text())
    live = {k: v for k, v in cfg["schedule"]["slot_depths"].items()
            if len(k) == 4 and k.isdigit()}
    want = {"0600": "light", "0845": "holdings_watchlist", "1030": "full",
            "1200": "holdings_watchlist", "1400": "full", "1500": "full",
            "1730": "evening_review"}
    assert live == want == DEFAULT_SLOT_DEPTHS
    # the 3pm run resolves to a full scan whether launched on time or a bit late
    for hhmm in ("1458", "1500", "1512", "1530"):
        assert slot_depth_from_hhmm(hhmm, cfg) == "full", hhmm


def _box_day(monkeypatch, tmp_path, et_hours):
    import json as _json
    import runlib.analytics as A
    runs = tmp_path / "journal" / "runs"
    runs.mkdir(parents=True)
    lines = []
    for i, h in enumerate(et_hours):
        utc_h = h + 4
        lines.append(_json.dumps({
            "ts": f"2026-09-28T{int(utc_h):02d}:{int(round((utc_h % 1) * 60)):02d}"
                  f":00+00:00", "run_id": f"20260928-{i:04d}", "node": "box",
            "manual": False}))
    (runs / "2026-09-28.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: "2026-09-28")
    monkeypatch.setattr(A, "to_et_date", lambda ts: "2026-09-28")
    return A


def test_a_normal_box_day_grades_all_seven_slots(monkeypatch, tmp_path):
    """Runs complete ~10-25 min after each slot (a record is stamped when the
    run FINISHES). The restored 14:00 full run is graded and credited."""
    A = _box_day(monkeypatch, tmp_path, [6.1, 9.0, 10.9, 12.2, 14.3, 15.3, 17.6])
    r = A.slot_report(now_h=18.0, weekday=True)
    assert r["missed_slots"] == [], r
    assert "14:00" in [s["label"] for s in r["slots"]]


def test_a_missing_1400_run_pages(monkeypatch, tmp_path):
    A = _box_day(monkeypatch, tmp_path, [6.1, 9.0, 10.9, 12.2, 15.3, 17.6])
    r = A.slot_report(now_h=18.0, weekday=True)
    assert r["missed_slots"] == ["14:00"], r


def test_a_missing_1500_run_still_pages(monkeypatch, tmp_path):
    A = _box_day(monkeypatch, tmp_path, [6.1, 9.0, 10.9, 12.2, 14.3])
    r = A.slot_report(now_h=16.5, weekday=True)
    assert r["missed_slots"] == ["15:00"]
