"""The watchdog's detector: name the slot to re-run, or nothing.

Self-healing must not become self-harming. The two failure modes to pin: re-running a
slot that has already been superseded by its successor (double-pricing a staler market),
and re-running a slot so late it is no longer that slot's trade.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.find_missed_slot as fm  # noqa: E402
import runlib.analytics as A  # noqa: E402


def _journal(tmp_path, hours):
    d = tmp_path / "journal" / "runs"
    d.mkdir(parents=True)
    lines = []
    for i, h in enumerate(hours):
        utc_h = h + 4
        lines.append(json.dumps({
            "ts": f"2026-07-20T{int(utc_h):02d}:{int(round((utc_h % 1) * 60)):02d}:00+00:00",
            "run_id": f"r{i}", "node": "vm", "manual": False}))
    (d / "2026-07-20.jsonl").write_text("\n".join(lines) + "\n")


def _find(monkeypatch, tmp_path, hours, now_h):
    _journal(tmp_path, hours)
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: "2026-07-20")
    monkeypatch.setattr(A, "to_et_date", lambda ts: "2026-07-20")
    # Freeze the calendar so weekday() inside find() reads a weekday regardless of when
    # the suite runs.
    return fm.find(now_h=now_h, weekday=True)


def test_names_the_missed_slot_and_its_depth(monkeypatch, tmp_path):
    """10:30 missed. Checked at 11:45 — past its 1h grace (so it is genuinely
    missed, not just late) and before the 12pm slot (so still recoverable)."""
    r = _find(monkeypatch, tmp_path, [8.75], now_h=11.75)
    assert r["slot"] == "10:30"
    assert r["depth"] == "holdings_watchlist"
    assert r["hhmm"] == "1030"


def test_silent_when_every_elapsed_slot_ran(monkeypatch, tmp_path):
    r = _find(monkeypatch, tmp_path, [6.0, 8.75, 10.5], now_h=11.75)
    assert r["slot"] is None


def test_a_slot_inside_its_grace_is_not_yet_recoverable(monkeypatch, tmp_path):
    """At 10:35 the 10am slot is 35 min late — inside its grace window, still 'pending'.
    The watchdog must not re-run a slot that may yet land on its own."""
    r = _find(monkeypatch, tmp_path, [9.0], now_h=10.58)
    assert r["slot"] is None


def test_does_not_resurrect_a_superseded_slot(monkeypatch, tmp_path):
    """2pm missed, but it is already 4:05pm — the 4pm full run is due and supersedes it.
    Re-running 2pm now would just trade a staler snapshot moments before 4pm."""
    r = _find(monkeypatch, tmp_path, [9.0, 10.0, 12.0], now_h=16.08)
    # 14:00 is superseded by 16:00 being due; 16:00 itself is still pending (not missed).
    assert r["slot"] != "14:00"


def test_does_not_resurrect_an_ancient_slot(monkeypatch, tmp_path):
    """A slot more than MAX_SLOT_AGE_H late is no longer that slot's trade even if,
    hypothetically, no later slot exists to supersede it."""
    # 17:30 is the last slot; miss it and check 3.5h later (21:00). No successor exists,
    # so only the age cap stops a pointless resurrection.
    r = _find(monkeypatch, tmp_path, [6.0, 9.0, 10.0, 12.0, 14.0, 16.0], now_h=21.0)
    assert r["slot"] is None


def test_recovers_the_last_slot_while_still_fresh(monkeypatch, tmp_path):
    """The mirror of the age test: 17:30 missed, checked at 18:40 — past its 1h grace,
    within the age cap, and with no successor, so it SHOULD be recovered."""
    r = _find(monkeypatch, tmp_path, [6.0, 9.0, 10.0, 12.0, 14.0, 16.0], now_h=18.67)
    assert r["slot"] == "17:30"
    assert r["depth"] == "evening_review"


def test_earliest_recoverable_slot_wins(monkeypatch, tmp_path):
    """The earliest still-recoverable slot is fixed first. At 7:30am the 6am slot is past
    its grace (missed) and before the 9am successor (recoverable); 9am has not come due
    yet. So 06:00 is the one to re-run."""
    r = _find(monkeypatch, tmp_path, [], now_h=7.5)
    assert r["slot"] == "06:00"
    assert r["depth"] == "light"


def test_main_emits_valid_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(A, "ROOT", tmp_path)
    (tmp_path / "journal" / "runs").mkdir(parents=True)
    fm.main()
    out = json.loads(capsys.readouterr().out.strip())
    assert "slot" in out
