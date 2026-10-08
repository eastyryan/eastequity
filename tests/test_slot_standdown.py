"""One journaled run per scheduled slot, and a stale --slot claim is harmless.

THE INCIDENT (2026-10-08). Run 20261008-433eed served the 15:00 full slot at
15:41 ET. A second routine marked 15:00 at 15:43, gathered, reasoned, and acted
on 15:00 again at 15:53. Its push lost a race with stop-watch and the duplicate
was discarded (0 fills). Earlier, a late routine ran
`mark_run_start.py --slot 14:00` at 15:22 as an ordinary start; that stale claim
is what got 433eed credited to 14:00.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import runlib.analytics as A  # noqa: E402

DAY = "2026-10-08"
NODE = "grok-bot-vm-390581422"
RUNS = [{"ts": "2026-10-08T10:03:00+00:00", "run_id": "20261008-b8e47d"},
        {"ts": "2026-10-08T13:09:59+00:00", "run_id": "20261008-c5cce9"},
        {"ts": "2026-10-08T19:41:39+00:00", "run_id": "20261008-433eed"}]
STARTS = [{"ts": "2026-10-08T19:22:05+00:00", "slot": "14:00", "recovery_for": "14:00"},
          {"ts": "2026-10-08T19:22:10+00:00", "slot": "15:00"},
          {"ts": "2026-10-08T19:43:36+00:00", "slot": "15:00"}]


@pytest.fixture
def journals(tmp_path, monkeypatch):
    for sub, recs in (("runs", RUNS), ("run_starts", STARTS)):
        d = tmp_path / "journal" / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{DAY}.jsonl").write_text("\n".join(
            json.dumps({"node": NODE, "manual": False, "stage": "start", **r})
            for r in recs) + "\n")
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: DAY)
    return tmp_path


# ---- slot_already_served ------------------------------------------------------

def test_the_duplicate_1500_run_sees_its_slot_already_served(journals):
    served = A.slot_already_served(now_h=15 + 53 / 60, weekday=True)   # 15:53 ET
    assert served["slot"] == "15:00"
    assert served["run_id"] == "20261008-433eed"


def test_an_unserved_slot_is_not_stood_down(journals):
    assert A.slot_already_served(now_h=14.5, weekday=True) is None      # 14:30 ET
    assert A.slot_already_served(now_h=15.2, weekday=True) is None      # before 433eed


def test_weekends_never_stand_down(journals):
    assert A.slot_already_served(now_h=15.9, weekday=False) is None


def test_off_slot_times_have_no_slot():
    assert A.clock_slot_label(0.2, True) is None
    assert A.clock_slot_label(15.9, True) == "15:00"
    assert A.clock_slot_label(13.8, True) == "14:00"     # early tolerance
    assert A.clock_slot_label(18.2, True) == "17:30"
    assert A.clock_slot_label(19.6, True) is None


# ---- orchestrator gate ---------------------------------------------------------

def _args(**kw):
    import orchestrator
    a = orchestrator._parse_args(kw.pop("argv"))
    return a


SERVED = {"slot": "15:00", "run_id": "20261008-433eed", "drift_min": 41,
          "recovered": False}


@pytest.fixture
def gate(monkeypatch):
    import orchestrator
    monkeypatch.setattr(A, "slot_already_served", lambda *a, **k: SERVED)
    monkeypatch.delenv("EE_RECOVERY_SLOT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("EE_SCHEDULED_TRADER", raising=False)
    return orchestrator


def test_a_scheduled_act_on_stands_down(gate):
    msg = gate._scheduled_slot_standdown(_args(argv=[
        "--act-on", "state/brain_response.md", "--context", "x.json", "--auto-depth"]))
    assert msg.startswith("STAND DOWN: scheduled slot 15:00")
    assert "20261008-433eed" in msg


def test_a_scheduled_routine_gather_stands_down(gate):
    assert gate._scheduled_slot_standdown(
        _args(argv=["--gather-only", "--auto-depth"])) is not None


def test_the_github_bundle_refresh_is_never_stood_down(gate, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert gate._scheduled_slot_standdown(
        _args(argv=["--gather-only", "--auto-depth"])) is None


@pytest.mark.parametrize("argv", [
    ["--act-on", "r.md", "--context", "c.json", "--depth", "full"],      # pinned
    ["--act-on", "r.md", "--context", "c.json", "--auto-depth", "--manual"],
    ["--act-on", "r.md", "--context", "c.json", "--auto-depth",
     "--trigger-run", "COST"],
])
def test_unscheduled_runs_are_never_stood_down(gate, argv):
    assert gate._scheduled_slot_standdown(_args(argv=argv)) is None


def test_a_watchdog_recovery_is_never_stood_down(gate, monkeypatch):
    monkeypatch.setenv("EE_RECOVERY_SLOT", "14:00")
    assert gate._scheduled_slot_standdown(
        _args(argv=["--gather-only", "--auto-depth"])) is None


def test_the_check_fails_open(gate, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("journal unreadable")
    monkeypatch.setattr(A, "slot_already_served", boom)
    assert gate._scheduled_slot_standdown(
        _args(argv=["--gather-only", "--auto-depth"])) is None


def test_main_exits_0_without_gathering(gate, monkeypatch, capsys):
    def no_gather(*a, **k):
        raise AssertionError("a stood-down run must not gather")
    monkeypatch.setattr(gate, "_run_gather_only", no_gather)
    monkeypatch.setattr(gate, "_earnings_escalation", lambda a, d, c: (d, None))
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--gather-only", "--auto-depth"])
    assert gate.main() == 0
    assert "STAND DOWN: scheduled slot 15:00" in capsys.readouterr().out


# ---- mark_run_start: stale / mismatched --slot ---------------------------------

def _mark(monkeypatch, tmp_path, now, **kw):
    import journal
    import mark_run_start as mk
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal")
    monkeypatch.setattr(mk, "_et_now_hour", lambda: now)
    monkeypatch.setattr(mk, "_et_is_weekday", lambda: True)
    assert mk.run(no_push=True, **kw) == 0
    return json.loads(next((tmp_path / "journal" / "run_starts").glob("*.jsonl"))
                      .read_text().splitlines()[-1])


def test_the_1008_stale_claim_falls_back_to_the_clock(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("EE_RECOVERY_SLOT", raising=False)
    rec = _mark(monkeypatch, tmp_path, (15 + 22 / 60, "15:22"), slot="14:00")
    assert rec["slot"] == "15:00"
    assert "recovery_for" not in rec
    assert "--slot 14:00 refused" in capsys.readouterr().out


def test_a_future_slot_claim_is_refused_too(monkeypatch, tmp_path):
    monkeypatch.delenv("EE_RECOVERY_SLOT", raising=False)
    rec = _mark(monkeypatch, tmp_path, (14.5, "14:30"), slot="15:00")
    assert rec["slot"] == "14:00" and "recovery_for" not in rec


def test_a_claim_matching_the_clock_is_kept(monkeypatch, tmp_path):
    monkeypatch.delenv("EE_RECOVERY_SLOT", raising=False)
    rec = _mark(monkeypatch, tmp_path, (15.1, "15:06"), slot="15:00")
    assert rec["slot"] == "15:00"


def test_a_watchdog_recovery_flag_is_still_honoured(monkeypatch, tmp_path):
    """find_missed_slot's mark_run_start_args: --slot X --stage recovery."""
    monkeypatch.delenv("EE_RECOVERY_SLOT", raising=False)
    rec = _mark(monkeypatch, tmp_path, (10.48, "10:29"), slot="08:45",
                stage="recovery")
    assert rec["slot"] == "08:45" and rec["recovery_for"] == "08:45"


def test_a_flag_backed_by_the_recovery_env_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("EE_RECOVERY_SLOT", "08:45")
    rec = _mark(monkeypatch, tmp_path, (10.48, "10:29"), slot="08:45")
    assert rec["slot"] == "08:45" and rec["recovery_for"] == "08:45"
