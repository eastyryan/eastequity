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

SLOTS = [6, 9, 10, 12, 14, 16, 17.5]


def _journal(tmp_path, hours, node="vm"):
    """Write a run journal with completed runs at the given ET hours."""
    runs = tmp_path / "journal" / "runs"
    runs.mkdir(parents=True)
    # ET is UTC-4 in July; the loader converts, so write UTC and let it do the work.
    lines = []
    for i, h in enumerate(hours):
        utc_h = h + 4
        lines.append(json.dumps({
            "ts": f"2026-07-20T{int(utc_h):02d}:{int(round((utc_h % 1) * 60)):02d}:00+00:00",
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
    r = _report(monkeypatch, tmp_path, [6.0, 9.0, 10.0, 12.0], now_h=17.1)
    assert r["missed_slots"] == ["14:00", "16:00"]


def test_two_runs_in_one_slot_do_not_cover_a_different_slot(monkeypatch, tmp_path):
    """Subtraction's blind spot: 5 runs against 5 elapsed slots netted to zero missed
    even when two of them landed in the same slot and a real one was empty."""
    r = _report(monkeypatch, tmp_path, [9.0, 9.5, 12.0, 14.0, 6.0], now_h=16.5)
    assert "10:00" in r["missed_slots"], "a doubled-up slot masked an empty one"


def test_a_slot_inside_its_grace_window_is_pending_not_missed(monkeypatch, tmp_path):
    """Cron jitter absorption. At 14:30 the 14:00 slot is 30 min late, not dead —
    GitHub coalesces crons under load and paging on that trains you to ignore it."""
    r = _report(monkeypatch, tmp_path, [6.0, 9.0, 10.0, 12.0], now_h=14.5)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["14:00"] == "pending"
    assert r["missed_count"] == 0


def test_grace_expires_and_the_slot_flips_to_missed(monkeypatch, tmp_path):
    """The mirror of the test above — without it, 'always pending' would pass."""
    r = _report(monkeypatch, tmp_path, [6.0, 9.0, 10.0, 12.0], now_h=15.1)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["14:00"] == "missed"


def test_grace_never_bleeds_into_the_next_slot(monkeypatch, tmp_path):
    """9:00 and 10:00 are closer together than the 1h grace. One run at 9:55 must
    satisfy 9:00 only — otherwise a single run covers two slots and 10:00 reads hit."""
    r = _report(monkeypatch, tmp_path, [9.92], now_h=12.5)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["09:00"] == "hit"
    assert statuses["10:00"] == "missed"


def test_a_slightly_early_run_still_counts_for_its_slot(monkeypatch, tmp_path):
    """A run at 8:52 is the 9am slot firing early, not a missed 9am."""
    r = _report(monkeypatch, tmp_path, [8.87], now_h=12.5)
    statuses = {s["label"]: s["status"] for s in r["slots"]}
    assert statuses["09:00"] == "hit"


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
    assert next(s for s in r["slots"] if s["label"] == "09:00")["node"] == "vm"


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
