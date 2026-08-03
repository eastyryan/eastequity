"""Per-slot run accounting — the fix for the 2026-07-20 silent outage.

WHAT WENT WRONG. The runs heartbeat computed `missed = expected - completed`, bare
subtraction over the day's journal, and paged only when `missed > 2`. On 2026-07-20 the
local trader was completely dead for eight days. At the 10:30 ET check three slots had
elapsed (6am, 9am, 10am), the cloud had covered one, `missed` came to 2, and `2 > 2` is
False. The alarm reported HEALTHY through a total node outage.

Three separate defects, each pinned below:
  * subtraction cannot name WHICH slot died, so the alert was unactionable
  * subtraction miscounts when a slot fires twice (2 runs at 9am + nothing at 10am
    netted to "0 missed")
  * a slot counted as expected the instant the clock passed it, so a slot still inside
    its own grace period was already being counted against the threshold
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlib.analytics as A  # noqa: E402

SLOTS = [6, 8.75, 10.5, 12, 14, 15.5, 17.5]  # 09:00/10:00 -> 08:45/10:30 (07-25);
                                             # 16:00 -> 15:30 (08-03): a full run
                                             # takes 18-20 min, so a 16:00 slot
                                             # COMPLETED after the closing bell.


def _journal(tmp_path, hours, node="vm"):
    """Write a run journal with completed runs at the given ET hours."""
    runs = tmp_path / "journal" / "runs"
    runs.mkdir(parents=True)
    # ET is UTC-4 in July; the loader converts, so write UTC and let it do the work.
    # An ET hour >= 20 pushes UTC past midnight, so roll the date rather than emit an
    # invalid "24:00" (which parses to None and silently drops the run) — recovery of the
    # last 17:30 slot lands there.
    lines = []
    for i, h in enumerate(hours):
        utc_h = h + 4
        day = 20
        if utc_h >= 24:
            utc_h -= 24
            day = 21
        lines.append(json.dumps({
            "ts": f"2026-07-{day:02d}T{int(utc_h):02d}:{int(round((utc_h % 1) * 60)):02d}:00+00:00",
            "run_id": f"20260720-{i:04d}", "node": node, "manual": False}))
    (runs / "2026-07-20.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path


def _starts(tmp_path, hours, node="vm"):
    """Write run_started breadcrumbs at the given ET hours, each tagged with its slot."""
    d = tmp_path / "journal" / "run_starts"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, h in enumerate(hours):
        utc_h = h + 4
        slot = f"{int(h):02d}:{int(round((h % 1) * 60)):02d}"
        lines.append(json.dumps({
            "ts": f"2026-07-20T{int(utc_h):02d}:{int(round((utc_h % 1) * 60)):02d}:00+00:00",
            "run_id": f"mk-{i}", "node": node, "slot": slot, "stage": "start"}))
    (d / "2026-07-20.jsonl").write_text("\n".join(lines) + "\n")


def _report(monkeypatch, tmp_path, hours, now_h, node="vm", start_hours=None):
    _journal(tmp_path, hours, node)
    if start_hours:
        _starts(tmp_path, start_hours, node)
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: "2026-07-20")
    monkeypatch.setattr(A, "to_et_date", lambda ts: "2026-07-20")
    return A.slot_report(now_h=now_h, weekday=True)


def test_the_2026_07_20_outage_is_now_caught(monkeypatch, tmp_path):
    """THE REGRESSION. One run at 9:22, checked at 10:30 — the exact shape of the
    morning that reported healthy. It must now name 06:00 as missed."""
    r = _report(monkeypatch, tmp_path, [9.37], now_h=10.5)
    assert r["missed_slots"] == ["06:00"]
    assert r["missed_count"] > 0, "the outage that started all this is still silent"


def test_a_missed_slot_is_named_not_just_counted(monkeypatch, tmp_path):
    """'missed 1' sends you digging; 'missed 14:00' tells you what to do.

    now_h is 17:06, not 16:30: at 16:30 the 16:00 slot is still inside its own grace
    window and correctly reads pending, which is the behaviour the grace test pins.
    """
    r = _report(monkeypatch, tmp_path, [6.0, 8.75, 10.5, 12.0], now_h=17.1)
    assert r["missed_slots"] == ["14:00", "15:30"]


def test_two_runs_in_one_slot_do_not_cover_a_different_slot(monkeypatch, tmp_path):
    """Subtraction's blind spot: 5 runs against 5 elapsed slots netted to zero missed
    even when two of them landed in the same slot and a real one was empty."""
    r = _report(monkeypatch, tmp_path, [8.75, 9.2, 12.0, 14.0, 6.0], now_h=16.5)
    assert "10:30" in r["missed_slots"], "a doubled-up slot masked an empty one"


def test_a_slot_inside_its_grace_window_is_pending_not_missed(monkeypatch, tmp_path):
    """Cron jitter absorption. At 14:30 the 14:00 slot is 30 min late, not dead —
    GitHub coalesces crons under load and paging on that trains you to ignore it."""
    r = _report(monkeypatch, tmp_path, [6.0, 8.75, 10.5, 12.0], now_h=14.5)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["14:00"] == "pending"
    assert r["missed_count"] == 0


def test_grace_expires_and_the_slot_flips_to_missed(monkeypatch, tmp_path):
    """The mirror of the test above — without it, 'always pending' would pass."""
    r = _report(monkeypatch, tmp_path, [6.0, 8.75, 10.5, 12.0], now_h=15.1)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["14:00"] == "missed"


def test_grace_never_bleeds_into_the_next_slot(monkeypatch, tmp_path):
    """One run must never satisfy two slots. A single run at 09:40 is the 08:45
    slot landing +55min — it must credit 08:45 and leave 10:30 honestly empty."""
    r = _report(monkeypatch, tmp_path, [9.67], now_h=12.5)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["08:45"] == "hit"
    assert statuses["10:30"] == "missed"


def test_no_two_slots_can_share_a_window(monkeypatch, tmp_path):
    """THE POINT OF THE 2026-07-25 RESCHEDULE, as an invariant rather than a
    story. 09:00/10:00 sat 60min apart against a 60min grace window, so their
    windows abutted exactly and the clamp to the next slot was load-bearing.
    Every gap in the current schedule is >= 90min, so no window can reach its
    neighbour even at full grace. If someone tightens the slot map again, this
    fails before a run can be double-counted in production."""
    from runlib.analytics import SLOT_EARLY_TOLERANCE_H, SLOT_GRACE_H
    gaps = [b - a for a, b in zip(SLOTS, SLOTS[1:])]
    assert min(gaps) >= SLOT_GRACE_H + SLOT_EARLY_TOLERANCE_H, (
        f"slots {SLOTS} have a {min(gaps)}h gap against a "
        f"{SLOT_GRACE_H}h grace + {SLOT_EARLY_TOLERANCE_H}h early tolerance — "
        f"two windows now overlap and one run can be credited to either")


def test_a_slightly_early_run_still_counts_for_its_slot(monkeypatch, tmp_path):
    """A run at 8:37 is the 08:45 slot firing early, not a missed 08:45."""
    r = _report(monkeypatch, tmp_path, [8.62], now_h=12.5)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["08:45"] == "hit"


def test_manual_runs_cannot_fill_a_missed_slot(monkeypatch, tmp_path):
    """An alarm a hand-run can silence is not an alarm. See scripts/manual_run.sh."""
    runs = tmp_path / "journal" / "runs"
    runs.mkdir(parents=True)
    (runs / "2026-07-20.jsonl").write_text(json.dumps({
        "ts": "2026-07-20T18:05:00+00:00", "run_id": "manual-1",
        "node": "MacBook-Pro-2.local", "manual": True}) + "\n")
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: "2026-07-20")
    monkeypatch.setattr(A, "to_et_date", lambda ts: "2026-07-20")
    r = A.slot_report(now_h=15.5, weekday=True)
    assert "14:00" in r["missed_slots"], "a manual run filled in a missed slot"


def test_halted_runs_do_not_count_as_hits(monkeypatch, tmp_path):
    """A halted run did not trade. Four halted attempts preceded the 2026-07-20
    incident and counting them would have hidden it further."""
    runs = tmp_path / "journal" / "runs"
    runs.mkdir(parents=True)
    (runs / "2026-07-20.jsonl").write_text(json.dumps({
        "ts": "2026-07-20T18:05:00+00:00", "run_id": "halted-1",
        "node": "vm", "manual": False, "halted": "KILL_SWITCH"}) + "\n")
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: "2026-07-20")
    monkeypatch.setattr(A, "to_et_date", lambda ts: "2026-07-20")
    assert "14:00" in A.slot_report(now_h=15.5, weekday=True)["missed_slots"]


def test_future_dated_runs_cannot_satisfy_a_slot(monkeypatch, tmp_path):
    """Clock skew must not let a run from later in the day retire an earlier slot."""
    r = _report(monkeypatch, tmp_path, [16.0], now_h=10.5)
    assert r["hit"] == 0


def test_node_identity_is_reported(monkeypatch, tmp_path):
    """The deepest defect: run records carried no node, so a live cloud trader masked
    a dead local one and the aggregate merely looked low."""
    r = _report(monkeypatch, tmp_path, [9.0], now_h=10.5, node="vm")
    assert r["nodes_seen"] == ["vm"]
    assert next(s for s in r["slots"] if s["label"] == "08:45")["node"] == "vm"


def test_a_fully_healthy_day_is_silent(monkeypatch, tmp_path):
    """Without this, 'always missed' passes every test above."""
    r = _report(monkeypatch, tmp_path, SLOTS, now_h=19.0)
    assert r["missed_slots"] == [] and r["missed_count"] == 0
    assert r["hit"] == len(SLOTS)


# --- run_started breadcrumbs: fired-and-died vs never-fired -------------------
# The 2026-07-20 root cause: a run recorded nothing until it succeeded, so a session
# that died mid-run was indistinguishable from a fire that never happened. A start
# breadcrumb makes the difference visible.

def test_a_slot_that_fired_and_died_is_labelled_died_not_missed(monkeypatch, tmp_path):
    """A start breadcrumb landed for the 14:00 slot but no summary followed. That is a
    run that fired and DIED — there is a session to read — not a fire that never was."""
    r = _report(monkeypatch, tmp_path, [9.0, 10.0, 12.0], now_h=16.5,
                start_hours=[14.0])
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["14:00"] == "died"
    assert r["died_slots"] == ["14:00"]


def test_a_died_slot_still_counts_as_missed_for_paging(monkeypatch, tmp_path):
    """Fired-and-died is a lost slot too — it must still trip the miss count, just with
    a more precise label. A death that did not page would be the original bug again."""
    r = _report(monkeypatch, tmp_path, [9.0, 10.0, 12.0], now_h=16.5,
                start_hours=[14.0])
    assert "14:00" in r["missed_slots"]
    assert r["missed_count"] >= 1


def test_never_fired_slot_has_no_breadcrumb_and_reads_missed(monkeypatch, tmp_path):
    """The mirror: no start breadcrumb → the fire never happened → plain 'missed',
    which tells the operator to look at the routine/platform, not a dead session."""
    r = _report(monkeypatch, tmp_path, [9.0, 10.0, 12.0], now_h=16.5, start_hours=[])
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["14:00"] == "missed"
    assert r["died_slots"] == []


def test_a_breadcrumb_followed_by_a_summary_is_just_a_hit(monkeypatch, tmp_path):
    """A healthy run marks its start AND journals its summary. The start must not turn a
    successful slot into a phantom death."""
    r = _report(monkeypatch, tmp_path, [9.0, 10.0, 12.0, 14.0], now_h=16.5,
                start_hours=[14.0])
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["14:00"] == "hit"
    assert r["died_slots"] == []


# --- self-heal recovery: a late recovery run credits the slot it recovered -----
# 2026-07-23 was the first real activation of the watchdog (scripts/find_missed_slot.py):
# the 06:00 slot never fired, and the watchdog re-ran it at 07:19 ET. But that run landed
# past the 06:00 grace window, so slot_report left 06:00 "missed" and the heartbeat paged
# all day — "1 missed [06:00] (expected 1, completed 2)". A recovered slot is not a missed
# slot.

def test_a_recovery_run_credits_the_slot_it_recovered(monkeypatch, tmp_path):
    """THE REGRESSION. 06:00 never fired; the watchdog re-ran it at 07:19 ET — past the
    grace window but well inside the recovery window. That slot is covered, not missed."""
    r = _report(monkeypatch, tmp_path, [7.32], now_h=7.75)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["06:00"] == "hit"
    assert r["missed_count"] == 0
    assert "06:00" not in r["missed_slots"]


def test_a_recovered_slot_is_flagged_recovered(monkeypatch, tmp_path):
    """The lateness must stay visible — a recovery is a hit, but a hit that had to be
    rescued, so the dashboard and journal can say so rather than hiding the gap."""
    r = _report(monkeypatch, tmp_path, [7.32], now_h=7.75)
    six = next(s for s in r["slots"] if s["label"] == "06:00")
    assert six.get("recovered") is True
    assert six["run_id"] == "20260720-0000"  # the recovery run, credited to the slot


def test_a_run_too_late_does_not_recover_the_slot(monkeypatch, tmp_path):
    """The recovery window is bounded by RECOVERY_MAX_AGE_H — the same age the watchdog
    itself will not resurrect past. A run 3.5h after the last slot is not its recovery."""
    r = _report(monkeypatch, tmp_path, [21.0], now_h=21.5)  # 17:30 slot + 3.5h
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["17:30"] == "missed"


def test_a_run_within_the_recovery_age_recovers_the_last_slot(monkeypatch, tmp_path):
    """The mirror of the age-cap test — a run 2.5h after the last slot IS its recovery,
    so 'always missed when late' cannot pass."""
    r = _report(monkeypatch, tmp_path, [20.0], now_h=20.5)  # 17:30 slot + 2.5h
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["17:30"] == "hit"
    assert "17:30" not in r["missed_slots"]


def test_recovery_never_steals_the_next_slots_own_run(monkeypatch, tmp_path):
    """A run at 8:39 is the 08:45 slot firing early — it must credit 08:45, never reach
    back to 'recover' 06:00. The recovery window stops at the next slot's early edge."""
    r = _report(monkeypatch, tmp_path, [8.65], now_h=9.5)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["08:45"] == "hit"
    assert statuses["06:00"] == "missed"


