"""Fail-soft X memo operator_notes / book_events plumbing (no LLM)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib import publish


def test_draft_fallback_includes_book_events(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    context = {
        "portfolio": {"total_equity_usd": 10000},
        "book_events": [{"type": "reset", "detail": "book reset to cash"}],
        "operator_notes": ["brain migrate complete"],
        "forced_exits": [],
    }
    publish.draft_x_summary(
        fills=[], results=[], context=context, run_id="RID1",
        operator_notes=context["operator_notes"],
        book_events=context["book_events"],
    )
    text = (tmp_path / "state" / "x_draft_RID1.txt").read_text()
    assert "Operator / book context" in text
    assert "reset" in text
    assert "brain migrate" in text


def test_brain_memo_facts_include_events(monkeypatch):
    captured = {}

    def fake_claude(prompt, call=""):
        captured["prompt"] = prompt
        return "x" * 250  # pass length gate

    monkeypatch.setattr("runlib.brain_io.run_claude", fake_claude, raising=False)
    # Patch where it's imported inside the function
    import runlib.brain_io as bio
    monkeypatch.setattr(bio, "run_claude", fake_claude)

    ctx = {
        "portfolio": {"total_equity_usd": 1},
        "book_events": [{"type": "kill_switch"}],
        "operator_notes": ["halted for ops"],
        "forced_exits": [],
    }
    memo = publish._brain_trade_memo(
        [{"ticker": "AMD", "action": "BUY", "fill_price": 100, "quantity": 1}],
        ctx,
        operator_notes=ctx["operator_notes"],
        book_events=ctx["book_events"],
    )
    assert memo and len(memo) >= 200
    assert "MATERIAL BOOK EVENTS" in captured["prompt"]
    assert "kill_switch" in captured["prompt"]
    assert "operator_notes" in captured["prompt"]
