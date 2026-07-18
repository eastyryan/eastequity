"""The learning loops must be able to produce evidence, and to say when they can't.

Two structural failures pinned here.

SHADOW BOOK was mathematically incapable of ever closing a position. The cap was 80
with `positions[-80:]` — a tail slice keeping the NEWEST and discarding the OLDEST,
i.e. exactly the shadows closest to the 30-day resolution threshold. At the observed
~9.6 opens/day nothing survived past ~8 days while closing needs 30. Live state was
48 open / 0 closed, `binding` (n_closed >= 8) unreachable, regret_rate_pct
permanently None. Dedupe on (ticker, source) also opened up to 3 shadows per skip.

KNOWLEDGE BASE had no memory. data/knowledge_base.json was never committed by
runlib/publish.py (untracked, and not gitignored) while its derived public journal
WAS — so a study session ran, published a lesson, and the source of truth vanished.
Every API here is fail-soft and returns 0 on a missing store, so a DEAD loop looked
exactly like a quiet one.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.knowledge_base as kb  # noqa: E402
import tools.shadow_portfolio as sp  # noqa: E402


# --------------------------------------------------------------------------- #
# Shadow book capacity
# --------------------------------------------------------------------------- #
def test_cap_exceeds_the_full_horizon_at_the_observed_open_rate():
    """THE arithmetic that made the loop dead. ~9.6 opens/day against a 90-day
    horizon needs >= ~865 slots; the old cap was 80, i.e. ~8 days."""
    observed_opens_per_day = 9.6
    survivable_days = sp.MAX_OPEN_SHADOWS / observed_opens_per_day
    assert survivable_days >= sp.HORIZON_DAYS, (
        f"cap {sp.MAX_OPEN_SHADOWS} survives only {survivable_days:.0f} days but "
        f"shadows need {sp.HORIZON_DAYS} to resolve — the book can never close one")


def test_eviction_keeps_the_oldest_not_the_newest():
    """The old tail slice deleted the observations about to become informative."""
    sp_cap = sp.MAX_OPEN_SHADOWS
    try:
        sp.MAX_OPEN_SHADOWS = 3
        positions = [{"id": i, "opened_at": f"2026-07-{i:02d}"} for i in range(1, 11)]
        kept = sp._evict(positions)
        assert [p["id"] for p in kept] == [1, 2, 3], "eviction must drop the YOUNGEST"
    finally:
        sp.MAX_OPEN_SHADOWS = sp_cap


def test_eviction_is_a_noop_under_the_cap():
    positions = [{"id": i, "opened_at": f"2026-07-{i:02d}"} for i in range(1, 5)]
    assert sp._evict(positions) == positions


# --------------------------------------------------------------------------- #
# Shadow book dedupe
# --------------------------------------------------------------------------- #
@pytest.fixture
def shadow(tmp_path, monkeypatch):
    f = tmp_path / "shadow.json"
    monkeypatch.setattr(sp, "SHADOW_FILE", f)
    return f


def test_one_shadow_per_ticker_regardless_of_source(shadow):
    """48 open shadows covered only 28 tickers because the key was (ticker, source).
    regret_rate_pct divides by shadow count, so watched names got up to 3x weight in
    the agent's own regret statistics."""
    assert sp.open_shadow(ticker="AMD", entry_price=100.0, source="rejected_idea", reason="r") is not None
    assert sp.open_shadow(ticker="AMD", entry_price=101.0, source="watchlist_hold", reason="r") is None
    assert sp.open_shadow(ticker="AMD", entry_price=102.0, source="watchlist_trigger", reason="r") is None

    state = json.loads(shadow.read_text())
    assert len([p for p in state["positions"] if p["ticker"] == "AMD"]) == 1


def test_distinct_tickers_still_open(shadow):
    assert sp.open_shadow(ticker="AMD", entry_price=100.0, source="rejected_idea", reason="r")
    assert sp.open_shadow(ticker="NVDA", entry_price=200.0, source="rejected_idea", reason="r")
    assert len(json.loads(shadow.read_text())["positions"]) == 2


