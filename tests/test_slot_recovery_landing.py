"""Self-healing must not eat the slot it is standing next to.

MEASURED 2026-08-03 by replaying journal/runs + journal/run_starts across the 11
weekdays 2026-07-20..08-03. The watchdog worked; its OUTPUT was the problem.

  * 2026-07-29 and 2026-08-03: the 08:45 slot never fired, the watchdog re-ran it, and
    the recovery landed at 10:29 ET — one minute inside the 10:30 window [10:15, 11:30).
    slot_report matches runs to slots by timestamp, so it credited the recovery to
    10:30, left 08:45 reading MISSED all day, and orphaned the REAL 10:30 run at 10:44.
    Both runs are in the journal saying so in their own words: "Recovery run for the
    missed 08:45 ET holdings_watchlist slot".
  * 2026-07-23/27/28/29/31: the 16:00 slot never fired, the watchdog re-ran it, and the
    recovery landed 17:29-17:35 — inside the 17:30 window and ~90 minutes after the
    closing bell. Useless as a trade, destructive as a record.

Two causes, both pinned below. The detector never accounted for the time the recovery
run itself takes (measured 12.5 min median / 23.4 p90 over 52 breadcrumb->summary
pairs), and it could not call a slot missed until slot_report's 1-hour grace window
closed, which for a 16:00 slot is 17:00 — too late for any recovery to land in time.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import runlib.analytics as A  # noqa: E402
import scripts.find_missed_slot as fm  # noqa: E402

DAY = "2026-07-20"


def _journal(tmp_path, run_hours, start_hours=()):
    """Write run summaries and run_start breadcrumbs at the given ET hours."""
    for sub, hours in (("runs", run_hours), ("run_starts", start_hours)):
        d = tmp_path / "journal" / sub
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for i, h in enumerate(hours):
            utc_h = h + 4  # ET -> UTC in July
            rec = {"ts": f"{DAY}T{int(utc_h):02d}:{int(round((utc_h % 1) * 60)):02d}:00"
                         f"+00:00",
                   "run_id": f"{sub}-{i}", "node": "vm", "manual": False}
            if sub == "run_starts":
                rec["slot"] = None
                rec["stage"] = "start"
            lines.append(json.dumps(rec))
        (d / f"{DAY}.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))


def _find(monkeypatch, tmp_path, run_hours, now_h, start_hours=()):
    _journal(tmp_path, run_hours, start_hours)
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: DAY)
    monkeypatch.setattr(A, "to_et_date", lambda ts: DAY)
    return fm.find(now_h=now_h, weekday=True)


# --- defect 1: the recovery must FINISH before the next slot's window opens ---------

def test_refuses_a_recovery_that_would_land_in_the_next_slots_window(monkeypatch,
                                                                     tmp_path):
    """THE 2026-07-29 / 2026-08-03 REGRESSION, at the exact clock time it happened.

    08:45 never fired. At 10:15 ET the old detector said "10:15 < 10:30, still this
    slot's trade, go" — and the run it launched landed at 10:29, inside the 10:30
    window. A recovery that cannot finish before its successor's window opens is not a
    recovery; it is a relabelling of the next slot.
    """
    r = _find(monkeypatch, tmp_path, [6.0], now_h=10.25)
    assert r["slot"] is None
    blocked = {b["slot"]: b for b in r["blocked"]}
    assert blocked["08:45"]["why"] == "would_land_in_next_window"
    assert "10:30" in blocked["08:45"]["detail"]


def test_the_same_slot_IS_recoverable_while_there_is_still_room(monkeypatch, tmp_path):
    """The mirror — without it, "never recover anything" passes the test above.

    At 09:30 the same missed 08:45 slot has a full hour of headroom before the 10:30
    window opens, so the recovery is both legal and useful.
    """
    r = _find(monkeypatch, tmp_path, [6.0], now_h=9.5)
    assert r["slot"] == "08:45"
    assert r["depth"] == "holdings_watchlist"
    assert r["hhmm"] == "0845"


def test_a_late_afternoon_recovery_never_takes_the_evening_slot(monkeypatch, tmp_path):
    """The 07-23/27/28/29/31 shape: the afternoon full scan never fired, and by the
    time a recovery could be decided it could only land on top of 17:30.

    The slot itself moved 16:00 -> 15:30 on 2026-08-03, which does not change the
    shape — it buys headroom. At 17:00 a 15:30 recovery would still land ~17:25,
    inside 17:30's window, so it stays blocked. What the move DOES change is that
    the same miss is now recoverable earlier: 15:30 + NO_SHOW_H is 16:00, and a
    recovery launched then lands ~16:25, comfortably clear of 17:30."""
    r = _find(monkeypatch, tmp_path, [6.0, 8.75, 10.5, 12.0, 14.0], now_h=17.0)
    assert r["slot"] is None
    assert any(b["slot"] == "15:30" and b["why"] == "would_land_in_next_window"
               for b in r["blocked"])


def test_the_last_slot_of_the_day_has_a_finite_launch_deadline(monkeypatch, tmp_path):
    """17:30 has no successor, so the next-slot bound is infinite. The deadline must
    fall back to the age cap rather than formatting infinity into a clock time."""
    r = _find(monkeypatch, tmp_path, [6.0, 8.75, 10.5, 12.0, 14.0, 16.0], now_h=18.67)
    assert r["slot"] == "17:30"
    assert r["launch_by_et"] == "20:05"          # 17:30 + 3h age cap - 25min run
    assert r["expected_landing_et"] == "19:05"


# --- defect 2: a slot that never fired is detectable long before its grace closes ---

def test_a_slot_with_no_breadcrumb_is_a_no_show_before_the_grace_window_closes(
        monkeypatch, tmp_path):
    """slot_report will not call 10:30 missed until 11:30. But the run-start breadcrumb
    is pushed within 1-10 minutes of every slot that actually fires (n=52, worst +10),
    so at 11:00 with no breadcrumb and no summary the slot demonstrably never fired —
    and waiting the other half hour is what made every recovery too late to matter."""
    r = _find(monkeypatch, tmp_path, [8.75], now_h=11.0)
    assert r["slot"] == "10:30"
    assert r["status"] == "no_show"
    # 10:30 was promoted to a full universe scan on 2026-08-03, which is exactly why
    # losing it costs the day's only in-session look at the whole universe.
    assert r["depth"] == "full"


def test_a_slot_that_DID_fire_is_left_alone_inside_its_grace(monkeypatch, tmp_path):
    """The safety half of the early detection: a breadcrumb at 10:33 means the routine
    fired and is still working. Re-running it would put two live cycles on the same
    book — the exact thing the lock and the lease exist to prevent."""
    r = _find(monkeypatch, tmp_path, [8.75], now_h=11.0, start_hours=[10.55])
    assert r["slot"] is None


def test_a_slot_still_inside_the_no_show_window_is_not_touched(monkeypatch, tmp_path):
    """30 minutes is a 3x margin over the worst measured fire delay; before that the
    honest answer is 'unknown', not 'missed'."""
    r = _find(monkeypatch, tmp_path, [8.75], now_h=10.75)
    assert r["slot"] is None


# --- the detector must read the LIVE schedule, not runlib.depths' defaults ---------

def test_depth_comes_from_the_live_config(monkeypatch, tmp_path):
    """It used to call slot_depth_from_hhmm() with no cfg, grading against hardcoded
    defaults. A config that drifts from those defaults would send the recovery run at
    the wrong depth with nothing to catch it."""
    _journal(tmp_path, [6.0])
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: DAY)
    monkeypatch.setattr(A, "to_et_date", lambda ts: DAY)
    cfg = {"schedule": {"slot_depths": {"0845": "light"}}}
    r = fm.find(now_h=9.5, weekday=True, cfg=cfg)
    assert r["slot"] == "08:45" and r["depth"] == "light"


def test_the_null_result_says_why_not_just_nothing(monkeypatch, tmp_path):
    """"no recoverable missed slot right now" reads identically on a healthy day and on
    a day with an unrecoverable hole in it. The blocked list is the difference."""
    r = _find(monkeypatch, tmp_path, [6.0], now_h=10.25)
    assert r["slot"] is None
    assert "08:45" in r["reason"] and r["blocked"]


def test_main_still_emits_valid_json_on_a_bare_tree(monkeypatch, tmp_path, capsys):
    """Fail-quiet contract: a watchdog that crashes does nothing this tick."""
    monkeypatch.setattr(A, "ROOT", tmp_path)
    (tmp_path / "journal" / "runs").mkdir(parents=True)
    fm.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert "slot" in out


# --- the recovery run must be able to declare which slot it answers for ------------

def test_mark_run_start_accepts_an_explicit_slot(monkeypatch, tmp_path):
    """A run re-doing 08:45 at 10:29 is not the 10:30 run. The breadcrumb has to be able
    to say so; the clock cannot."""
    import journal
    import mark_run_start as mk
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal")
    monkeypatch.setattr(mk, "_et_now_hour", lambda: (10.48, "10:29"))
    monkeypatch.setattr(mk, "_et_is_weekday", lambda: True)

    assert mk.run(stage="recovery", no_push=True, slot="08:45") == 0
    rec = json.loads(
        next((tmp_path / "journal" / "run_starts").glob("*.jsonl")).read_text()
        .splitlines()[-1])
    assert rec["slot"] == "08:45"
    assert rec["recovery_for"] == "08:45"
    assert rec["stage"] == "recovery"


def test_the_recovery_slot_travels_through_the_environment(monkeypatch, tmp_path):
    """orchestrator.py re-invokes the marker itself under --gather-only with NO
    arguments, so a recovery has no flag to pass. $EE_RECOVERY_SLOT reaches the marker
    through both processes of the cloud flow without editing the orchestrator."""
    import journal
    import mark_run_start as mk
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal")
    monkeypatch.setattr(mk, "_et_now_hour", lambda: (10.48, "10:29"))
    monkeypatch.setattr(mk, "_et_is_weekday", lambda: True)
    monkeypatch.setenv("EE_RECOVERY_SLOT", "08:45")

    assert mk.run(no_push=True) == 0
    rec = json.loads(
        next((tmp_path / "journal" / "run_starts").glob("*.jsonl")).read_text()
        .splitlines()[-1])
    assert rec["slot"] == "08:45" and rec["recovery_for"] == "08:45"


def test_an_unknown_slot_label_falls_back_instead_of_being_journaled(monkeypatch,
                                                                    tmp_path):
    """A typo must not stamp a label no consumer recognises — and must not raise, since
    the breadcrumb may never be the reason a run does not run."""
    import journal
    import mark_run_start as mk
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal")
    monkeypatch.setattr(mk, "_et_now_hour", lambda: (14.08, "14:05"))
    monkeypatch.setattr(mk, "_et_is_weekday", lambda: True)

    assert mk.run(no_push=True, slot="8:45") == 0
    rec = json.loads(
        next((tmp_path / "journal" / "run_starts").glob("*.jsonl")).read_text()
        .splitlines()[-1])
    assert rec["slot"] == "14:00"          # fell back to the clock
    assert "recovery_for" not in rec
