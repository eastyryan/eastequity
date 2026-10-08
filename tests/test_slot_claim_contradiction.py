"""A recovery claim that the run's own gather contradicts must not steal a slot.

THE INCIDENT (2026-10-08, replayed verbatim below). A late routine ran
`mark_run_start.py --slot 14:00` at 15:22:05 ET, then an UNPINNED
`orchestrator.py --gather-only --auto-depth`. The orchestrator re-marks under
--gather-only, and 5 s later that marker said 15:00 with no claim: the gather
resolved its depth from the clock. The run (20261008-433eed, 15:41) summarised
itself as "Full 15:00 slot ...". slot_report's pre-pass credited it to 14:00 on
the claim alone. That left 15:00 "died" (a second 15:00 attempt marked at 15:43
lost its push race and was discarded), and from 16:00 find_missed_slot asked
the watchdog to re-run 15:00, a slot that had already been served.

A real watchdog recovery exports EE_RECOVERY_SLOT around BOTH processes, so its
gather marker repeats the claim. That stays credited to the claimed slot.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import runlib.analytics as A  # noqa: E402
import scripts.find_missed_slot as fm  # noqa: E402

DAY = "2026-10-08"
NODE = "grok-bot-vm-390581422"

# ts are UTC (EDT = UTC-4), exactly as journaled on 2026-10-08.
RUNS_1008 = [
    {"ts": "2026-10-08T10:03:00+00:00", "run_id": "20261008-b8e47d"},
    {"ts": "2026-10-08T13:09:59+00:00", "run_id": "20261008-c5cce9"},
    {"ts": "2026-10-08T19:41:39+00:00", "run_id": "20261008-433eed"},
]
STARTS_1008 = [
    {"ts": "2026-10-08T09:59:03+00:00", "slot": "06:00"},
    {"ts": "2026-10-08T09:59:10+00:00", "slot": "06:00"},
    {"ts": "2026-10-08T12:58:49+00:00", "slot": "08:45"},
    {"ts": "2026-10-08T13:00:35+00:00", "slot": "08:45"},
    {"ts": "2026-10-08T19:22:05+00:00", "slot": "14:00", "recovery_for": "14:00"},
    {"ts": "2026-10-08T19:22:10+00:00", "slot": "15:00"},
    {"ts": "2026-10-08T19:43:36+00:00", "slot": "15:00"},
    {"ts": "2026-10-08T19:43:40+00:00", "slot": "15:00"},
]


def _write(tmp_path, runs, starts, monkeypatch):
    for sub, recs in (("runs", runs), ("run_starts", starts)):
        d = tmp_path / "journal" / sub
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for r in recs:
            rec = {"node": NODE, "manual": False, **r}
            if sub == "run_starts":
                rec.setdefault("stage", "start")
            lines.append(json.dumps(rec))
        (d / f"{DAY}.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: DAY)


def _by_label(rep):
    return {s["label"]: s for s in rep["slots"]}


def test_the_1008_run_is_credited_to_the_slot_it_actually_ran(tmp_path, monkeypatch):
    _write(tmp_path, RUNS_1008, STARTS_1008, monkeypatch)

    rep = A.slot_report(now_h=16.6, weekday=True)
    s = _by_label(rep)

    assert s["15:00"]["status"] == "hit"
    assert s["15:00"]["run_id"] == "20261008-433eed"
    assert s["14:00"]["status"] == "missed", "14:00 never fired inside its window"
    assert s["14:00"].get("run_id") is None


def test_no_false_1500_recovery_after_4pm(tmp_path, monkeypatch):
    """Before the fix, find() returned {"slot": "15:00", "status": "died"} from 16:00."""
    _write(tmp_path, RUNS_1008, STARTS_1008, monkeypatch)
    for now_h in (16.05, 16.6):
        r = fm.find(now_h=now_h, weekday=True)
        assert r["slot"] is None, r
        assert "15:00" not in {b["slot"] for b in r["blocked"]}


def test_a_pinned_recovery_is_still_credited_to_the_slot_it_claims(tmp_path,
                                                                    monkeypatch):
    """The mirror. A watchdog recovery of 08:45 landing at 10:29 (EE_RECOVERY_SLOT
    set around both processes, so both markers carry the claim) belongs to 08:45,
    and the real 10:30 run is not orphaned (2026-07-29 / 08-03)."""
    runs = [{"ts": "2026-10-08T10:03:00+00:00", "run_id": "r0600"},
            {"ts": "2026-10-08T14:29:00+00:00", "run_id": "r_recovery"},
            {"ts": "2026-10-08T14:44:00+00:00", "run_id": "r1030"}]
    starts = [{"ts": "2026-10-08T14:08:00+00:00", "slot": "08:45",
               "recovery_for": "08:45"},
              {"ts": "2026-10-08T14:08:05+00:00", "slot": "08:45",
               "recovery_for": "08:45"},
              {"ts": "2026-10-08T14:31:00+00:00", "slot": "10:30"}]
    _write(tmp_path, runs, starts, monkeypatch)

    s = _by_label(A.slot_report(now_h=11.6, weekday=True))

    assert s["08:45"]["run_id"] == "r_recovery" and s["08:45"].get("recovered")
    assert s["10:30"]["run_id"] == "r1030"


def test_another_nodes_marker_does_not_void_a_claim(tmp_path, monkeypatch):
    """Only the claiming node's own gather marker can contradict its claim."""
    runs = [{"ts": "2026-10-08T14:29:00+00:00", "run_id": "r_recovery"}]
    starts = [{"ts": "2026-10-08T14:08:00+00:00", "slot": "08:45",
               "recovery_for": "08:45"},
              {"ts": "2026-10-08T14:10:00+00:00", "slot": "10:30",
               "node": "some-other-node"}]
    _write(tmp_path, runs, starts, monkeypatch)

    s = _by_label(A.slot_report(now_h=10.6, weekday=True))

    assert s["08:45"]["run_id"] == "r_recovery"


def test_a_marker_after_the_run_does_not_void_the_claim(tmp_path, monkeypatch):
    """The next scheduled routine marking after the recovery already landed is not
    evidence about the recovery."""
    runs = [{"ts": "2026-10-08T14:29:00+00:00", "run_id": "r_recovery"}]
    starts = [{"ts": "2026-10-08T14:08:00+00:00", "slot": "08:45",
               "recovery_for": "08:45"},
              {"ts": "2026-10-08T14:31:00+00:00", "slot": "10:30"}]
    _write(tmp_path, runs, starts, monkeypatch)

    s = _by_label(A.slot_report(now_h=10.6, weekday=True))

    assert s["08:45"]["run_id"] == "r_recovery"
