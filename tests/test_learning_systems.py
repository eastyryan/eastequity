"""Offline tests for the five self-improvement systems."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


@pytest.fixture()
def tmp_learning(tmp_path, monkeypatch):
    """Point learning modules at temp paths."""
    import tools.shadow_portfolio as SP
    import tools.concept_memory as CM
    import tools.exit_autopsy as EA
    import tools.learning_adopt as LA
    import tools.calibration_gate as CG
    import tools.post_exit_runners as PR

    monkeypatch.setattr(SP, "SHADOW_FILE", tmp_path / "shadow.json")
    monkeypatch.setattr(SP, "ROOT", tmp_path)
    monkeypatch.setattr(CM, "MEM_DIR", tmp_path / "concept_memory")
    monkeypatch.setattr(CM, "ROOT", tmp_path)
    monkeypatch.setattr(EA, "AUTOPSY_DIR", tmp_path / "exit_autopsies")
    monkeypatch.setattr(EA, "BINDING_FILE", tmp_path / "binding_exit_lessons.json")
    monkeypatch.setattr(EA, "ROOT", tmp_path)
    monkeypatch.setattr(LA, "PROPOSALS_FILE", tmp_path / "learning_proposals.json")
    monkeypatch.setattr(LA, "ADOPTED_FILE", tmp_path / "adopted_lessons.md")
    monkeypatch.setattr(LA, "ADOPTED_JSON", tmp_path / "adopted_lessons.json")
    monkeypatch.setattr(LA, "ROOT", tmp_path)
    # post_exit_runners was MISSING here, and that omission wrote fabricated trades
    # into production. test_exit_grade persists a synthetic HPE stub (fill 40.00,
    # pnl -50.00, days_held 20, stop 41.00) and grade_and_persist_autopsy chains
    # into exit_autopsy -> register_from_exit, which wrote to the REAL
    # data/post_exit_runners.json. That record was committed on 2026-07-16, re-marked
    # on every run for three days, and the runner-learning loop computed
    # current_leftover_pct against it — studying upside left on the table by a trade
    # that never happened (real HPE close: 46.08 / -45.33 / 4 days / stop 43.75).
    monkeypatch.setattr(PR, "RUNNERS_FILE", tmp_path / "post_exit_runners.json")
    monkeypatch.setattr(PR, "ROOT", tmp_path)
    # calibration reads real latest.json — leave as is for soft test; unit pure funcs below
    return tmp_path


def test_shadow_portfolio(tmp_learning):
    print("shadow portfolio:")
    from tools.shadow_portfolio import (
        open_shadow, mark_shadows, record_from_rejected_ideas,
        close_shadow_if_bought, brain_facing_shadow_learning, _verdict,
    )
    p = open_shadow(ticker="AMD", entry_price=100.0, source="rejected_idea",
                    reason="MACD still weak")
    check("opened", p is not None and p["ticker"] == "AMD")
    n = record_from_rejected_ideas(
        [{"ticker": "MU", "reason": "cycle peak risk on memory name"}],
        {"MU": 50.0}, run_id="test")
    check("rejected recorded", n == 1)
    mark_shadows({"AMD": 115.0, "MU": 45.0})
    # Force verdict helpers
    check("regret verdict", _verdict({
        "would_hit_target": True, "would_hit_stop": False,
        "max_upside_pct": 12, "max_drawdown_pct": -3,
    }) == "regret_miss")
    check("good skip verdict", _verdict({
        "would_hit_target": False, "would_hit_stop": True,
        "max_upside_pct": 2, "max_drawdown_pct": -15,
    }) == "good_skip")
    close_shadow_if_bought("AMD", 116.0)
    face = brain_facing_shadow_learning()
    check("brain facing keys", "regret_misses" in face and "good_skips" in face)


def test_exit_grade(tmp_learning):
    print("exit grade:")
    from tools.exit_autopsy import grade_exit_autopsy, grade_and_persist_autopsy
    stub = {
        "ticker": "HPE", "action": "SELL_TO_CLOSE", "fill_price": 40.0,
        "quantity": 10, "days_held": 20, "realized_pnl_usd": -50.0,
        "forced": True, "forced_reason": "stop_loss_breached",
        "entry_plan": {"stop_loss": 41.0, "thesis_invalidators": {
            "invalidating_print": "guide cut", "invalidating_structure": "50dma",
            "time_box": "30 days"}},
        "status": "system_stub", "needs_brain_grade": True,
    }
    g = grade_exit_autopsy(stub)
    check("graded", g.get("status") == "graded")
    check("has lesson", len(str(g.get("lesson") or "")) > 20)
    check("process set", g.get("brain_grade", {}).get("process") in (
        "process_win", "process_fail", "mixed"))
    merged = grade_and_persist_autopsy(stub)
    check("persisted grade", merged.get("status") == "graded")


def test_concept_memory(tmp_learning):
    print("concept memory:")
    from tools.concept_memory import (
        update_from_research, append_lesson, brain_facing_concept_memory, load_memory,
    )
    update_from_research(
        "NBIS",
        stack_card={
            "what_they_do": "GPU cloud capacity for AI training.",
            "demand_driver": "hyperscaler_cloud_infra",
            "differential_question": "Utilization vs hyperscaler pricing?",
            "business_model_note": "Capex-growth scale-up.",
        },
        checklist={
            "model_type": "capex_growth_scaleup",
            "weight": "Growth and liquidity first.",
            "label": "Capex growth",
            "lines": [{"line": "revenue_growth", "red_flag": "cuts"}],
        },
    )
    mem = load_memory("NBIS")
    check("what they do", "GPU" in (mem.get("what_they_do") or ""))
    append_lesson("NBIS", "Estimate cuts into rich multiple hurt entries.",
                  source="test", tags=["estimates"])
    face = brain_facing_concept_memory(["NBIS"])
    check("in face", "NBIS" in (face.get("by_ticker") or {}))
    check("lessons", len(face["by_ticker"]["NBIS"].get("recent_lessons") or []) >= 1)


def test_learning_adopt(tmp_learning):
    print("learning adopt:")
    from tools.learning_adopt import (
        propose_adoptions, auto_adopt_soft, brain_facing_adopted_lessons,
    )
    notes = [
        {"ts": "2026-07-14T00:00:00+00:00", "run_id": "r1",
         "note": "Weekly self-review: I will stop chasing unconfirmed breakouts without volume confirmation for the next week."},
        {"ts": "2026-07-14T00:00:00+00:00", "run_id": "r2",
         "note": "We should change the validator hard rule for market hours somehow."},
    ]
    props = propose_adoptions(notes)
    check("proposals", len(props) >= 1)
    soft = [p for p in props if p.get("auto_adoptable")]
    hard = [p for p in props if not p.get("auto_adoptable")]
    check("has soft", len(soft) >= 1)
    check("has hard", len(hard) >= 1)
    res = auto_adopt_soft(props)
    check("auto adopted", res.get("auto_adopted", 0) >= 1)
    face = brain_facing_adopted_lessons()
    check("lessons injected", face.get("n_adopted", 0) >= 1)


def test_calibration_pure():
    print("calibration pure:")
    from tools.calibration_gate import high_conf_inflated, losing_sector_buckets
    inflated, _ = high_conf_inflated({
        "high_conf_0_70_plus": {"inflated": True, "trades": 10, "win_rate_pct": 30},
    })
    check("inflated true", inflated is True)
    losers = losing_sector_buckets({
        "by_sector": [
            {"sector": "networking", "trades": 6, "win_rate_pct": 20},
            {"sector": "mega_tech", "trades": 2, "win_rate_pct": 0},
        ]
    })
    check("one loser sector", len(losers) == 1 and losers[0]["sector"] == "networking")


def test_post_exit_runners(tmp_learning, monkeypatch):
    print("post-exit runners (let winners run):")
    import tools.post_exit_runners as PR
    monkeypatch.setattr(PR, "RUNNERS_FILE", tmp_learning / "post_exit_runners.json")
    monkeypatch.setattr(PR, "ROOT", tmp_learning)
    from tools.post_exit_runners import (
        register_from_exit, mark_post_exit_runners, brain_facing_runner_learning,
        _score_runner,
    )
    # Winner exit at 110 from cost 100
    rec = register_from_exit({
        "ticker": "DELL", "fill_price": 110.0, "avg_cost": 100.0,
        "realized_pnl_usd": 50.0, "filled_at": "2026-05-01T15:00:00+00:00",
        "forced": False, "brain_grade": {"process": "process_win"},
        "days_held": 20,
    })
    check("registered winner", rec is not None and rec.get("track_kind") == "winner")
    # Simulate price ran to 130 after exit
    # Force age by editing exit_date far in past
    st = json.loads((tmp_learning / "post_exit_runners.json").read_text())
    st["tracking"][0]["exit_date"] = "2026-05-01"
    (tmp_learning / "post_exit_runners.json").write_text(json.dumps(st))
    mark_post_exit_runners({"DELL": 130.0})
    st2 = json.loads((tmp_learning / "post_exit_runners.json").read_text())
    # Either still tracking with marks or completed
    track = (st2.get("tracking") or []) + (st2.get("completed") or [])
    check("has dell track", any(t.get("ticker") == "DELL" for t in track))
    dell = next(t for t in track if t.get("ticker") == "DELL")
    check("extension positive", float(dell.get("max_extension_pct") or
          (dell.get("high_after_exit", 110) / 110 - 1) * 100) >= 10
          or float(dell.get("high_after_exit") or 0) >= 125)
    scored = _score_runner({
        "ticker": "DELL", "exit_price": 110, "high_after_exit": 130,
        "low_after_exit": 108, "track_kind": "winner", "gain_at_exit_pct": 10,
        "marks": {"30": {"leftover_pct": 15}},
    })
    check("left on table verdict", scored.get("runner_verdict") == "left_on_table")
    check("lesson mentions partials", "PARTIAL" in (scored.get("hold_lesson") or "").upper()
          or "trail" in (scored.get("hold_lesson") or "").lower())
    face = brain_facing_runner_learning()
    check("playbook present", "hold_winners_playbook" in face)

    # Attribution: catalyst vs technical
    from tools.post_exit_runners import (
        classify_headline_for_attribution, attribute_left_on_table, classify_path_shape,
    )
    check("earn headline catalyst",
          classify_headline_for_attribution("Nebius beats earnings, raises guidance") == "catalyst")
    check("breakout headline technical",
          classify_headline_for_attribution("Stock breaks out above resistance on volume")
          == "technical")
    path = classify_path_shape(
        {"15": {"leftover_pct": 2}, "30": {"leftover_pct": 9}, "60": {"leftover_pct": 18}},
        18)
    check("late/grind shape", path.get("path_shape") in ("late_spike", "steady_grind"))
    attr = attribute_left_on_table(
        headlines=[{"title": "Company wins major AI contract with Microsoft"}],
        path_shape={"path_shape": "early_spike", "note": "spike"},
        max_extension_pct=20,
    )
    check("catalyst primary", attr.get("primary_driver") == "catalyst")
    attr2 = attribute_left_on_table(
        headlines=[],
        path_shape={"path_shape": "steady_grind",
                    "note": "grind", "mark_leftovers": {"15": 3, "60": 14}},
        max_extension_pct=14,
    )
    check("technical primary", attr2.get("primary_driver") == "technical")
    scored2 = _score_runner({
        "ticker": "DELL", "exit_price": 110, "high_after_exit": 130,
        "low_after_exit": 108, "track_kind": "winner", "gain_at_exit_pct": 10,
        "marks": {"15": {"leftover_pct": 3}, "60": {"leftover_pct": 18}},
    }, attribution=attr)
    check("lesson mentions catalyst why",
          "CATALYST" in (scored2.get("hold_lesson") or "").upper())


if __name__ == "__main__":
    # Script mode: use real paths carefully — only pure + tmp via pytest preferred
    test_calibration_pure()
    print("(run full suite with pytest for tmp-isolated tests)")
    if FAILURES:
        sys.exit(1)
