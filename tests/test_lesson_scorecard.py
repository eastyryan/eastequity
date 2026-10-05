"""Lesson scorecard — the learning loop's measuring and acting half.

Pins: conservative verdicts (no action under MIN_GRADED, Wilson-bounded edge),
the outcome join (skips vs shadow verdicts, buys vs closed-trade R, market-wide
verdicts excluded, re-watched names deduped), the capped/protected action plan,
apply-mode writes confined to lesson metadata, brain-pack ordering, and the
act-on citation ledger (the 2026-09-23..10-05 outage: zero lesson citations in
~60 routine runs, invisible because a zero left no trace).

    .venv/bin/python -m pytest tests/test_lesson_scorecard.py -q
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import journal  # noqa: E402
import tools.knowledge_base as kbm  # noqa: E402
import tools.learning_adopt as la  # noqa: E402
import tools.lesson_scorecard as ls  # noqa: E402
from runlib.brain_io import parse_proposals  # noqa: E402

TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Every store the scorecard reads or writes, redirected."""
    monkeypatch.setattr(kbm, "KB_JSON", tmp_path / "knowledge_base.json")
    monkeypatch.setattr(kbm, "KB_MD", tmp_path / "knowledge_base.md")
    monkeypatch.setattr(kbm, "JOURNAL_JSON", tmp_path / "learning_journal.json")
    monkeypatch.setattr(la, "ADOPTED_JSON", tmp_path / "adopted_lessons.json")
    monkeypatch.setattr(la, "PROPOSALS_FILE", tmp_path / "learning_proposals.json")
    monkeypatch.setattr(ls, "SCORECARD_JSON", tmp_path / "lesson_scorecard.json")
    monkeypatch.setattr(ls, "SCORECARD_MD", tmp_path / "lesson_scorecard.md")
    monkeypatch.setattr(ls, "RUN_ARCHIVE_GLOB", str(tmp_path / "archive" / "run_*.json"))
    monkeypatch.setattr(ls, "SHADOW_JSON", tmp_path / "shadow_portfolio.json")
    monkeypatch.setattr(ls, "EXIT_AUTOPSY_DIR", tmp_path / "exit_autopsies")
    monkeypatch.setattr(journal, "JOURNAL", tmp_path / "journal")
    (tmp_path / "archive").mkdir()
    return tmp_path


def _kb(tmp, entries):
    (tmp / "knowledge_base.json").write_text(json.dumps({"entries": entries}))


def _kb_entry(lid, learned="2026-08-01", discipline="technical_analysis", **kw):
    return {"id": lid, "topic": f"topic {lid}", "discipline": discipline,
            "summary": "s" * 90, "how_to_apply": "h" * 70,
            "learned_at": learned + "T00:00:00+00:00", **kw}


def _adopted(tmp, lessons):
    (tmp / "adopted_lessons.json").write_text(json.dumps({"lessons": lessons}))


def _shadows(tmp, closed, positions=()):
    (tmp / "shadow_portfolio.json").write_text(json.dumps(
        {"version": 1, "closed": list(closed), "positions": list(positions)}))


def _shadow(sid, ticker, opened, verdict, run_id=None, reason="r", attribution=None):
    return {"id": sid, "ticker": ticker, "opened_at": opened, "closed_at": opened,
            "status": "closed", "verdict": verdict, "run_id": run_id,
            "source": "rejected_idea", "reason": reason,
            "verdict_attribution": attribution or "idiosyncratic"}


def _archive(tmp, run_id, day, rejected=(), watchlist=(), proposals=(), reasoning=""):
    (tmp / "archive" / f"run_{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "as_of_et": day, "rejected_ideas": list(rejected),
        "watchlist": list(watchlist), "proposals": list(proposals),
        "latest_reasoning": reasoning}))


