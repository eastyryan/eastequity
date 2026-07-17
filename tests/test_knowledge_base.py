"""Offline unit tests for tools.knowledge_base (store round-trip, dedupe,
rotation, prune, brain-facing shape). All file writes are redirected into a
tmp_path — the real data/knowledge_base.json is never touched.

    python3 -m pytest tests/test_knowledge_base.py -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.knowledge_base as kb  # noqa: E402


@pytest.fixture(autouse=True)
def _sandbox_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "KB_JSON", tmp_path / "knowledge_base.json")
    monkeypatch.setattr(kb, "KB_MD", tmp_path / "knowledge_base.md")
    monkeypatch.setattr(kb, "JOURNAL_JSON", tmp_path / "learning_journal.json")


def _lesson(topic="Anchored VWAP for swing entries", discipline="technical_analysis"):
    return {
        "discipline": discipline,
        "topic": topic,
        "summary": "s" * 120,
        "key_points": ["one", "two", "three"],
        "how_to_apply": "h" * 80,
        "sources": ["https://example.com/a", "https://example.com/b"],
    }


def test_add_and_read_back():
    res = kb.add_lesson(_lesson(), "run-1")
    assert res["status"] == "ok" and res["id"].startswith("KB-")
    bf = kb.brain_facing_knowledge_base()
    assert bf["n_active"] == 1
    assert bf["recent"][0]["topic"] == "Anchored VWAP for swing entries"
    assert bf["discipline_counts"]["technical_analysis"] == 1
    # dual-file + journal all written
    assert kb.KB_JSON.exists() and kb.KB_MD.exists() and kb.JOURNAL_JSON.exists()
    journal = json.loads(kb.JOURNAL_JSON.read_text())
    assert len(journal["entries"]) == 1


def test_duplicate_topic_rejected():
    kb.add_lesson(_lesson(), "run-1")
    res = kb.add_lesson(_lesson(), "run-2")
    assert res["status"] == "duplicate"
    assert kb.brain_facing_knowledge_base()["n_active"] == 1


def test_thin_lesson_rejected():
    res = kb.add_lesson({"topic": "x", "summary": "short", "how_to_apply": "short"})
    assert res["status"] == "invalid"


def test_unknown_discipline_falls_back():
    res = kb.add_lesson(_lesson(discipline="astrology"), "run-1")
    assert res["status"] == "ok"
    assert res["discipline"] == "strategy_playbooks"


def test_next_discipline_rotation_and_hints():
    # empty KB -> first discipline in curriculum order
    assert kb.next_discipline() == "technical_analysis"
    kb.add_lesson(_lesson(), "run-1")  # covers technical_analysis
    assert kb.next_discipline() == "fundamental_analysis"  # least covered next
    # hints override rotation, least-covered among hints wins
    assert kb.next_discipline(["risk_management"]) == "risk_management"
    # unknown hints are ignored
    assert kb.next_discipline(["not_a_discipline"]) == "fundamental_analysis"


def test_prune_caps_active():
    for i in range(5):
        kb.add_lesson(_lesson(topic=f"unique topic number {i} " + "x" * 20), f"run-{i}")
    out = kb.prune_knowledge_base(max_active=2)
    assert out["pruned"] == 3
    bf = kb.brain_facing_knowledge_base()
    assert bf["n_active"] == 2
    # journal only publishes active lessons
    journal = json.loads(kb.JOURNAL_JSON.read_text())
    assert len(journal["entries"]) == 2


if __name__ == "__main__":
    print("run under pytest: python3 -m pytest tests/test_knowledge_base.py -q")
