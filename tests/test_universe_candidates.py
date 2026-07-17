"""Offline unit tests for runlib.reviews.apply_universe_candidates — the
deterministic gate for brain-proposed dynamic universe additions. Filesystem
writes are sandboxed into tmp_path; the market-data gates (unpriceable /
below_min_cap) and journaling are monkeypatched so nothing hits the network.

    python3 -m pytest tests/test_universe_candidates.py -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlib.reviews as reviews  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Fake ROOT with a small universe file + no-network gates."""
    (tmp_path / "data").mkdir()
    (tmp_path / "dashboard" / "data").mkdir(parents=True)
    universe = {"description": "test", "last_reviewed": "2026-07-14",
                "sectors": {"gpu_accelerators": ["NVDA", "AMD"],
                            "healthcare": ["UNH"]}}
    (tmp_path / "data" / "universe.json").write_text(json.dumps(universe))

    monkeypatch.setattr(reviews, "ROOT", tmp_path)
    monkeypatch.setattr(reviews, "unpriceable", lambda ts: set())
    monkeypatch.setattr(reviews, "below_min_cap", lambda ts, floor: set())
    logged = []
    monkeypatch.setattr(reviews.journal, "log_improvement",
                        lambda note, run_id=None: logged.append(note))
    return {"root": tmp_path, "logged": logged}


def _universe(root: Path) -> dict:
    return json.loads((root / "data" / "universe.json").read_text())


def test_accepts_valid_candidate_into_existing_sector(sandbox):
    out = reviews.apply_universe_candidates(
        [{"ticker": "lly", "sector": "healthcare", "reason": "GLP-1 momentum"}],
        "run-1")
    assert [a["ticker"] for a in out["accepted"]] == ["LLY"]
    assert out["accepted"][0]["sector"] == "healthcare"
    u = _universe(sandbox["root"])
    assert "LLY" in u["sectors"]["healthcare"]
    log = json.loads((sandbox["root"] / "dashboard" / "data"
                      / "universe_log.json").read_text())
    assert log[-1]["status"] == "dynamic_add" and log[-1]["added"] == ["LLY"]
    assert sandbox["logged"]  # journaled


def test_unknown_sector_falls_back_to_dynamic_additions(sandbox):
    out = reviews.apply_universe_candidates(
        [{"ticker": "CAT", "sector": "not_a_sector", "reason": "infra capex"}],
        "run-1")
    assert out["accepted"][0]["sector"] == reviews.DYNAMIC_SECTOR_FALLBACK
    u = _universe(sandbox["root"])
    assert "CAT" in u["sectors"][reviews.DYNAMIC_SECTOR_FALLBACK]


def test_rejections_by_gate(sandbox):
    out = reviews.apply_universe_candidates(
        [
            {"ticker": "NVDA", "sector": "gpu_accelerators", "reason": "dup"},
            {"ticker": "brk.b", "sector": "financials", "reason": "format"},
            {"ticker": "SOXL", "sector": "gpu_accelerators", "reason": "levered"},
            {"ticker": "CAT", "sector": "industrials", "reason": ""},
        ],
        "run-1")
    why = {r["ticker"]: r["why"] for r in out["rejected"]}
    assert why["NVDA"] == "already_in_universe"
    assert why["BRK.B"] == "invalid_format"
    assert why["SOXL"] == "forbidden_product"
    assert why["CAT"] == "no_reason_given"
    assert out["accepted"] == []
    # nothing written when nothing accepted
    assert not (sandbox["root"] / "dashboard" / "data" / "universe_log.json").exists()


def test_per_run_cap(sandbox):
    cands = [{"ticker": t, "sector": "healthcare", "reason": "r"}
             for t in ("LLY", "ABBV", "TMO", "ISRG", "VRTX")]
    out = reviews.apply_universe_candidates(cands, "run-1")
    assert len(out["accepted"]) == reviews.MAX_DYNAMIC_ADDS_PER_RUN
    capped = [r for r in out["rejected"] if r["why"] == "per_run_cap"]
    assert len(capped) == len(cands) - reviews.MAX_DYNAMIC_ADDS_PER_RUN


def test_market_data_gates_apply(sandbox, monkeypatch):
    monkeypatch.setattr(reviews, "unpriceable", lambda ts: {"DEAD"})
    monkeypatch.setattr(reviews, "below_min_cap", lambda ts, floor: {"TINY"})
    out = reviews.apply_universe_candidates(
        [{"ticker": "DEAD", "sector": "healthcare", "reason": "r"},
         {"ticker": "TINY", "sector": "healthcare", "reason": "r"},
         {"ticker": "LLY", "sector": "healthcare", "reason": "r"}],
        "run-1")
    why = {r["ticker"]: r["why"] for r in out["rejected"]}
    assert why["DEAD"] == "unpriceable"
    assert why["TINY"] == "below_min_market_cap"
    assert [a["ticker"] for a in out["accepted"]] == ["LLY"]


def test_duplicate_within_batch(sandbox):
    out = reviews.apply_universe_candidates(
        [{"ticker": "LLY", "sector": "healthcare", "reason": "r"},
         {"ticker": "LLY", "sector": "healthcare", "reason": "again"}],
        "run-1")
    assert [a["ticker"] for a in out["accepted"]] == ["LLY"]
    assert out["rejected"][0]["why"] == "already_in_universe"


def test_recent_dynamic_adds_protection_window(sandbox):
    reviews.apply_universe_candidates(
        [{"ticker": "LLY", "sector": "healthcare", "reason": "r"}], "run-1")
    assert "LLY" in reviews.recent_dynamic_adds()
    # entries older than the window do not protect
    log_file = sandbox["root"] / "dashboard" / "data" / "universe_log.json"
    log = json.loads(log_file.read_text())
    log[-1]["date"] = "2020-01-01"
    log_file.write_text(json.dumps(log))
    assert "LLY" not in reviews.recent_dynamic_adds()


def test_parse_proposals_passes_universe_candidates_through():
    from runlib.brain_io import parse_proposals
    resp = ('```json\n{"proposals": [], "no_trade_reason": "none", '
            '"universe_candidates": [{"ticker": "LLY", "sector": "healthcare", '
            '"reason": "r"}, "not-a-dict"]}\n```')
    parsed = parse_proposals(resp)
    assert parsed["universe_candidates"] == [
        {"ticker": "LLY", "sector": "healthcare", "reason": "r"}]
    # absent field and unparsable responses degrade to []
    assert parse_proposals('```json\n{"proposals": []}\n```')["universe_candidates"] == []
    assert parse_proposals("no json here")["universe_candidates"] == []


def test_garbage_input_never_raises(sandbox):
    assert reviews.apply_universe_candidates(None, "r")["accepted"] == []
    assert reviews.apply_universe_candidates(["str", 42], "r")["accepted"] == []
    assert reviews.apply_universe_candidates([{}], "r")["accepted"] == []


if __name__ == "__main__":
    print("run under pytest: python3 -m pytest tests/test_universe_candidates.py -q")
