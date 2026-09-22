"""Offline tests for ATR map persistence / backfill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import atr_map


def test_persist_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    n = atr_map.persist_atr_map({"amd": 2.5, "NVDA": 3.1}, source="test")
    assert n == 2
    loaded = atr_map.load_atr_map()
    assert loaded == {"AMD": 2.5, "NVDA": 3.1}


def test_ensure_backfills_empty_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    atr_map.persist_atr_map({"DELL": 4.0})
    scan = {"status": "light", "atr_by_ticker": {}}
    out = atr_map.ensure_atr_on_scan(scan)
    assert out["DELL"] == 4.0
    assert scan["atr_by_ticker"]["DELL"] == 4.0
    assert "backfilled" in scan.get("atr_map_note", "")


def test_ensure_persists_nonempty_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    scan = {"status": "ok", "atr_by_ticker": {"AAPL": 1.2}}
    atr_map.ensure_atr_on_scan(scan)
    blob = json.loads((tmp_path / "atr_map.json").read_text())
    assert blob["atr_by_ticker"]["AAPL"] == 1.2
