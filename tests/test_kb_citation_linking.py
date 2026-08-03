"""The knowledge base's grading loop had no INPUT, and these tests pin the join
that was missing.

MEASURED 2026-08-03 against the real stores. The loop was NOT write-only, which
is what it looked like from the outside: `record_citations` had been working the
whole time (64 citations across the 11 lessons, 1-14 each, `last_cited` set on
10 of 11). What never happened once was a LINK — `linked_trades` was empty on
every lesson, so `update_lesson_outcomes` had nothing to grade and no lesson has
ever carried an `evidence_status`.

The cause is that the two halves of the loop read different text.
`record_citations` scans the whole brain response; the trade link scanned only
`json.dumps(proposal)`. Not one of the 19 proposals in the entire journal
contains a KB- id — every citation landed in prose (`latest_reasoning`,
`watchlist[].thoughts`, `no_trade_reason`, `rejected_ideas[].reason`). A second,
compounding fact: only 3 BUYs ever filled (DELL 07-09, HPE 07-10, BKR 07-28) and
only BKR came after the first lesson existed (07-17), so the link had at most one
chance to fire and the proposal it needed carried no id.

    ./.venv311/bin/python -m pytest tests/test_kb_citation_linking.py -q
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.knowledge_base as kb  # noqa: E402


@pytest.fixture(autouse=True)
def _sandbox_paths(tmp_path, monkeypatch):
    """Every path tools.knowledge_base writes to. The real
    data/knowledge_base.json is a GUARDED learning store (tests/conftest.py fails
    the build on a write) — and a synthetic citation there would be
    indistinguishable from a real one, which is the whole reason the guard
    exists."""
    monkeypatch.setattr(kb, "KB_JSON", tmp_path / "knowledge_base.json")
    monkeypatch.setattr(kb, "KB_MD", tmp_path / "knowledge_base.md")
    monkeypatch.setattr(kb, "JOURNAL_JSON", tmp_path / "learning_journal.json")


def _lesson(topic, discipline="technical_analysis"):
    return {"discipline": discipline, "topic": topic, "summary": "s" * 120,
            "key_points": ["one", "two"], "how_to_apply": "h" * 80,
            "sources": ["https://example.com/a"]}


def _add(topic, discipline="technical_analysis", run_id="20260731-098397"):
    return kb.add_lesson(_lesson(topic, discipline), run_id)["id"]


def _closed(ticker, opened_at, pnl, closed_at="2026-07-30"):
    return {"ticker": ticker, "opened_at": opened_at, "closed_at": closed_at,
            "pnl_usd": pnl, "verdict": "Thesis exit"}


def _by_id(lid):
    return next(e for e in kb._load()["entries"] if e["id"] == lid)


# ---------------------------------------------------------------------------
# extraction: the proposal's own fields
# ---------------------------------------------------------------------------

def test_citation_extracted_from_every_proposal_text_field():
    prop = {
        "ticker": "BKR",
        "thesis": "per KB-1111111111, waiting for the light-volume retest",
        "catalysts": ["Q3 print, KB-2222222222 applies to the geometry"],
        "thesis_invalidators": {"invalidating_structure": "close below 50-DMA "
                                "(KB-3333333333)"},
    }
    assert kb.citations_in_proposal(prop) == [
        "KB-1111111111", "KB-2222222222", "KB-3333333333"]


def test_ids_outside_the_named_reasoning_fields_are_not_citations():
    """A KB- id in an unrelated key is not the brain citing a lesson."""
    assert kb.citations_in_proposal(
        {"ticker": "BKR", "sizing_note": "clamped, ref KB-9999999999"}) == []
    assert kb.citations_in_proposal(None) == []
    assert kb.citations_in_proposal("not a dict") == []


# ---------------------------------------------------------------------------
# extraction: run prose, scoped to the ticker (the actual fix)
# ---------------------------------------------------------------------------

def test_prose_citation_links_only_within_the_ticker_s_own_paragraph():
    """THE REGRESSION. The brain cites in prose, never inside the proposal, so
    the link has to read prose — but only the part of it about this trade."""
    response = (
        "Regime is supportive.\n\n"
        "BKR: per KB-1111111111, the catalyst-in-a-downtrend setup applies "
        "here and the geometry clears 2:1.\n\n"
        "I rejected MPC — per KB-2222222222 the base is too shallow.\n"
    )
    assert kb.citations_near_ticker(response, "BKR") == ["KB-1111111111"]
    assert kb.citations_near_ticker(response, "MPC") == ["KB-2222222222"]


def test_a_lesson_cited_while_rejecting_a_name_never_links_to_a_different_trade():
    """CLAUDE.md: 'citing a lesson that did not actually drive the decision
    corrupts your own grading loop.' A fabricated link is worse than a missing
    one — the win rate it feeds looks identical to a real one."""
    response = ("I passed on MPC; per KB-2222222222 the base is too shallow.\n\n"
                "Bought BKR on its own merits, no lesson drove it.\n")
    assert kb.citations_near_ticker(response, "BKR") == []


def test_ticker_match_is_word_bounded_and_cashtag_aware():
    assert kb.citations_near_ticker("$BKR ripped, per KB-1111111111", "BKR") == \
        ["KB-1111111111"]
    # BKRX is a different company; substring matching would link the wrong trade
    assert kb.citations_near_ticker("BKRX moved, per KB-1111111111", "BKR") == []


def test_citations_for_trade_unions_proposal_and_prose():
    prop = {"ticker": "BKR", "thesis": "geometry per KB-1111111111"}
    response = "BKR also fits KB-2222222222, the pullback-continuation entry."
    assert kb.citations_for_trade(prop, response) == [
        "KB-1111111111", "KB-2222222222"]
    # no response text -> proposal-only, i.e. the old behaviour, still works
    assert kb.citations_for_trade(prop) == ["KB-1111111111"]


# ---------------------------------------------------------------------------
# the link, end to end: prose citation -> executed BUY -> closed trade -> status
# ---------------------------------------------------------------------------

def test_prose_citation_grades_a_lesson_end_to_end():
    """The whole loop the store has never once completed."""
    lid = _add("catalyst in a downtrend setup topic")
    for i, day in enumerate(["2026-07-01", "2026-07-05", "2026-07-10"]):
        response = f"NVDA: per {lid}, I waited for the retest before adding."
        ids = kb.citations_for_trade({"ticker": "NVDA"}, response)
        assert ids == [lid]
        assert kb.link_lessons_to_trade("NVDA", ids, f"run-{i}", linked_on=day,
                                        trade_ref=f"SIM-{i}") == 1

    e = _by_id(lid)
    assert len(e["linked_trades"]) == 3
    assert e["linked_trades"][0]["trade_ref"] == "SIM-0"

    out = kb.update_lesson_outcomes(
        [_closed("NVDA", "2026-07-01", 50), _closed("NVDA", "2026-07-05", 30),
         _closed("NVDA", "2026-07-10", -20)])
    assert out["graded"] == 3
    e = _by_id(lid)
    assert e["outcome"]["n"] == 3 and e["outcome"]["wins"] == 2
    assert e["evidence_status"] == "validated"  # 2/3 >= 60%


def test_status_absent_under_three_linked_trades():
    lid = _add("small sample stays an anecdote topic")
    kb.link_lessons_to_trade("MU", [lid], "r1", linked_on="2026-07-01")
    kb.update_lesson_outcomes([_closed("MU", "2026-07-02", -10)])
    e = _by_id(lid)
    assert e["outcome"]["n"] == 1
    assert e.get("evidence_status") is None


def test_underperforming_is_reached_at_the_documented_threshold():
    lid = _add("a lesson the evidence kills topic")
    days = ["2026-07-01", "2026-07-03", "2026-07-05"]
    for i, d in enumerate(days):
        kb.link_lessons_to_trade("DELL", [lid], f"r{i}", linked_on=d)
    kb.update_lesson_outcomes([_closed("DELL", d, -10) for d in days])
    assert _by_id(lid)["evidence_status"] == "underperforming"


# ---------------------------------------------------------------------------
# duplicate links
# ---------------------------------------------------------------------------

def test_relinking_the_same_fill_is_idempotent():
    """A manual re-run of the same run_id must not manufacture evidence."""
    lid = _add("idempotent linking topic here")
    assert kb.link_lessons_to_trade("BKR", [lid], "run-1", linked_on="2026-07-28",
                                    trade_ref="SIM-a") == 1
    assert kb.link_lessons_to_trade("BKR", [lid], "run-1", linked_on="2026-07-28",
                                    trade_ref="SIM-a") == 0
    assert len(_by_id(lid)["linked_trades"]) == 1


def test_scale_in_adds_are_distinct_links_not_duplicates():
    """The validator permits up to 2 adds per position: separate fills on the
    same ticker in the same run are genuinely separate links, told apart by
    trade_ref."""
    lid = _add("scale in linking topic here")
    assert kb.link_lessons_to_trade("BKR", [lid], "run-1", trade_ref="SIM-a") == 1
    assert kb.link_lessons_to_trade("BKR", [lid], "run-1", trade_ref="SIM-b") == 1
    assert len(_by_id(lid)["linked_trades"]) == 2


# ---------------------------------------------------------------------------
# grading survives supersession
# ---------------------------------------------------------------------------

def test_a_superseded_lesson_still_gets_its_outcome_graded():
    """Swing horizons run 3-90 days and consolidation runs weekly, so a lesson
    can be retired between its trade opening and closing. Grading only active
    lessons froze that evidence forever — including the evidence that would have
    justified (or overturned) the retirement."""
    lid = _add("lesson retired mid trade topic")
    days = ["2026-07-01", "2026-07-03", "2026-07-05"]
    for i, d in enumerate(days):
        kb.link_lessons_to_trade("AMD", [lid], f"r{i}", linked_on=d)

    doc = kb._load()
    doc["entries"][0]["retired"] = True
    kb._save(doc)

    out = kb.update_lesson_outcomes([_closed("AMD", d, 25) for d in days])
    assert out["graded"] == 3
    assert _by_id(lid)["evidence_status"] == "validated"


def test_link_is_refused_once_a_lesson_is_retired():
    """Grading a retired lesson's OPEN trades is right; opening NEW ones on it
    is not — it is out of rotation and did not drive that decision."""
    lid = _add("retired lesson takes no new links")
    doc = kb._load()
    doc["entries"][0]["retired"] = True
    kb._save(doc)
    assert kb.link_lessons_to_trade("AMD", [lid], "r1") == 0


# ---------------------------------------------------------------------------
# learned_at
# ---------------------------------------------------------------------------

def test_new_lessons_are_stamped():
    lid = _add("freshly studied lesson topic here")
    assert _by_id(lid)["learned_at"]


def test_learned_at_backfills_from_the_run_id_date():
    lid = _add("lesson missing its stamp topic", run_id="20260731-098397")
    doc = kb._load()
    doc["entries"][0]["learned_at"] = None
    kb._save(doc)

    out = kb.backfill_learned_at()
    assert out["filled"] == 1 and out["unresolvable"] == []
    assert _by_id(lid)["learned_at"].startswith("2026-07-31")


def test_backfill_never_invents_a_date_it_cannot_derive():
    """An honestly missing date stays missing. Substituting _now() would date a
    July lesson to whenever maintenance ran and make a stale curriculum look
    freshly studied — the exact failure health.learning_loop exists to catch."""
    lid = _add("lesson with no usable run id", run_id="manual-session")
    doc = kb._load()
    doc["entries"][0]["learned_at"] = None
    kb._save(doc)

    out = kb.backfill_learned_at()
    assert out["filled"] == 0 and out["unresolvable"] == [lid]
    assert _by_id(lid).get("learned_at") is None


def test_backfill_is_idempotent_and_leaves_stamped_lessons_alone():
    lid = _add("already stamped lesson topic here")
    before = _by_id(lid)["learned_at"]
    assert kb.backfill_learned_at()["filled"] == 0
    assert _by_id(lid)["learned_at"] == before


@pytest.mark.parametrize("run_id,expect", [
    ("20260731-098397", "2026-07-31"),
    ("20260709-31a10e", "2026-07-09"),
    ("2026073-098397", None),      # short date
    ("20261331-098397", None),     # month 13
    ("", None),
    (None, None),
])
def test_run_id_date_parsing(run_id, expect):
    got = kb._learned_at_from_run_id(run_id)
    assert (got[:10] if got else None) == expect


def test_recovered_journal_row_with_null_learned_at_is_repaired():
    """setdefault does not replace an explicit null, and the journal carries
    `learned_at: null` on rows written before add_lesson stamped it."""
    row = kb._normalize_published(
        {"id": "KB-1111111111", "learned_at": None, "run_id": "20260717-ed4649"})
    assert row["learned_at"].startswith("2026-07-17")


def test_a_lesson_with_no_derivable_date_is_untouchable_by_consolidation():
    """_date_gap_days returns 999 for a missing date and 999 < 7 is False, so an
    undated lesson read as ancient and became instantly retirable — the guard
    inverted on exactly the rows most likely to lack a date."""
    lid = _add("undated lesson protected from retirement")
    doc = kb._load()
    doc["entries"][0]["learned_at"] = None
    kb._save(doc)

    res = kb.apply_consolidation({"retires": [
        {"id": lid, "why": "x" * 60}]}, "run-1")
    assert res["retired"] == []
    assert res["rejected"][0]["why"] == "too_fresh"
    assert not _by_id(lid).get("retired")


def test_journal_reflects_the_backfill():
    lid = _add("published lesson gets its date topic", run_id="20260731-098397")
    doc = kb._load()
    doc["entries"][0]["learned_at"] = None
    kb._save(doc)
    kb.backfill_learned_at()
    published = json.loads(kb.JOURNAL_JSON.read_text())["entries"]
    assert next(e for e in published if e["id"] == lid)["learned_at"].startswith(
        "2026-07-31")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