# --------------------------------------------------------------------------- #
# Drift + one-basis reporting (2026-07-25)
#
# The alarm was paging with arithmetic that cannot be true:
#   "1 scheduled run(s) missed today [10:00 ET] (expected 3, completed 3)"
#   "1 scheduled run(s) missed today [16:00 ET] (expected 7, completed 9)"
# Both were CORRECT underneath — `expected`/`missed` counted SLOTS while
# `completed` counted RUNS — but an operator cannot parse that, and an alarm
# nobody can parse is an alarm nobody reads. The fix reports slots against slots
# and keeps the raw run count as a separate labelled number.
# --------------------------------------------------------------------------- #
def test_slots_covered_is_not_the_raw_run_count(monkeypatch, tmp_path):
    """THE REGRESSION, in the shape logged on 2026-07-23: nine runs journaled,
    seven slots elapsed, and one slot nonetheless uncovered. `hit` must count
    SLOTS so the alarm can compare like with like."""
    # 06:00 and 09:00 each fire twice; 16:00 never fires.
    hours = [6.1, 6.4, 8.9, 9.3, 10.7, 12.3, 14.3, 17.6, 17.9]
    r = _report(monkeypatch, tmp_path, hours, now_h=23.0)
    assert "15:30" in r["missed_slots"]
    assert r["hit"] < len(hours), "slots covered must not equal the raw run count"
    assert r["hit"] + r["missed_count"] == r["elapsed"], (
        "slots covered + slots missed must reconcile to elapsed slots — this is "
        "the identity the old 'expected N, completed M' message violated")


