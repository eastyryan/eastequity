"""A learning loop must report when it has stopped learning, and must not
describe itself as more binding than it is.

TWO FINDINGS (audited 2026-07-19)
---------------------------------
1. SILENT LEARNING DEATH. CLAUDE.md states a study session runs "every weekday
   after the close". The scheduling is real — scripts/run_cycle.sh chains --study
   on the weekday 17:30 slot — but across ~8 eligible weekdays only TWO sessions
   ever ran, both on 2026-07-17 six minutes apart, and one failed to parse. Yield:
   one lesson. NOTHING NOTICED. build_health counted missed runs and bundle age; a
   knowledge base that has not grown in a week looked identical to a well-curated
   one. Same failure class as the rest of this audit, applied to the learning layer.

2. OVERSOLD ENFORCEMENT. The shadow book sets binding=True at >=8 closed shadows
   and calls itself a "Binding caution", but NO validator rule reads it — `binding`
   resolved only to a different note string. It is the largest sample in the system
   (15 closed vs 2 real trades) and it enforces nothing.

   The fix is deliberately NOT a new gate. The verdict mix is 14 good_skip to 1
   regret_miss, so a rule derived from it would almost only ever argue for MORE
   skipping — and the asymmetry is partly structural, since a skip that would have
   hit its stop resolves fast while one that would have compounded needs the full
   window. Enforcing on a sample skewed that way would encode the bias as a rule.
   Say plainly that it is advisory and hand over the numbers to discount it with.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from runlib import analytics


# ---------------------------------------------------------------------------
# 1. the study loop reports when it goes quiet
# ---------------------------------------------------------------------------

@pytest.fixture
def kb(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(analytics, "ROOT", tmp_path)
    def _write(entries, updated_at=None):
        (tmp_path / "data" / "knowledge_base.json").write_text(json.dumps(
            {"entries": entries, "updated_at": updated_at}))
    return _write


def _lesson(days_ago: float) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"id": "KB-1", "discipline": "risk", "learned_at": ts.isoformat()}


def test_recent_lesson_is_not_stale(kb):
    kb([_lesson(1.0)])
    out = analytics.learning_loop_freshness()
    assert out["stale"] is False
    assert out["days_since_last_lesson"] == pytest.approx(1.0, abs=0.1)


def test_quiet_loop_is_flagged_stale(kb):
    """THE REGRESSION: a loop that stopped producing looked exactly like one that
    was merely well-curated."""
    kb([_lesson(9.0)])
    out = analytics.learning_loop_freshness()
    assert out["stale"] is True
    assert "9 days" in out["note"]
    assert "scheduler" in out["note"]


def test_stale_learning_surfaces_in_health(kb, monkeypatch):
    kb([_lesson(30.0)])
    monkeypatch.setattr(analytics, "expected_slots", lambda _w: [])
    health = analytics.build_health()
    assert health["learning_loop"]["stale"] is True
    assert "learning loop quiet" in health["status"]


def test_learning_warning_never_outranks_a_real_outage(kb, monkeypatch):
    """A quiet learning loop is a WARNING. It must not mask missed runs, and it
    must never gate trading — exits are never blocked on it."""
    kb([_lesson(30.0)])
    monkeypatch.setattr(analytics, "expected_slots", lambda _w: [0.0] * 10)
    health = analytics.build_health()
    assert "DEGRADED" in health["status"], "missed runs must win over a learning warning"


def test_entry_without_learned_at_falls_back_to_store_stamp(kb):
    """The single real lesson on disk has learned_at: None — that must not read
    as 'the loop never ran'."""
    ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    kb([{"id": "KB-1", "learned_at": None}], updated_at=ts)
    out = analytics.learning_loop_freshness()
    assert out["basis"] == "store_updated_at"
    assert out["days_since_last_lesson"] == pytest.approx(2.0, abs=0.1)


def test_unreadable_store_is_unknown_not_healthy(kb, monkeypatch, tmp_path):
    monkeypatch.setattr(analytics, "ROOT", tmp_path / "nope")
    out = analytics.learning_loop_freshness()
    assert out["stale"] is False and "unknown" in out["note"]


def test_empty_knowledge_base_is_not_reported_as_stale(kb):
    """Nothing to be stale about yet — a fresh install is not a broken loop."""
    kb([])
    out = analytics.learning_loop_freshness()
    assert out["n_lessons"] == 0
    assert out["stale"] is False


# ---------------------------------------------------------------------------
# 2. the shadow book does not oversell itself
# ---------------------------------------------------------------------------

@pytest.fixture
def shadow(tmp_path, monkeypatch):
    import tools.shadow_portfolio as SP
    f = tmp_path / "shadow.json"
    monkeypatch.setattr(SP, "SHADOW_FILE", f, raising=False)
    monkeypatch.setattr(SP, "_load", lambda: json.loads(f.read_text()))

    def _write(closed, positions=None):
        f.write_text(json.dumps({"positions": positions or [], "closed": closed}))
    return SP, _write


def _closed(verdict, *, sampled=False, ticker="AAA") -> dict:
    r = {"ticker": ticker, "verdict": verdict, "source": "rejected_idea",
         "max_upside_pct": 12, "max_drawdown_pct": -9}
    if sampled:
        r["touch_source"] = "sampled_only"
    return r


def test_shadow_book_declares_itself_advisory(shadow):
    SP, write = shadow
    write([_closed("good_skip") for _ in range(10)])
    out = SP.brain_facing_shadow_learning()
    assert out["binding"] is True
    assert out["enforcement"] == "advisory_only", (
        "'binding' with no validator rule reading it oversells the loop")
    assert "not code-enforced" in out["note"]
    assert "No validator rule" in out["enforcement_note"]


def test_lopsided_verdicts_are_disclosed(shadow):
    """THE REAL DISTRIBUTION: 14 good_skip to 1 regret_miss."""
    SP, write = shadow
    write([_closed("good_skip") for _ in range(14)] + [_closed("regret_miss")])
    out = SP.brain_facing_shadow_learning()
    assert out["verdict_distribution"] == {"good_skip": 14, "regret_miss": 1}
    assert out["verdict_skew_warning"]
    assert "FOMO suppressant" in out["verdict_skew_warning"]


def test_balanced_verdicts_raise_no_skew_warning(shadow):
    SP, write = shadow
    write([_closed("good_skip") for _ in range(6)]
          + [_closed("regret_miss") for _ in range(6)])
    out = SP.brain_facing_shadow_learning()
    assert out["verdict_skew_warning"] is None


def test_measurement_quality_is_disclosed(shadow):
    """8 of 15 real closed shadows were resolved on sampled marks only."""
    SP, write = shadow
    write([_closed("good_skip", sampled=True) for _ in range(8)]
          + [_closed("good_skip") for _ in range(7)])
    mq = SP.brain_facing_shadow_learning()["measurement_quality"]
    assert mq["sampled_only"] == 8
    assert "not bar-verified" in mq["note"]


def test_warming_up_book_is_not_called_binding(shadow):
    SP, write = shadow
    write([_closed("good_skip") for _ in range(3)])
    out = SP.brain_facing_shadow_learning()
    assert out["binding"] is False
    assert "Warming up" in out["note"]


# ---------------------------------------------------------------------------
# 3. the disclosures must SURVIVE into the pack the brain reads
# ---------------------------------------------------------------------------

def test_shadow_disclosures_reach_the_brain_pack(monkeypatch):
    """CAUGHT IN INTEGRATION, 2026-07-19, and it is the failure class this whole
    audit is about: compact_learning_pack's `shadow` block is a cherry-picking
    ALLOWLIST. The enforcement/skew/measurement caveats were added to
    shadow_portfolio.py and then silently dropped on the way to the brain — the
    warning existed and never arrived, exactly like factor_map being present in
    the archive and stripped from the slim pack.

    A caveat the brain cannot read is not a caveat.
    """
    import tools.shadow_portfolio as SP
    from runlib.context_tiers import slim_context_for_brain

    monkeypatch.setattr(SP, "brain_facing_shadow_learning", lambda *_a, **_k: {
        "binding": True,
        "enforcement": "advisory_only",
        "enforcement_note": "No validator rule reads the shadow book.",
        "verdict_distribution": {"good_skip": 14, "regret_miss": 1},
        "verdict_skew_warning": "VERDICT SKEW: 14 good_skips vs 1 regret_miss.",
        "measurement_quality": {"sampled_only": 8, "note": "not bar-verified"},
        "regret_misses": [], "good_skips": [], "open_running_best": [], "stats": {},
    })

    # No shadow_learning in the bundle -> compact_learning_pack calls the live fn.
    pack = slim_context_for_brain({"reasoning_process": {}, "portfolio": {}})
    sh = pack["reasoning_process"]["learning_pack"]["shadow"]

    for field in ("enforcement", "enforcement_note", "verdict_distribution",
                  "verdict_skew_warning", "measurement_quality"):
        assert sh.get(field) is not None, (
            f"{field} was dropped by the learning-pack allowlist — the brain never "
            f"sees this disclosure")
    assert sh["verdict_distribution"]["good_skip"] == 14
    assert "SKEW" in sh["verdict_skew_warning"]