def _skip_world(tmp, lid, n_good, n_regret, start=date(2026, 7, 1), extra_good=40,
                extra_regret=40):
    """n distinct tickers skipped citing `lid`, each with its own graded
    shadow, plus uncited background shadows that set the baseline."""
    shadows = []
    i = 0
    for verdict, n in (("good_skip", n_good), ("regret_miss", n_regret)):
        for _ in range(n):
            day = (start + timedelta(days=i)).isoformat()
            t = f"T{i:02d}"
            rid = f"{day.replace('-', '')}-aaaa{i:02d}"
            _archive(tmp, rid, day, rejected=[{"ticker": t, "reason": f"per {lid} no"}])
            shadows.append(_shadow(f"SH-{i}", t, day, verdict, run_id=rid))
            i += 1
    for k in range(extra_good):
        shadows.append(_shadow(f"BG-G{k}", f"BG{k}", "2026-07-01", "good_skip"))
    for k in range(extra_regret):
        shadows.append(_shadow(f"BG-R{k}", f"BR{k}", "2026-07-01", "regret_miss"))
    _shadows(tmp, shadows)


def _card(**kw):
    return ls.build_scorecard(TODAY, closed_trades=[], **kw)


def _row(card, lid):
    return next(r for r in card["lessons"] if r["id"] == lid)


# --------------------------------------------------------------------- pure math
def test_classify_needs_min_sample_and_wilson_edge():
    assert ls.classify(0, ls.MIN_GRADED - 1, 0.5) == "insufficient_data"
    assert ls.classify(0, 10, 0.5) == "hurting"
    assert ls.classify(10, 10, 0.5) == "helping"
    # a 15-point gap on a small sample is NOT enough when Wilson overlaps
    assert ls.classify(3, 9, 0.5) == "no_clear_edge"
    lo, hi = ls.wilson_bounds(5, 10)
    assert 0 < lo < 0.5 < hi < 1


# --------------------------------------------------------------- outcome joins
def test_skip_citations_grade_against_shadow_verdicts(sandbox):
    _kb(sandbox, [_kb_entry("KB-1111111111")])
    _skip_world(sandbox, "KB-1111111111", n_good=0, n_regret=9)
    r = _row(_card(), "KB-1111111111")
    assert r["n_graded"] == 9 and r["n_correct"] == 0
    assert r["skips"]["regret_miss"] == 9
    assert r["verdict"] == "hurting"


def test_market_wide_shadow_verdicts_are_excluded(sandbox):
    _kb(sandbox, [_kb_entry("KB-1111111111")])
    _archive(sandbox, "20260801-aaaa01", "2026-08-01",
             rejected=[{"ticker": "AAA", "reason": "KB-1111111111"}])
    _shadows(sandbox, [_shadow("S1", "AAA", "2026-08-01", "regret_miss",
                               run_id="20260801-aaaa01", attribution="market_wide")])
    r = _row(_card(), "KB-1111111111")
    assert r["n_graded"] == 0
    assert r["skips"]["market_wide_excluded"] == 1


def test_rewatched_ticker_counts_once_per_window(sandbox):
    """A name re-watched every run is one decision, not twenty."""
    _kb(sandbox, [_kb_entry("KB-1111111111")])
    shadows = []
    for i, day in enumerate(["2026-08-01", "2026-08-06", "2026-08-11"]):
        rid = f"{day.replace('-', '')}-bbbb0{i}"
        _archive(sandbox, rid, day, watchlist=[
            {"ticker": "AAA", "status": "hold", "thoughts": "KB-1111111111 wait"}])
        shadows.append(_shadow(f"S{i}", "AAA", day, "regret_miss", run_id=rid))
    _shadows(sandbox, shadows)
    r = _row(_card(), "KB-1111111111")
    assert r["n_graded"] == 1
    assert r["skips"]["correlated_dupes"] == 2


def test_buy_citation_grades_against_closed_trade_r(sandbox):
    _kb(sandbox, [_kb_entry("KB-2222222222")])
    _archive(sandbox, "20260810-cccc01", "2026-08-10", proposals=[
        {"proposal": {"ticker": "HPE", "action": "BUY",
                      "thesis": "per KB-2222222222 the gap holds"}}])
    _shadows(sandbox, [])
    card = ls.build_scorecard(TODAY, closed_trades=[
        {"ticker": "HPE", "opened_at": "2026-08-10", "closed_at": "2026-08-19",
         "r_multiple": -0.54, "total_pnl_usd": -2.1}])
    r = _row(card, "KB-2222222222")
    assert r["trades"]["n"] == 1 and r["trades"]["wins"] == 0
    assert r["trades"]["avg_r"] == -0.54