def test_drift_is_recorded_for_covered_slots(monkeypatch, tmp_path):
    """Covered and on-time are different facts. Measured 07-20..24, NO run landed
    on time (median +19min), which is the real reason 10:00 keeps getting eaten."""
    r = _report(monkeypatch, tmp_path, [6.0, 9.25], now_h=11.5)
    by = {s["label"]: s for s in r["slots"]}
    assert by["06:00"]["drift_min"] == 0
    assert by["08:45"]["drift_min"] == 30
    assert r["max_drift_min"] == 30


def test_worst_case_drift_still_lands_inside_its_own_slot(monkeypatch, tmp_path):
    """Drift measured 07-20..24 ran +15..30min with a worst case of +43. Under the
    OLD 09:00/10:00 pair a +43 run landed at 09:43, inside the window 10:00 also
    claimed. Under 08:45/10:30 the same drift lands at 09:28 — still unambiguously
    08:45's, with 62 minutes of daylight before 10:30's window opens."""
    r = _report(monkeypatch, tmp_path, [8.75 + 43 / 60, 10.5 + 43 / 60], now_h=12.5)
    by = {s["label"]: s for s in r["slots"]}
    assert by["08:45"]["status"] == "hit"
    assert by["10:30"]["status"] == "hit"
    assert by["08:45"]["drift_min"] == 43 and by["10:30"]["drift_min"] == 43
