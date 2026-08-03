"""The run-start breadcrumb: it must record the right slot and never abort a run.

This is the diagnostic half of the 2026-07-20 fix. If the marker mislabels its slot,
the heartbeat correlates it to the wrong window and a death hides in the wrong place;
if the marker can raise, it becomes the incident it exists to report.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mark_run_start as mk  # noqa: E402


def test_slot_label_matches_a_real_slot(monkeypatch):
    """At 14:05 ET the marker must claim the 14:00 slot — the SAME slot list the
    heartbeat grades against, so a marker cannot certify a slot the heartbeat ignores."""
    monkeypatch.setattr(mk, "_et_now_hour", lambda: (14.08, "14:05"))
    # BOTH halves of "when is it" must be pinned. This used to patch only the hour
    # and let the weekday fall through to the real clock, so the assertion below
    # was false every Saturday and Sunday (the weekend slot list is [0, 23.98])
    # and the suite went red on schedule twice a week.
    monkeypatch.setattr(mk, "_et_is_weekday", lambda: True)
    assert mk._current_slot_label() == "14:00"


def test_off_slot_time_gets_no_slot(monkeypatch):
    """A run fired at 4:45pm — more than an hour past the 3:30pm slot and before the
    5:30pm one — is off-slot and must not falsely certify either neighbour.

    THIS TIME MOVED 2026-08-03. The case used to be 3:30pm, which was off-slot when
    the afternoon full scan sat at 16:00; 15:30 is now the slot itself, so the old
    fixture was asserting that a real slot certifies nothing."""
    monkeypatch.setattr(mk, "_et_now_hour", lambda: (16.75, "16:45"))
    monkeypatch.setattr(mk, "_et_is_weekday", lambda: True)
    assert mk._current_slot_label() is None


def test_marker_is_written_with_slot_and_stage(monkeypatch, tmp_path):
    """The journaled record must carry slot + stage so slot_report can read it."""
    import journal
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal")
    monkeypatch.setattr(mk, "_et_now_hour", lambda: (14.08, "14:05"))
    monkeypatch.setattr(mk, "_et_is_weekday", lambda: True)

    rc = mk.main_for_test(no_push=True)
    assert rc == 0
    f = tmp_path / "journal" / "run_starts"
    files = list(f.glob("*.jsonl"))
    assert files, "no run_start breadcrumb written"
    rec = json.loads(files[0].read_text().splitlines()[-1])
    assert rec["slot"] == "14:00"
    assert rec["stage"] == "start"
    assert rec["node"]


def test_marker_never_raises_even_when_journal_is_broken(monkeypatch):
    """Fail-open contract: any internal error exits 0, so the marker can never be the
    reason a scheduled run does not run."""
    def boom(*a, **k):
        raise RuntimeError("disk full")

    import journal
    monkeypatch.setattr(journal, "log_run_start", boom)
    monkeypatch.setattr(mk, "_et_now_hour", lambda: (14.08, "14:05"))
    assert mk.main_for_test(no_push=True) == 0
