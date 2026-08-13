"""Desk phase carve-out + one-landing-per-slot guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from runlib.brain_io import _desk_phase_instructions

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "slot_already_landed", _ROOT / "scripts" / "slot_already_landed.py")
sal = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sal)


def test_desk_is_anecdote_under_binding_n(monkeypatch):
    monkeypatch.setattr("runlib.analytics.compute_closed_trades",
                        lambda: [{}, {}])
    text = _desk_phase_instructions()
    assert "PHASE: anecdote" in text
    assert "merely adequate, that is a veto" not in text
    assert "APPROVE with a haircut" in text
    assert "Fabrication is still a veto" in text


def test_desk_is_strict_once_sample_binds(monkeypatch):
    monkeypatch.setattr("runlib.analytics.compute_closed_trades",
                        lambda: [{}] * 15)
    text = _desk_phase_instructions()
    assert "PHASE: anecdote" not in text
    assert "If the case is merely adequate, that is a veto." in text


def test_watchdog_is_never_already_landed():
    assert sal.already_landed("watchdog") is False
    assert sal.already_landed("auto") is False


def test_study_landed_detects_today_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sal, "ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    monkeypatch.setattr("runlib.core.et_date", lambda: "2026-08-12")
    assert sal.study_landed_today() is False
    (tmp_path / "state" / "study_20260812-deadbe.json").write_text("{}")
    assert sal.study_landed_today() is True


def test_clock_slot_hit_uses_slot_report(monkeypatch):
    monkeypatch.setattr(sal, "clock_slot_hit", lambda: True)
    assert sal.already_landed("brain") is True
    monkeypatch.setattr(sal, "clock_slot_hit", lambda: False)
    assert sal.already_landed("brain") is False
