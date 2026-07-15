"""Offline tests for tools.exit_autopsy (temp journal root via ROOT patch).

    python3 tests/test_exit_autopsy.py
    python3 -m pytest tests/test_exit_autopsy.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.exit_autopsy as EA  # noqa: E402


@pytest.fixture
def autopsy_root(tmp_path, monkeypatch):
    """Point exit_autopsy at a temp repo root (journal/ under tmp)."""
    journal = tmp_path / "journal" / "exit_autopsies"
    journal.mkdir(parents=True)
    monkeypatch.setattr(EA, "ROOT", tmp_path)
    monkeypatch.setattr(EA, "AUTOPSY_DIR", journal)
    return tmp_path


def test_build_exit_autopsy_from_fill_forced(autopsy_root):
    fill = {
        "ticker": "HPE",
        "action": "SELL_TO_CLOSE",
        "fill_price": 44.0,
        "quantity": 10.0,
        "avg_cost": 48.0,
        "realized_pnl_usd": -40.0,
        "entry_plan": {"stop_loss": 45.0, "target_price": 58.0,
                       "holding_horizon_days": 40},
        "position_opened_at": "2026-07-01T15:00:00+00:00",
        "filled_at": "2026-07-10T15:00:00+00:00",
        "status": "filled",
    }
    order = {"ticker": "HPE", "action": "SELL_TO_CLOSE",
             "forced_exit_reason": "stop_loss_breached", "proposal_id": "run-x"}
    pos = {"ticker": "HPE", "days_held": 9, "avg_cost": 48.0,
           "plan": {"stop_loss": 45.0}}

    rec = EA.build_exit_autopsy_from_fill(
        fill, order, pos, forced=True, reason="stop_loss_breached",
    )
    assert rec["ticker"] == "HPE"
    assert rec["action"] == "SELL_TO_CLOSE"
    assert rec["fill_price"] == 44.0
    assert rec["quantity"] == 10.0
    assert rec["days_held"] == 9
    assert rec["entry_plan"]["stop_loss"] == 45.0
    assert rec["forced"] is True
    assert rec["forced_reason"] == "stop_loss_breached"
    assert rec["realized_pnl_usd"] == -40.0
    assert rec["status"] == "system_stub"
    assert rec["needs_brain_grade"] is True


def test_build_computes_pnl_and_days_when_missing(autopsy_root):
    fill = {
        "ticker": "AMD",
        "action": "SELL_TO_CLOSE",
        "fill_price": 110.0,
        "quantity": 5.0,
        "avg_cost": 100.0,
        "position_opened_at": "2026-07-01T12:00:00+00:00",
        "filled_at": "2026-07-08T12:00:00+00:00",
    }
    rec = EA.build_exit_autopsy_from_fill(
        fill, None, None, forced=False, reason=None,
    )
    assert rec["forced"] is False
    assert rec["forced_reason"] is None
    assert rec["realized_pnl_usd"] == 50.0  # (110-100)*5
    assert rec["days_held"] == 7
    assert rec["status"] == "system_stub"
    assert rec["needs_brain_grade"] is True


def test_build_fail_soft_on_garbage(autopsy_root):
    rec = EA.build_exit_autopsy_from_fill(
        None, None, None, forced=False, reason=None,  # type: ignore[arg-type]
    )
    assert rec["status"] == "system_stub"
    assert rec["needs_brain_grade"] is True
    assert rec["ticker"] == ""


def test_append_and_recent_newest_first(autopsy_root):
    r1 = EA.build_exit_autopsy_from_fill(
        {"ticker": "AAA", "action": "SELL_TO_CLOSE", "fill_price": 10,
         "quantity": 1, "realized_pnl_usd": 1.0},
        None, {"days_held": 2}, forced=False, reason=None,
    )
    r1["ts"] = "2026-07-10T10:00:00+00:00"
    path1 = EA.append_exit_autopsy(r1)
    assert path1.name == "2026-07-10.jsonl"
    assert path1.exists()

    r2 = EA.build_exit_autopsy_from_fill(
        {"ticker": "BBB", "action": "SELL_TO_CLOSE", "fill_price": 20,
         "quantity": 2, "realized_pnl_usd": -3.0},
        None, {"days_held": 5}, forced=True, reason="horizon_expired",
    )
    r2["ts"] = "2026-07-11T12:00:00+00:00"
    EA.append_exit_autopsy(r2)

    # Same day, later line should sort first within day.
    r3 = dict(r1)
    r3["ticker"] = "CCC"
    r3["ts"] = "2026-07-11T18:00:00+00:00"
    EA.append_exit_autopsy(r3)

    recent = EA.recent_exit_autopsies(limit=10)
    tickers = [r["ticker"] for r in recent]
    # Newest day first; within 07-11, last-written (CCC) before BBB.
    assert tickers[0] == "CCC"
    assert tickers[1] == "BBB"
    assert "AAA" in tickers
    assert recent[0]["ts"] == "2026-07-11T18:00:00+00:00"


def test_recent_respects_limit(autopsy_root):
    for i in range(5):
        EA.append_exit_autopsy({
            "ts": f"2026-07-12T0{i}:00:00+00:00",
            "ticker": f"T{i}",
            "status": "system_stub",
            "needs_brain_grade": True,
        })
    assert len(EA.recent_exit_autopsies(limit=3)) == 3
    assert EA.recent_exit_autopsies(limit=0) == []


def test_brain_facing_exit_lessons(autopsy_root):
    EA.append_exit_autopsy({
        "ts": "2026-07-13T10:00:00+00:00",
        "ticker": "DELL",
        "action": "SELL_TO_CLOSE",
        "fill_price": 140.0,
        "quantity": 3.0,
        "days_held": 12,
        "realized_pnl_usd": 15.0,
        "forced": False,
        "forced_reason": None,
        "status": "system_stub",
        "needs_brain_grade": True,
        "entry_plan": {"stop_loss": 120.0},
    })
    out = EA.brain_facing_exit_lessons(limit=5)
    assert "note" in out
    assert isinstance(out["recent"], list)
    assert out["ungraded_count"] >= 1
    assert out["recent"][0]["ticker"] == "DELL"
    assert out["recent"][0]["needs_brain_grade"] is True


def test_append_never_raises(autopsy_root, monkeypatch):
    # Point AUTOPSY_DIR at a file (not a dir) so mkdir/open can fail paths.
    bad = autopsy_root / "not_a_dir"
    bad.write_text("x")
    monkeypatch.setattr(EA, "AUTOPSY_DIR", bad / "nested")
    path = EA.append_exit_autopsy({"ticker": "X"})
    assert isinstance(path, Path)


def test_recent_empty_when_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(EA, "AUTOPSY_DIR", tmp_path / "nope")
    assert EA.recent_exit_autopsies() == []
    out = EA.brain_facing_exit_lessons()
    assert out["recent"] == []
    assert out["ungraded_count"] == 0


# ---------------------------------------------------------------------------
# Script mode
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    fails = 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        journal = root / "journal" / "exit_autopsies"
        journal.mkdir(parents=True)
        EA.ROOT = root
        EA.AUTOPSY_DIR = journal

        rec = EA.build_exit_autopsy_from_fill(
            {"ticker": "ZZ", "action": "SELL_TO_CLOSE", "fill_price": 1,
             "quantity": 1, "avg_cost": 2, "realized_pnl_usd": -1},
            {"forced_exit_reason": "stop_loss_breached"},
            {"days_held": 3}, forced=True, reason="stop_loss_breached",
        )
        ok = rec["status"] == "system_stub" and rec["forced"] is True
        print(f"  [{'PASS' if ok else 'FAIL'}] build stub")
        fails += 0 if ok else 1

        EA.append_exit_autopsy({**rec, "ts": "2026-07-14T12:00:00+00:00"})
        recent = EA.recent_exit_autopsies(5)
        ok = len(recent) == 1 and recent[0]["ticker"] == "ZZ"
        print(f"  [{'PASS' if ok else 'FAIL'}] append + recent")
        fails += 0 if ok else 1

        lessons = EA.brain_facing_exit_lessons(5)
        ok = lessons["ungraded_count"] == 1 and lessons["recent"][0]["ticker"] == "ZZ"
        print(f"  [{'PASS' if ok else 'FAIL'}] brain_facing_exit_lessons")
        fails += 0 if ok else 1

    sys.exit(1 if fails else 0)
