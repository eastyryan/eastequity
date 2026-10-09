"""A late FULL slot keeps its slot type until it lands or the next window opens.

THE INCIDENT (2026-10-09). The 10:30 full routine fired at 11:07 ET. Its act at
about 11:16 re-resolved --auto-depth from the clock with the NEAREST-slot rule:
11:16 is 46 min after 10:30 and 44 min before 12:00, so it labelled itself the
12:00 holdings_watchlist slot. That would cost a late full run its starter-buy
power.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlib.analytics as A  # noqa: E402
from runlib.depths import resolve_depth, slot_depth_from_hhmm  # noqa: E402

DAY = "2026-10-09"
ET = ZoneInfo("America/New_York")


def _utc(et_hhmm: str) -> str:
    h, m = int(et_hhmm[:2]), int(et_hhmm[3:])
    return datetime(2026, 10, 9, h, m, tzinfo=ET).astimezone(
        ZoneInfo("UTC")).isoformat()


@pytest.fixture
def journal(tmp_path, monkeypatch):
    runs = tmp_path / "journal" / "runs"
    runs.mkdir(parents=True)
    (tmp_path / "journal" / "run_starts").mkdir(parents=True)
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: DAY)

    def land(*et_times):
        (runs / f"{DAY}.jsonl").write_text("".join(
            json.dumps({"ts": _utc(t), "run_id": f"r{i}", "node": "box",
                        "run_depth": "full"}) + "\n"
            for i, t in enumerate(et_times)))
    land("06:07", "09:16")          # 06:00 and 08:45 served, 10:30 not yet
    return land


def h(hhmm: str) -> float:
    return int(hhmm[:2]) + int(hhmm[3:]) / 60


def test_the_old_nearest_rule_is_what_mislabelled_1116():
    assert slot_depth_from_hhmm("1116") == "holdings_watchlist"


def test_the_late_1030_act_keeps_full(journal):
    for t in ("11:07", "11:16", "11:30", "11:44"):
        held = A.held_full_slot(now_h=h(t), weekday=True)
        assert held == {"slot": "10:30", "hhmm": "1030", "depth": "full"}, t
    assert resolve_depth(hhmm="1030") == "full"


def test_once_the_next_window_opens_the_next_slot_rules(journal):
    assert A.held_full_slot(now_h=h("11:46"), weekday=True) is None


def test_a_landed_slot_is_not_held(journal):
    journal("06:07", "09:16", "11:18")      # 10:30 landed at 11:18
    assert A.held_full_slot(now_h=h("11:20"), weekday=True) is None


def test_only_full_slots_are_held(journal):
    journal("06:07")                         # 08:45 never landed
    assert A.held_full_slot(now_h=h("09:50"), weekday=True) is None
    assert A.held_full_slot(now_h=h("12:50"), weekday=True) is None


def test_1400_holds_until_the_1500_window(journal):
    assert A.held_full_slot(now_h=h("14:40"), weekday=True)["slot"] == "14:00"
    assert A.held_full_slot(now_h=h("14:50"), weekday=True)["slot"] == "15:00"


def test_no_hold_after_the_primary_window(journal):
    assert A.held_full_slot(now_h=h("16:10"), weekday=True)["slot"] == "15:00"
    assert A.held_full_slot(now_h=h("16:20"), weekday=True) is None


def test_weekends_unaffected(journal):
    assert A.held_full_slot(now_h=h("11:16"), weekday=False) is None


# ---- orchestrator wiring ---------------------------------------------------------

@pytest.fixture
def orch(monkeypatch):
    import orchestrator
    monkeypatch.setattr(orchestrator, "et_now",
                        lambda: datetime(2026, 10, 9, 11, 16, tzinfo=ET))
    return orchestrator


def test_auto_depth_resolves_full_for_the_late_1030_act(orch, monkeypatch, capsys):
    monkeypatch.setattr(A, "held_full_slot", lambda **k: {
        "slot": "10:30", "hhmm": "1030", "depth": "full"})
    args = orch._parse_args(["--act-on", "r.md", "--context", "c.json",
                             "--auto-depth"])
    hhmm = orch._auto_depth_hhmm(args, {})
    assert hhmm == "1030" and resolve_depth(hhmm=hhmm) == "full"
    assert "keeping depth 'full'" in capsys.readouterr().out


def test_without_a_held_slot_the_clock_rules(orch, monkeypatch):
    monkeypatch.setattr(A, "held_full_slot", lambda **k: None)
    args = orch._parse_args(["--gather-only", "--auto-depth"])
    assert orch._auto_depth_hhmm(args, {}) == "1116"


def test_explicit_depth_flags_are_untouched(orch, monkeypatch):
    def boom(**k):
        raise AssertionError("not consulted")
    monkeypatch.setattr(A, "held_full_slot", boom)
    args = orch._parse_args(["--gather-only", "--auto-depth", "--depth", "light"])
    assert orch._auto_depth_hhmm(args, {}) == "1116"


def test_the_hold_fails_open(orch, monkeypatch):
    def boom(**k):
        raise RuntimeError("journal unreadable")
    monkeypatch.setattr(A, "held_full_slot", boom)
    args = orch._parse_args(["--gather-only", "--auto-depth"])
    assert orch._auto_depth_hhmm(args, {}) == "1116"
