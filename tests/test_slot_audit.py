"""The schedule audit must reproduce the failure it was written to find.

tools/slot_audit.py exists because nothing could answer "what has the schedule actually
been doing over the last fortnight" — only "how is today going". So a systematic defect
(the 16:00 slot covered on 5 of 11 weekdays, every recovery run taking the next slot's
window) looked like a string of unrelated one-day alarms.

The fixture below is the real 2026-08-03 day, to the minute, from journal/runs and
journal/run_starts.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import slot_audit as sa  # noqa: E402

DAY = "2026-08-03"


def _write(tmp_path, subdir, records):
    d = tmp_path / "journal" / subdir
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (et_hour, extra) in enumerate(records):
        utc = et_hour + 4  # ET -> UTC, August
        rec = {"ts": f"{DAY}T{int(utc):02d}:{int(round((utc % 1) * 60)):02d}:00+00:00",
               "run_id": f"{subdir}-{i}", "node": "vm", "manual": False, **extra}
        lines.append(json.dumps(rec))
    (d / f"{DAY}.jsonl").write_text("\n".join(lines) + "\n")


def _real_0803(tmp_path, with_marker=False):
    """2026-08-03 as journaled: 06:00 fired, 08:45 never did, the watchdog's 08:45
    recovery landed 10:29, the REAL 10:30 run landed 10:44, then 12:00 and 14:00."""
    _write(tmp_path, "runs", [
        (0.22, {"run_depth": "full"}),                    # nightly news run, out of band
        (6.17, {"run_depth": "light"}),
        (10.48, {"run_depth": "holdings_watchlist",
                 "no_trade_reason": "Recovery run for the missed 08:45 ET "
                                    "holdings_watchlist slot."}),
        (10.73, {"run_depth": "holdings_watchlist"}),
        (12.25, {"run_depth": "holdings_watchlist"}),
        (14.25, {"run_depth": "holdings_watchlist"}),
    ])
    starts = [(6.05, {"slot": "06:00"}), (10.55, {"slot": "10:30"}),
              (12.07, {"slot": "12:00"}), (14.07, {"slot": "14:00"})]
    if with_marker:
        # What the recovery WILL look like once it stamps its own slot (--slot 08:45).
        starts.insert(1, (10.40, {"slot": "08:45", "recovery_for": "08:45"}))
    _write(tmp_path, "run_starts", starts)


def _audit(tmp_path):
    runs = sa._load("runs", tmp_path)
    starts = sa._load("run_starts", tmp_path)
    return sa.audit_day(DAY, runs.get(DAY, []), starts.get(DAY, []))


def test_the_0845_recovery_is_credited_to_1030_and_orphans_the_real_run(tmp_path):
    """THE MEASURED FAILURE. This is what the heartbeat reported on 2026-07-29 and
    2026-08-03: 08:45 missed, 10:30 apparently fine, and no mention anywhere that a run
    for 08:45 had actually happened or that the real 10:30 run went uncounted."""
    _real_0803(tmp_path)
    r = _audit(tmp_path)
    by = {e["label"]: e for e in r["slots"]}
    assert by["08:45"]["status"] == "missed"
    assert by["10:30"]["status"] == "hit"
    assert by["10:30"]["at"] == "10:29", "the 08:45 recovery took the 10:30 window"
    assert [o["at"] for o in r["in_band_orphans"]] == ["10:44"]


def test_the_out_of_band_nightly_run_is_not_counted_as_an_orphan(tmp_path):
    """The 00:13 ET cloud news run is an orphan by design (expected_slots says so).
    Counting it would put a permanent orphan on the board and train the operator to
    ignore the number that matters."""
    _real_0803(tmp_path)
    r = _audit(tmp_path)
    assert any(o["at"] == "00:13" for o in r["orphans"])
    assert all(o["at"] != "00:13" for o in r["in_band_orphans"])


def test_a_stamped_recovery_is_reported_as_a_collision(tmp_path):
    """Once the recovery declares itself (scripts/mark_run_start.py --slot), a slot
    credited to a run that was really recovering a DIFFERENT slot is nameable instead
    of merely suspicious."""
    _real_0803(tmp_path, with_marker=True)
    r = _audit(tmp_path)
    assert "10:30" in r["collisions"]


def test_drift_is_decomposed_into_fire_delay_and_work(tmp_path):
    """THE REFRAME. slot_report measures drift from the run SUMMARY, written when the
    run FINISHES, so "median +19 min drift" reads as a late cron. Split against the
    breadcrumb it is a punctual cron (+1..+10 min on all 52 measured fires) plus a run
    that takes 12-25 minutes. Those two facts need opposite responses."""
    _real_0803(tmp_path)
    r = _audit(tmp_path)
    twelve = next(e for e in r["slots"] if e["label"] == "12:00")
    assert twelve["drift_min"] == 15
    assert twelve["fire_delay_min"] == 4
    assert twelve["run_minutes"] == 11


def test_history_aware_slot_list(tmp_path):
    """08:45/10:30 replaced 09:00/10:00 on 2026-07-25. Grading a 2026-07-22 run against
    today's list is what produces the phantom "+37 min drift at 08:45" — the 09:22 runs
    of that week were +22 against the 09:00 slot they were actually scheduled for."""
    assert sa.slots_for("2026-07-22")[1] == 9
    assert sa.slots_for("2026-07-30")[1] == 8.75


def test_a_torn_journal_line_is_skipped_not_fatal(tmp_path):
    """journal.py appends non-atomically; a crash mid-write leaves a partial line. An
    audit that dies on one bad byte is an audit nobody can run during an incident."""
    _real_0803(tmp_path)
    f = tmp_path / "journal" / "runs" / f"{DAY}.jsonl"
    f.write_text(f.read_text() + '{"ts": "2026-08-03T20:0')
    r = _audit(tmp_path)
    assert r["covered"] >= 4
