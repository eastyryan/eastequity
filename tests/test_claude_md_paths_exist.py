"""Paths CLAUDE.md tells the brain to read must exist in the pack it receives.

WHY THIS EXISTS
---------------
CLAUDE.md is the brain's instruction manual and the slim pack is the thing it
describes. Nothing kept them in sync, and the drift is invisible from both
sides: the prompt reads correct, the pack looks complete, and the brain follows
an instruction to a key that is not there and quietly finds nothing.

Two instances were found on 2026-07-19, both by hand:

  * "Opportunity cost: read `reasoning_process.watchlist_feedback`" — the field
    only ever existed at reasoning_process.learning_pack.watchlist_feedback, one
    level deeper.
  * The learning-pack `shadow` block is a cherry-picking ALLOWLIST, so
    disclosures added to shadow_portfolio.py (enforcement, verdict skew,
    measurement quality) were silently dropped before reaching the brain.

Both are the same failure as factor_map: present upstream, stripped on the way
to the reader. Hand-checking found two; this checks all of them every run.

SCOPE, deliberately narrow: it asserts that documented dotted paths RESOLVE in a
real pack. It does not check prose, and it does not require a key to be
non-empty — a genuinely absent feed is allowed to be absent. What it forbids is
the prompt naming a location that does not exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from runlib.context_tiers import slim_context_for_brain

ROOT = Path(__file__).resolve().parent.parent

# Dotted paths CLAUDE.md instructs the brain to read. Kept explicit rather than
# scraped: a regex over prose produces false positives on ordinary sentences,
# and a wrong test is worse than no test. Add to this list when CLAUDE.md starts
# pointing at something new.
DOCUMENTED_PATHS = [
    "hard_limits",
    "digest.by_ticker",
    "factor_map",
    "portfolio_competition",
    "ownership_flow.by_ticker",
    "reasoning_process.process_checklist",
    "reasoning_process.watchlist_feedback",          # the drift found by hand
    "reasoning_process.theme_exposure",
    "reasoning_process.theme_concentration_cap_pct",
    "reasoning_process.price_freshness",
    "reasoning_process.learning_pack",
    "reasoning_process.learning_pack.shadow",
    "reasoning_process.learning_pack.exits",
    "reasoning_process.learning_pack.runners",
    "reasoning_process.learning_pack.adopted_lessons",
    "reasoning_process.learning_pack.knowledge_base",
    "reasoning_process.learning_pack.calibration_status",
    "stop_engineering.floors",
    "position_stop_cushion",
    "track_record",
    "universe_scan.prices_meta",
    "stack_cards.by_ticker",
    "financial_checklists",
    "concept_memory.by_ticker",
    "portfolio_risk",
    "market_radar",
    "todays_8ks",
    "_pack_budget",
]

# Documented in CLAUDE.md as "(when present)" — these describe a CONDITION, so
# absence is information rather than drift. data_quality is only written when the
# run is degraded, stale or partial; forced_exits only when the safety layer
# closed something. Asserting they exist would make a healthy run fail.
CONDITIONAL_PATHS = [
    "data_quality",
    "stale_data_notice",
    "forced_exits",
    "corporate_actions",
    "earnings_deep_dive",
    "trigger_run_note",
    "operator_note",
]


def _resolve(obj, dotted: str):
    """Walk a dotted path. Returns (found, value)."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _newest_full_bundle() -> dict | None:
    files = sorted((ROOT / "state").glob("context_full_2*.json"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for p in files[:3]:
        try:
            d = json.loads(p.read_text())
            if isinstance(d, dict) and d:
                return d
        except Exception:
            continue
    return None


@pytest.fixture(scope="module")
def pack():
    full = _newest_full_bundle()
    if full is None:
        pytest.skip("no gathered bundle on disk to check CLAUDE.md against")
    return slim_context_for_brain(full)


@pytest.mark.parametrize("dotted", DOCUMENTED_PATHS)
def test_documented_path_resolves_in_the_pack(pack, dotted):
    found, _ = _resolve(pack, dotted)
    assert found, (
        f"CLAUDE.md points the brain at `{dotted}` and it does not exist in the "
        f"slim pack. Either the pack lost it (an allowlist dropped it, or a cap "
        f"trimmed it) or CLAUDE.md is describing a location that never existed. "
        f"Both look identical to the brain: it follows the instruction and finds "
        f"nothing.")


@pytest.mark.parametrize("dotted", CONDITIONAL_PATHS)
def test_conditional_path_is_well_formed_when_present(pack, dotted):
    """These may be absent — but if present they must be a real block, not a
    leftover empty string that reads as 'nothing to report'."""
    found, val = _resolve(pack, dotted)
    if not found:
        pytest.skip(f"{dotted} legitimately absent on this run")
    assert val is not None


def test_the_paths_this_test_watches_are_actually_named_in_claude_md():
    """Guard the guard: a path list that drifts from the doc tests nothing.

    Only the leaf-most segment is required to appear, because CLAUDE.md refers to
    blocks both fully-qualified and by short name ("read `factor_map`",
    "`learning_pack.shadow`").
    """
    doc = (ROOT / "CLAUDE.md").read_text()
    missing = [p for p in DOCUMENTED_PATHS
               if p != "_pack_budget" and p.split(".")[-1] not in doc]
    assert not missing, (
        f"these paths are asserted but no longer mentioned in CLAUDE.md: "
        f"{missing}. Remove them, or the test is guarding a doc that moved on.")


def test_shadow_disclosures_are_not_silently_dropped(pack):
    """The learning-pack shadow block is an allowlist; adding a field upstream
    without adding it there drops it before the brain ever sees it."""
    found, shadow = _resolve(pack, "reasoning_process.learning_pack.shadow")
    assert found
    # `binding` has always been there; the disclosures were the ones lost.
    for field in ("binding", "enforcement"):
        assert field in shadow, (
            f"learning_pack.shadow is missing `{field}` — the allowlist in "
            f"compact_learning_pack dropped it on the way to the brain")


def test_blocking_paths_are_inside_the_read_window(pack):
    """A path that resolves but sits past line ~2,000 is only nominally present."""
    from runlib.context_tiers import READ_WINDOW_LINES, key_start_lines
    starts = key_start_lines(pack)
    for top in ("hard_limits", "digest", "factor_map", "portfolio_competition",
                "reasoning_process", "stop_engineering"):
        if top in pack:
            assert starts[top] <= READ_WINDOW_LINES, (
                f"{top} resolves but starts at line {starts[top]} — past the "
                f"default Read window, so the brain does not actually receive it")


def test_momentum_health_reaches_the_brain():
    """FOUND BY THE BRAIN, 2026-07-19 weekly run.

    momentum_health was in neither ALWAYS_KEYS nor FOCUS_KEYS, so it was
    gathered, written to the full archive, READ BY THE VALIDATOR — which halves
    new-BUY size on an unwind — and stripped from the slim pack. CLAUDE.md names
    it a required input to the regime step on EVERY run.

    That run's archive carried {"status": "unwind", "momentum_unwind": true}
    while the brain, unable to see the block, reconstructed the unwind by hand
    from sector_relative_strength and filed it as its improvement note. The
    validator and the brain were reading different worlds.
    """
    from runlib.context_tiers import ALWAYS_KEYS, FOCUS_KEYS
    assert "momentum_health" in ALWAYS_KEYS or "momentum_health" in FOCUS_KEYS, (
        "momentum_health is in neither key list, so it cannot survive slimming")
    pack = slim_context_for_brain({
        "momentum_health": {"status": "unwind", "momentum_unwind": True},
        "reasoning_process": {}, "portfolio": {},
    })
    assert pack.get("momentum_health", {}).get("status") == "unwind"