def test_exit_autopsy_trades_count_but_ghost_writeoffs_do_not(sandbox):
    d = sandbox / "exit_autopsies"
    d.mkdir()
    rows = [
        {"ticker": "DXCM", "action": "SELL_TO_CLOSE", "fill_price": 60.39,
         "avg_cost": 60.0, "entry_plan": {"stop_loss": 59.0},
         "position_opened_at": "2026-08-06T15:00:00", "filled_at": "2026-09-08T15:00:00",
         "realized_pnl_usd": 0.25},
        {"ticker": "BKR", "action": "SELL_TO_CLOSE", "fill_price": 63.64,
         "avg_cost": 58.2, "entry_plan": {"stop_loss": 54.0},
         "forced_reason": "ghost_reconcile_broker_flat",
         "filled_at": "2026-09-10T15:00:00"},
    ]
    (d / "2026-09-10.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    trades = ls.load_closed_trades(closed_trades=[])
    assert [t["ticker"] for t in trades] == ["DXCM"]
    assert trades[0]["r"] == 0.39


# ------------------------------------------------------------------ action plan
def test_hurting_lesson_retired_and_marked_superseded(sandbox):
    _kb(sandbox, [_kb_entry("KB-1111111111"), _kb_entry("KB-9999999999")])
    _skip_world(sandbox, "KB-1111111111", n_good=0, n_regret=10)
    card = ls.run(apply=True, today=TODAY)
    assert card["changed"]["retired"] == ["KB-1111111111"]
    doc = json.loads((sandbox / "knowledge_base.json").read_text())
    e = next(x for x in doc["entries"] if x["id"] == "KB-1111111111")
    assert e["superseded_by"] == ls.SCORECARD_SUPERSEDER
    assert e["supersede_reason"] == "scorecard_hurting"
    # the brain sees the marker, and the lesson leaves the active pack
    pack = kbm.brain_facing_knowledge_base()
    assert "KB-1111111111" not in [r["id"] for r in pack["recent"]]
    markers = pack.get("superseded_by_scorecard") or []
    assert markers and markers[0]["marker"].startswith("[SUPERSEDED")
    # nothing outside lesson metadata was written
    assert (sandbox / "lesson_scorecard.json").exists()
    assert json.loads((sandbox / "lesson_scorecard.json").read_text())["history"][-1]["retired"] \
        == ["KB-1111111111"]


def test_weekly_retirement_cap(sandbox, monkeypatch):
    monkeypatch.setattr(ls, "MAX_RETIREMENTS_PER_WEEK", 1)
    _kb(sandbox, [_kb_entry("KB-1111111111"), _kb_entry("KB-3333333333")])
    shadows = []
    for j, lid in enumerate(("KB-1111111111", "KB-3333333333")):
        for i in range(9):
            day = (date(2026, 7, 1) + timedelta(days=i)).isoformat()
            t = f"X{j}{i}"
            rid = f"{day.replace('-', '')}-dd{j}{i:03d}"
            _archive(sandbox, rid, day, rejected=[{"ticker": t, "reason": lid}])
            shadows.append(_shadow(f"S{j}{i}", t, day, "regret_miss", run_id=rid))
    shadows += [_shadow(f"G{k}", f"G{k}", "2026-07-01", "good_skip") for k in range(60)]
    _shadows(sandbox, shadows)
    first = ls.run(apply=True, today=TODAY)
    assert len(first["changed"]["retired"]) == 1
    assert len(first["actions"]["deferred_by_cap"]) == 1
    second = ls.run(apply=True, today=TODAY + timedelta(days=1))  # same ISO week
    assert second["changed"]["retired"] == []


def test_protected_fresh_and_validated_lessons_are_flagged_not_retired(sandbox):
    _kb(sandbox, [
        _kb_entry("KB-4444444444", discipline="risk_management"),
        _kb_entry("KB-5555555555", learned="2026-10-01"),
        _kb_entry("KB-6666666666", evidence_status="validated"),
    ])
    shadows = []
    for j, lid in enumerate(("KB-4444444444", "KB-5555555555", "KB-6666666666")):
        for i in range(10):
            day = (date(2026, 7, 1) + timedelta(days=i)).isoformat()
            t = f"P{j}{i}"
            rid = f"{day.replace('-', '')}-ee{j}{i:03d}"
            _archive(sandbox, rid, day, rejected=[{"ticker": t, "reason": lid}])
            shadows.append(_shadow(f"S{j}{i}", t, day, "regret_miss", run_id=rid))
    shadows += [_shadow(f"G{k}", f"G{k}", "2026-07-01", "good_skip") for k in range(60)]
    _shadows(sandbox, shadows)
    plan = _card()["actions"]
    assert plan["retire"] == []
    whys = {x["id"]: x["why"] for x in plan["flag_for_review"]}
    assert whys["KB-4444444444"].startswith("protected_risk_lesson")
    assert whys["KB-5555555555"].startswith("too_fresh")
    assert whys["KB-6666666666"].startswith("validated_by_trades")


def test_insufficient_data_never_acts(sandbox):
    _kb(sandbox, [_kb_entry("KB-1111111111")])
    _skip_world(sandbox, "KB-1111111111", n_good=0, n_regret=ls.MIN_GRADED - 1)
    plan = _card()["actions"]
    assert plan["retire"] == [] and plan["boost"] == []


def test_helping_lesson_boosted_into_the_pack(sandbox):
    entries = [_kb_entry("KB-7777777777", learned="2026-07-01")]
    entries += [_kb_entry(f"KB-80000000{i:02d}", learned=f"2026-09-{10 + i:02d}")
                for i in range(10)]
    _kb(sandbox, entries)
    _skip_world(sandbox, "KB-7777777777", n_good=10, n_regret=0)
    assert "KB-7777777777" not in [r["id"] for r in kbm.brain_facing_knowledge_base()["recent"]]
    card = ls.run(apply=True, today=TODAY)
    assert "KB-7777777777" in card["changed"]["boosted"]
    recent = kbm.brain_facing_knowledge_base()["recent"]
    assert recent[0]["id"] == "KB-7777777777"
    assert recent[0]["scorecard"].startswith("helping")


def test_stale_deprioritized_only_when_capture_is_healthy(sandbox):
    _kb(sandbox, [_kb_entry("KB-1212121212", learned="2026-08-01"),
                  _kb_entry("KB-3434343434", learned="2026-08-01")])
    _shadows(sandbox, [])
    plan = _card()["actions"]
    assert plan["deprioritize"] == []
    assert "unhealthy" in plan["stale_skipped_reason"]
    # a fresh citation of the OTHER lesson proves capture works -> staleness judged
    _archive(sandbox, "20261003-ffff01", "2026-10-03",
             rejected=[{"ticker": "ZZZ", "reason": "KB-3434343434"}])
    plan = _card()["actions"]
    assert [x["id"] for x in plan["deprioritize"]] == ["KB-1212121212"]


def test_adopted_noise_sinks_below_real_lessons(sandbox):
    _adopted(sandbox, [
        {"id": "LP-0000000001", "text": "Real process lesson about triggers " * 3,
         "adopted_at": "2026-09-01T00:00:00+00:00"},
        {"id": "LP-0000000002", "text": "[learning-adopt] auto_adopted=22 ids=[...]",
         "adopted_at": "2026-09-18T00:00:00+00:00"},
    ])
    _kb(sandbox, [])
    _shadows(sandbox, [])
    assert la.brain_facing_adopted_lessons(1)["lessons"][0]["id"] == "LP-0000000002"
    ls.run(apply=True, today=TODAY)
    assert la.brain_facing_adopted_lessons(1)["lessons"][0]["id"] == "LP-0000000001"


def test_dry_run_changes_nothing(sandbox):
    _kb(sandbox, [_kb_entry("KB-1111111111")])
    _skip_world(sandbox, "KB-1111111111", n_good=0, n_regret=10)
    before = (sandbox / "knowledge_base.json").read_text()
    card = ls.run(apply=False, today=TODAY)
    assert [x["id"] for x in card["actions"]["retire"]] == ["KB-1111111111"]
    assert (sandbox / "knowledge_base.json").read_text() == before
    assert not (sandbox / "lesson_scorecard.json").exists()
    assert not (sandbox / "journal" / "lesson_scorecard").exists()


def test_scorecard_never_touches_config_or_risk_files():
    """Code only (docstrings and comments stripped): the scorecard may edit
    lesson metadata, never config, validator, the book or the kill switch."""
    import ast
    tree = ast.parse(Path(ls.__file__).read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
    code = ast.unparse(tree)
    for forbidden in ("autonomy_config", "validator", "state/portfolio", "\"state\"",
                      "KILL_SWITCH", "load_config"):
        assert forbidden not in code, forbidden


# ------------------------------------------------------- citation capture path
def test_parse_keeps_lessons_applied():
    resp = ('```json\n{"proposals": [], "no_trade_reason": "x", "lessons_applied": '
            '[{"id": "KB-1111111111", "ticker": "NOW", "decision": "wait"}, "junk"]}\n```')
    out = parse_proposals(resp)
    assert out["lessons_applied"] == [
        {"id": "KB-1111111111", "ticker": "NOW", "decision": "wait"}]
    assert "lessons_applied" not in parse_proposals("no json")  # legacy key set kept


def test_extract_run_citations_by_decision():
    parsed = {
        "lessons_applied": [{"id": "LP-0000000009", "ticker": "okta", "decision": "buy"}],
        "proposals": [{"ticker": "OKTA", "action": "BUY", "thesis": "flag per KB-1000000001"}],
        "rejected_ideas": [{"ticker": "MU", "reason": "extended (KB-1000000002)"}],
        "watchlist": [{"ticker": "HPE", "status": "hold", "thoughts": "KB-1000000003"},
                      {"ticker": "COST", "status": "buy", "thoughts": "KB-1000000004"}],
    }
    response = "Para about OKTA and KB-1000000005.\n\nUnrelated KB-1000000006."
    rows = {(r["lesson_id"], r["ticker"], r["decision"])
            for r in ls.extract_run_citations(parsed, response)}
    assert ("LP-0000000009", "OKTA", "act") in rows
    assert ("KB-1000000001", "OKTA", "act") in rows
    assert ("KB-1000000005", "OKTA", "act") in rows      # paragraph names OKTA
    assert ("KB-1000000002", "MU", "skip") in rows
    assert ("KB-1000000003", "HPE", "wait") in rows
    assert ("KB-1000000006", None, "other") in rows      # cited, never graded
    assert not any(r[0] == "KB-1000000004" and r[1] == "COST" for r in rows)


def test_record_run_citations_writes_ledger_even_when_zero(sandbox):
    _adopted(sandbox, [{"id": "LP-0000000009", "text": "t" * 50,
                        "adopted_at": "2026-09-01T00:00:00+00:00"}])
    ls.record_run_citations({"proposals": []}, "nothing cited", "20261005-000001")
    out = ls.record_run_citations(
        {"lessons_applied": [{"id": "LP-0000000009", "ticker": "NOW", "decision": "wait"}]},
        "", "20261005-000002")
    assert out["n_adopted_bumped"] == 1
    files = list((sandbox / "journal" / "lesson_citations").glob("*.jsonl"))
    recs = [json.loads(x) for f in files for x in f.read_text().splitlines()]
    assert [r["n_citations"] for r in recs] == [0, 1]
    lp = json.loads((sandbox / "adopted_lessons.json").read_text())["lessons"][0]
    assert lp["times_cited"] == 1
    # the ledger feeds the scorecard
    atts = ls.load_ledger_attributions()
    assert atts and atts[0]["lesson_id"] == "LP-0000000009"


def test_citation_health_nudges_on_silent_runs(sandbox):
    for i in range(4):
        ls.record_run_citations({}, "", f"20261005-00000{i}")
    ch = ls.citation_health()
    assert ch["runs"] == 4 and ch["runs_citing"] == 0
    assert "nudge" in ch


def test_orchestrator_side_outputs_call_the_ledger():
    src = (Path(__file__).resolve().parent.parent / "orchestrator.py").read_text()
    body = src.split("def _journal_side_outputs", 1)[1].split("\ndef ", 1)[0]
    assert "record_run_citations(parsed, response, run_id)" in body
    assert '"--lesson-scorecard"' in src
