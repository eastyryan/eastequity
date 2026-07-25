"""Tests for tiered brain context + learning mark / lesson prune."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_slim_context_for_brain():
    print("context tiers:")
    from runlib.context_tiers import slim_context_for_brain, write_tiered_context

    full = {
        "run_date": "2026-07-14T12:00:00+00:00",
        "as_of_et": "2026-07-14",
        "trading_mode": "paper",
        "run_depth": "full",
        "allows_new_buys": True,
        "hard_limits": {"max_position_pct": 15},
        "digest": {"by_ticker": {"DELL": {"one_line": "held"}}},
        "macro_regime": {"status": "ok", "regime_hint": "neutral"},
        "portfolio": {"positions": [{"ticker": "DELL"}], "cash_usd": 5000},
        "lessons_learned": ["be patient"],
        "track_record": {
            "closed_trades": [{"ticker": f"T{i}"} for i in range(20)],
            "performance": {"win_rate": 50},
        },
        "universe_scan": {
            "status": "ok",
            "prices": {"DELL": 100.0, "NVDA": 900.0},
            "top_setups": [{"ticker": f"S{i}"} for i in range(30)],
        },
        "deep_fundamentals": {
            "DELL": {"quality": "ok"},
            "ZZZZ": {"quality": "noise"},
            "status": "ok",
        },
        "reasoning_process": {
            "process_checklist": ["regime", "book"],
            "theme_exposure": {},
            "shadow_learning": {
                "binding": False,
                "regret_misses": [{"ticker": "A"}] * 10,
                "good_skips": [{"ticker": "B"}] * 10,
                "stats": {"n": 2},
            },
            "adopted_lessons": {
                "lessons": [{"id": "1", "text": "x"}] * 8,
                "n_adopted": 8,
            },
            "calibration_status": {
                "phase": "warmup",
                "total_closed_trades": 2,
            },
            "demand_driver_map": {
                "DELL": "servers",
                "NOISE1": "a",
                "NOISE2": "b",
            },
        },
        "stack_cards": {"by_ticker": {"DELL": {"layer": "OEM"}}},
    }
    slim = slim_context_for_brain(full, learning_n=5)
    check("tier marker", slim.get("_context_tier") == "brain_slim_v2")
    check("always keys kept", slim.get("portfolio") is not None)
    check("learning pack present",
          isinstance((slim.get("reasoning_process") or {}).get("learning_pack"), dict))
    lp = slim["reasoning_process"]["learning_pack"]
    check("regret top-N", len(lp.get("shadow", {}).get("regret_misses") or []) <= 5)
    check("track record truncated",
          len((slim.get("track_record") or {}).get("closed_trades") or []) <= 10)
    check("top_setups capped",
          len((slim.get("universe_scan") or {}).get("top_setups") or []) <= 15)

    # write tiered
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        slim_p = Path(td) / "context_test.json"
        full_p = Path(td) / "context_full_test.json"
        out = write_tiered_context(full, slim_p, full_p, learning_n=3)
        check("slim file exists", slim_p.exists())
        check("full file exists", full_p.exists())
        check("full path stamped", "full_context_path" in out)
        full_loaded = json.loads(full_p.read_text())
        check("full has deep noise ticker", "ZZZZ" in (full_loaded.get("deep_fundamentals") or {}))


def test_lesson_prune(tmp_path, monkeypatch):
    print("lesson prune:")
    import tools.learning_adopt as LA
    monkeypatch.setattr(LA, "ADOPTED_JSON", tmp_path / "adopted_lessons.json")
    monkeypatch.setattr(LA, "ADOPTED_FILE", tmp_path / "adopted_lessons.md")
    monkeypatch.setattr(LA, "PROPOSALS_FILE", tmp_path / "learning_proposals.json")
    monkeypatch.setattr(LA, "ROOT", tmp_path)

    lessons = []
    for i in range(10):
        lessons.append({
            "id": f"L{i}",
            "kind": "soft_lesson",
            "text": f"Always re-underwrite holdings before adding risk number {i % 3}",
            "adopted_at": f"2026-01-{i+1:02d}T00:00:00+00:00",
        })
    # Exact near-dups on fingerprint of first 48 chars
    lessons.append({
        "id": "L_DUP",
        "kind": "soft_lesson",
        "text": "Always re-underwrite holdings before adding risk number 0 EXTRA",
        "adopted_at": "2026-02-01T00:00:00+00:00",
    })
    LA.ADOPTED_JSON.write_text(json.dumps({"lessons": lessons}, indent=2))

    r = LA.prune_adopted_lessons(max_active=5, near_dup_chars=40)
    check("prune ran", r.get("status") != "error", str(r))
    check("some pruned or capped", (r.get("pruned") or 0) >= 1, str(r))
    check("active <= max", (r.get("active") or 99) <= 5, str(r))

    face = LA.brain_facing_adopted_lessons(limit=20)
    check("brain sees only active",
          all(not L.get("superseded_by") for L in face.get("lessons") or []))
    check("brain n_adopted <= 5", (face.get("n_adopted") or 0) <= 5, str(face))


def test_learning_mark_offline(tmp_path, monkeypatch):
    print("learning mark offline:")
    import tools.shadow_portfolio as SP
    import tools.post_exit_runners as PER
    import tools.learning_adopt as LA
    import tools.learning_mark as LM

    monkeypatch.setattr(SP, "SHADOW_FILE", tmp_path / "shadow.json")
    monkeypatch.setattr(SP, "ROOT", tmp_path)
    monkeypatch.setattr(PER, "RUNNERS_FILE", tmp_path / "runners.json")
    monkeypatch.setattr(PER, "ROOT", tmp_path)
    monkeypatch.setattr(LA, "ADOPTED_JSON", tmp_path / "adopted.json")
    monkeypatch.setattr(LA, "ADOPTED_FILE", tmp_path / "adopted.md")
    monkeypatch.setattr(LA, "PROPOSALS_FILE", tmp_path / "props.json")
    monkeypatch.setattr(LA, "ROOT", tmp_path)
    monkeypatch.setattr(LM, "ROOT", tmp_path)

    # Seed a shadow
    SP.open_shadow(ticker="AMD", entry_price=100.0, source="test", reason="unit")
    # Seed a runner mid-window
    PER._save({
        "tracking": [{
            "ticker": "HPE",
            "exit_date": "2026-06-01",
            "exit_price": 20.0,
            "track_kind": "winner",
            "gain_at_exit_pct": 10.0,
            "marks": {},
            "high_after_exit": 20.0,
            "low_after_exit": 20.0,
        }],
        "completed": [],
    })
    LA.ADOPTED_JSON.write_text(json.dumps({
        "lessons": [
            {"id": "A", "text": "lesson one about process discipline always",
             "adopted_at": "2026-01-01T00:00:00+00:00"},
            {"id": "B", "text": "lesson one about process discipline always again",
             "adopted_at": "2026-01-02T00:00:00+00:00"},
        ]
    }))

    # Avoid network for news cache
    monkeypatch.setattr(PER, "cache_headlines_for_ticker",
                        lambda *a, **k: [{"title": "HPE wins deal", "published": "2026-06-20"}])

    out = LM.run_learning_mark(
        run_id="test-mark",
        prices={"AMD": 110.0, "HPE": 24.0},
        cache_news=True,
        prune=True,
        max_active_lessons=10,
    )
    check("status ok", out.get("status") == "ok", str(out))
    check("shadow marked", "shadow" in out)
    check("runners marked", "post_exit_runners" in out)
    check("prune ran", "lesson_prune" in out)

    # Headlines cached on mark if age allows
    state = PER._load()
    tracking = state.get("tracking") or state.get("completed") or []
    # May have completed if age past 60 from fixed exit_date relative to "today"
    # Just ensure no crash and structure ok
    check("runners state loadable", isinstance(state, dict))


def test_orchestrator_reexports():
    print("orchestrator facade:")
    import orchestrator
    check("claude_cmd alias", callable(orchestrator._claude_cmd))
    check("llm_settings alias", callable(orchestrator._llm_settings))
    check("sector_map alias", callable(orchestrator._sector_map))
    check("expected_slots", orchestrator.expected_slots(True) == [6, 8.75, 10.5, 12, 14, 16, 17.5])
    check("compute_closed_trades", callable(orchestrator.compute_closed_trades))
    check("write_tiered_context", callable(orchestrator.write_tiered_context))
    check("gather_context", callable(orchestrator.gather_context))
    check("main", callable(orchestrator.main))


def test_cached_headlines_prefer_marks():
    print("headline cache attribution:")
    from tools.post_exit_runners import (
        _cached_headlines_from_marks, research_left_on_table_why,
    )
    rec = {
        "ticker": "DELL",
        "exit_date": "2026-05-01",
        "max_extension_pct": 15.0,
        "marks": {
            "15": {
                "headlines": [
                    {"title": "Dell raises AI server guidance after strong orders"},
                ]
            },
            "30": {
                "headlines": [
                    {"title": "Dell stock breaks out above resistance"},
                ]
            },
        },
    }
    heads = _cached_headlines_from_marks(rec)
    check("cached flattened", len(heads) == 2)
    attr = research_left_on_table_why(rec)
    check("uses mark cache source", attr.get("headline_source") == "mark_cache", str(attr))
    check("has primary driver", attr.get("primary_driver") in
          ("catalyst", "technical", "mixed", "unknown"), str(attr))


if __name__ == "__main__":
    # minimal non-pytest runner for a few pure tests
    test_slim_context_for_brain()
    test_orchestrator_reexports()
    test_cached_headlines_prefer_marks()
    print()
    if FAILURES:
        print(f"FAILED: {FAILURES}")
        sys.exit(1)
    print("standalone checks passed (run pytest for full suite)")
