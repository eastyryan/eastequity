"""Offline tests for tools.research_feedback (temp outcomes file via ROOT patch).

    python3 tests/test_research_feedback.py
    python3 -m pytest tests/test_research_feedback.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.research_feedback as RF  # noqa: E402


SAMPLE = [
    {
        "ticker": "AMD",
        "first_watched": "2026-07-13",
        "price_when_added": 557.89,
        "hit_buy_level": True,
        "currently_watched": True,
        "would_buy_at": "near $555",
        "one_line": "At buy level",
        "latest_price": 548.13,
        "move_pct_since_watched": -1.7,
        "acted": False,
        "parsed_level": 555.0,
        "hit_date": "2026-07-14",
    },
    {
        "ticker": "OKTA",
        "first_watched": "2026-07-13",
        "price_when_added": 141.28,
        "hit_buy_level": False,
        "currently_watched": False,
        "would_buy_at": "pullback",
        "one_line": "Cheapest cyber",
        "latest_price": 154.62,
        "move_pct_since_watched": 9.4,
        "acted": False,
        "dropped_date": "2026-07-13",
    },
    {
        "ticker": "S",
        "first_watched": "2026-07-13",
        "price_when_added": 17.88,
        "hit_buy_level": True,
        "currently_watched": False,
        "would_buy_at": "near $19",
        "one_line": "Diversifier",
        "latest_price": 19.92,
        "move_pct_since_watched": 11.4,
        "acted": False,
        "hit_date": "2026-07-14",
        "dropped_date": "2026-07-13",
    },
    {
        "ticker": "MU",
        "first_watched": "2026-07-13",
        "price_when_added": 937.0,
        "hit_buy_level": False,
        "currently_watched": True,
        "would_buy_at": "50-DMA",
        "one_line": "Memory cycle",
        "latest_price": 983.12,
        "move_pct_since_watched": 4.9,  # just under large-move threshold
        "acted": False,
    },
    {
        "ticker": "NVDA",
        "first_watched": "2026-07-10",
        "price_when_added": 100.0,
        "hit_buy_level": True,
        "currently_watched": True,
        "would_buy_at": "near $100",
        "one_line": "Bought later",
        "latest_price": 120.0,
        "move_pct_since_watched": 20.0,
        "acted": True,
        "hit_date": "2026-07-11",
    },
]


@pytest.fixture
def outcomes_root(tmp_path, monkeypatch):
    data_dir = tmp_path / "dashboard" / "data"
    data_dir.mkdir(parents=True)
    path = data_dir / "watchlist_outcomes.json"
    path.write_text(json.dumps(SAMPLE))
    monkeypatch.setattr(RF, "ROOT", tmp_path)
    monkeypatch.setattr(RF, "OUTCOMES_FILE", path)
    return path


def test_load_watchlist_outcomes(outcomes_root):
    rows = RF.load_watchlist_outcomes()
    assert len(rows) == 5
    assert rows[0]["ticker"] == "AMD"


def test_load_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(RF, "OUTCOMES_FILE", tmp_path / "missing.json")
    assert RF.load_watchlist_outcomes() == []


def test_load_corrupt_file(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(RF, "OUTCOMES_FILE", bad)
    assert RF.load_watchlist_outcomes() == []


def test_brain_facing_watchlist_feedback_shapes(outcomes_root):
    out = RF.brain_facing_watchlist_feedback(limit=12)
    assert "note" in out
    assert "opportunity_cost" in out
    assert "hits_not_bought" in out
    assert "stats" in out

    hit_tickers = {r["ticker"] for r in out["hits_not_bought"]}
    # AMD: currently watched + hit + not acted
    assert "AMD" in hit_tickers
    # NVDA acted — must not appear
    assert "NVDA" not in hit_tickers
    # S is dropped (not currently watched) — not in hits_not_bought
    assert "S" not in hit_tickers

    oc_tickers = {r["ticker"] for r in out["opportunity_cost"]}
    # OKTA dropped + 9.4% move, never bought
    assert "OKTA" in oc_tickers
    # S dropped + 11.4% + hit — large move
    assert "S" in oc_tickers
    # NVDA acted — not opportunity cost
    assert "NVDA" not in oc_tickers
    # MU 4.9% under default 5% threshold and no hit — not OC
    assert "MU" not in oc_tickers

    stats = out["stats"]
    assert stats["tracked"] == 5
    assert stats["acted"] == 1
    assert stats["hit_buy_level"] == 3
    assert stats["hit_buy_level_not_acted_still_watched"] == 1
    assert stats["large_move_threshold_pct"] == 5.0


def test_opportunity_cost_sorted_by_abs_move(outcomes_root):
    out = RF.brain_facing_watchlist_feedback()
    moves = [abs(float(r["move_pct_since_watched"])) for r in out["opportunity_cost"]]
    assert moves == sorted(moves, reverse=True)
    # S (11.4) before OKTA (9.4)
    assert out["opportunity_cost"][0]["ticker"] == "S"


def test_limit_truncates(outcomes_root):
    out = RF.brain_facing_watchlist_feedback(limit=1)
    assert len(out["opportunity_cost"]) <= 1
    assert len(out["hits_not_bought"]) <= 1


def test_process_checklist_static_order():
    cl = RF.brain_facing_process_checklist()
    assert cl["order"] == ["regime", "book", "idea", "geometry", "kill"]
    assert len(cl["steps"]) == 5
    ids = [s["id"] for s in cl["steps"]]
    assert ids == cl["order"]
    for step in cl["steps"]:
        assert "title" in step
        assert "must" in step
        assert isinstance(step.get("context_keys"), list)
    assert "note" in cl


def test_feedback_fail_soft_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(RF, "OUTCOMES_FILE", tmp_path / "none.json")
    out = RF.brain_facing_watchlist_feedback()
    assert out["opportunity_cost"] == []
    assert out["hits_not_bought"] == []
    assert out["stats"]["tracked"] == 0


# ---------------------------------------------------------------------------
# Script mode
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    fails = 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = root / "dashboard" / "data" / "watchlist_outcomes.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(SAMPLE))
        RF.ROOT = root
        RF.OUTCOMES_FILE = path

        rows = RF.load_watchlist_outcomes()
        ok = len(rows) == 5
        print(f"  [{'PASS' if ok else 'FAIL'}] load outcomes")
        fails += 0 if ok else 1

        fb = RF.brain_facing_watchlist_feedback()
        ok = (
            any(r["ticker"] == "AMD" for r in fb["hits_not_bought"])
            and any(r["ticker"] == "OKTA" for r in fb["opportunity_cost"])
        )
        print(f"  [{'PASS' if ok else 'FAIL'}] feedback buckets")
        fails += 0 if ok else 1

        cl = RF.brain_facing_process_checklist()
        ok = cl["order"] == ["regime", "book", "idea", "geometry", "kill"]
        print(f"  [{'PASS' if ok else 'FAIL'}] process checklist")
        fails += 0 if ok else 1

    sys.exit(1 if fails else 0)
