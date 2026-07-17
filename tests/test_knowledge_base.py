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


# ---------------------------------------------------------------------------
# Evidence lifecycle: citations -> trade links -> outcomes -> status; plus the
# contradiction (apply_conflicts) and Friday consolidation guardrails.
# ---------------------------------------------------------------------------

def _add(topic, discipline="technical_analysis"):
    return kb.add_lesson(_lesson(topic=topic, discipline=discipline), "run-x")["id"]


def _closed(ticker, opened_at, pnl, closed_at="2026-07-30"):
    return {"ticker": ticker, "opened_at": opened_at, "closed_at": closed_at,
            "pnl_usd": pnl, "verdict": "Thesis exit"}


def test_record_citations_counts_only_active_ids():
    lid = _add("topic one plus padding padding")
    n = kb.record_citations(f"per {lid} I waited for the retest; also KB-0000000000")
    assert n == 1
    e = kb.active_lessons()[0]
    assert e["times_cited"] == 1 and e["last_cited"]
    assert kb.record_citations("no ids here") == 0


def test_link_and_outcome_grading_thresholds():
    lid = _add("stops and noise band lesson topic")
    for i, (pnl, day) in enumerate([(50, "2026-07-01"), (30, "2026-07-05"),
                                    (-20, "2026-07-10")]):
        assert kb.link_lessons_to_trade("NVDA", [lid], f"r{i}", linked_on=day) == 1
    closes = [_closed("NVDA", "2026-07-01", 50), _closed("NVDA", "2026-07-05", 30),
              _closed("NVDA", "2026-07-10", -20)]
    out = kb.update_lesson_outcomes(closes)
    assert out["graded"] == 3
    e = kb.active_lessons()[0]
    assert e["outcome"] == {"n": 3, "wins": 2, "graded_at": e["outcome"]["graded_at"]}
    assert e["evidence_status"] == "validated"  # 2/3 >= 60%


def test_outcomes_below_min_trades_stay_anecdote():
    lid = _add("small sample stays anecdote topic")
    kb.link_lessons_to_trade("MU", [lid], "r1", linked_on="2026-07-01")
    kb.update_lesson_outcomes([_closed("MU", "2026-07-02", -10)])
    e = kb.active_lessons()[0]
    assert e["outcome"]["n"] == 1
    assert e.get("evidence_status") is None  # anecdote: no judgment


def test_underperforming_status_and_selection_order():
    bad = _add("bad lesson that keeps losing topic")
    for i, day in enumerate(["2026-07-01", "2026-07-03", "2026-07-05"]):
        kb.link_lessons_to_trade("DELL", [bad], f"r{i}", linked_on=day)
    kb.update_lesson_outcomes([_closed("DELL", d, -10) for d in
                               ("2026-07-01", "2026-07-03", "2026-07-05")])
    _add("newer neutral lesson topic here", discipline="macro_regimes")
    bf = kb.brain_facing_knowledge_base()
    rows = bf["recent"]
    # underperforming ranks LAST despite any recency, and carries a warning
    assert rows[-1]["id"] == bad and rows[-1]["evidence_status"] == "underperforming"
    assert "warning" in rows[-1]
    assert rows[0]["id"] != bad


def test_apply_conflicts_guardrails():
    old = _add("old contradicted lesson topic here")
    val = _add("validated lesson cannot be superseded")
    # validate `val` with 3 winning linked trades
    for i, d in enumerate(["2026-07-01", "2026-07-02", "2026-07-03"]):
        kb.link_lessons_to_trade("AMD", [val], f"r{i}", linked_on=d)
    kb.update_lesson_outcomes([_closed("AMD", d, 25) for d in
                               ("2026-07-01", "2026-07-02", "2026-07-03")])
    new = _add("new stronger evidence lesson topic")
    res = kb.apply_conflicts(
        [{"id": old, "resolution": "supersede", "why": "w" * 50},
         {"id": val, "resolution": "supersede", "why": "w" * 50},
         {"id": old, "resolution": "supersede", "why": "thin"}],
        new, "run-c")
    assert res["superseded"] == [old]
    why = {r["id"]: r["why"] for r in res["rejected"]}
    assert why[val] == "target_validated_by_evidence"
    active_ids = {e["id"] for e in kb.active_lessons()}
    assert old not in active_ids and val in active_ids


def test_apply_conflicts_supersede_budget():
    olds = [_add(f"budget lesson number {i} padding") for i in range(3)]
    new = _add("the lesson that supersedes many")
    res = kb.apply_conflicts(
        [{"id": o, "resolution": "supersede", "why": "w" * 50} for o in olds],
        new, "run-c")
    assert len(res["superseded"]) == kb.MAX_SUPERSEDES_PER_STUDY
    assert any(r["why"] == "supersede_budget_exhausted" for r in res["rejected"])


def test_apply_conflicts_coexist_records_tension():
    a = _add("regime A lesson topic padding here")
    b = _add("regime B lesson topic padding here")
    res = kb.apply_conflicts([{"id": a, "resolution": "coexist",
                               "why": "A in trends, B in chop"}], b, "run-c")
    assert res["coexist"] == [a]
    newer = [e for e in kb.active_lessons() if e["id"] == b][0]
    assert newer["tensions"][0]["with"] == a


def _age(lesson_id, iso_date):
    """Backdate a lesson so consolidation age-guards can be exercised."""
    import json as _json
    doc = _json.loads(kb.KB_JSON.read_text())
    for e in doc["entries"]:
        if e["id"] == lesson_id:
            e["learned_at"] = iso_date + "T00:00:00+00:00"
    kb.KB_JSON.write_text(_json.dumps(doc))


def test_consolidation_merge_and_retire_with_guardrails():
    a = _add("merge member one topic padding")
    b = _add("merge member two topic padding")
    fresh = _add("fresh lesson cannot be retired yet")
    stale = _add("stale weak lesson retire me please")
    for lid in (a, b, stale):
        _age(lid, "2026-01-01")
    plan = {
        "merges": [{"ids": [a, b],
                    "lesson": _lesson(topic="merged principle of one and two")}],
        "retires": [{"id": stale, "why": "w" * 50},
                    {"id": fresh, "why": "w" * 50},
                    {"id": "KB-0000000000", "why": "w" * 50}],
    }
    res = kb.apply_consolidation(plan, "run-f")
    assert len(res["merged"]) == 1 and res["merged"][0]["from"] == [a, b]
    assert res["retired"] == [stale]
    why = {str(r.get("id")): r["why"] for r in res["rejected"]}
    assert why[fresh] == "too_fresh"
    assert why["KB-0000000000"] == "unknown_id"
    active_ids = {e["id"] for e in kb.active_lessons()}
    assert a not in active_ids and b not in active_ids and stale not in active_ids
    merged = [e for e in kb.active_lessons()
              if e["topic"] == "merged principle of one and two"][0]
    assert merged["merged_from"] == [a, b]


def test_consolidation_action_budget():
    ids = [_add(f"budgeted retire lesson {i} padding") for i in range(7)]
    for lid in ids:
        _age(lid, "2026-01-01")
    plan = {"retires": [{"id": i, "why": "w" * 50} for i in ids]}
    res = kb.apply_consolidation(plan, "run-f")
    assert len(res["retired"]) == kb.MAX_CONSOLIDATION_ACTIONS
    assert any(r["why"] == "action_budget_exhausted" for r in res["rejected"])


if __name__ == "__main__":
    print("run under pytest: python3 -m pytest tests/test_knowledge_base.py -q")