def test_the_same_ticker_reopens_after_the_dedupe_window(shadow, monkeypatch):
    sp.open_shadow(ticker="AMD", entry_price=100.0, source="rejected_idea", reason="r")
    state = json.loads(shadow.read_text())
    old = (datetime.now(timezone.utc) - timedelta(days=sp.DEDUPE_DAYS + 1)).date().isoformat()
    state["positions"][0]["opened_at"] = old
    shadow.write_text(json.dumps(state))

    assert sp.open_shadow(ticker="AMD", entry_price=110.0, source="rejected_idea", reason="r") is not None


# --------------------------------------------------------------------------- #
# Knowledge base persistence
# --------------------------------------------------------------------------- #
def test_store_health_flags_a_missing_store_with_published_lessons(tmp_path, monkeypatch):
    """THE real failure: lessons published, store gone, every grading call silently
    returning 0."""
    monkeypatch.setattr(kb, "KB_JSON", tmp_path / "knowledge_base.json")
    journal = tmp_path / "learning_journal.json"
    journal.write_text(json.dumps({"updated_at": "x", "entries": [{"id": "KB-1"}]}))
    monkeypatch.setattr(kb, "JOURNAL_JSON", journal)

    h = kb.store_health()
    assert h["ok"] is False
    assert h["status"] == "store_missing_but_lessons_published"
    assert h["n_published"] == 1 and h["n_entries"] == 0


def test_store_health_reads_the_real_journal_key(tmp_path, monkeypatch):
    """The journal writes {"updated_at","note","discipline_counts","entries"}.
    Guessing "lessons" made the check report a cheerful zero — the exact silent-zero
    failure it exists to catch."""
    monkeypatch.setattr(kb, "KB_JSON", tmp_path / "kb.json")
    journal = tmp_path / "j.json"
    journal.write_text(json.dumps({"note": "x", "entries": [1, 2, 3]}))
    monkeypatch.setattr(kb, "JOURNAL_JSON", journal)
    assert kb.store_health()["n_published"] == 3


def test_store_health_flags_a_store_behind_the_journal(tmp_path, monkeypatch):
    store = tmp_path / "kb.json"
    store.write_text(json.dumps({"entries": [{"id": "KB-1"}]}))
    monkeypatch.setattr(kb, "KB_JSON", store)
    journal = tmp_path / "j.json"
    journal.write_text(json.dumps({"entries": [{"id": "KB-1"}, {"id": "KB-2"}]}))
    monkeypatch.setattr(kb, "JOURNAL_JSON", journal)

    h = kb.store_health()
    assert h["ok"] is False and h["status"] == "store_behind_published"


def test_store_health_ok_when_consistent(tmp_path, monkeypatch):
    store = tmp_path / "kb.json"
    store.write_text(json.dumps({"entries": [{"id": "KB-1"}, {"id": "KB-2"}]}))
    monkeypatch.setattr(kb, "KB_JSON", store)
    journal = tmp_path / "j.json"
    journal.write_text(json.dumps({"entries": [{"id": "KB-1"}]}))
    monkeypatch.setattr(kb, "JOURNAL_JSON", journal)
    assert kb.store_health()["ok"] is True


def test_kb_save_is_atomic(tmp_path, monkeypatch):
    store = tmp_path / "kb.json"
    monkeypatch.setattr(kb, "KB_JSON", store)
    kb._save({"entries": [{"id": "KB-1"}]})
    assert json.loads(store.read_text())["entries"][0]["id"] == "KB-1"
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_publish_commits_the_learning_stores():
    """Root cause: publish.py listed cusip_map.json and ai_exposure.json but no
    learning store, so on an ephemeral cloud runner every one was rebuilt empty."""
    src = (Path(__file__).resolve().parent.parent / "runlib" / "publish.py").read_text()
    for name in ("knowledge_base.json", "adopted_lessons.json",
                 "shadow_portfolio.json", "concept_memory"):
        assert name in src, f"publish.py must persist data/{name}"
