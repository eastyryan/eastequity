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
import sys
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
    "universe_scan.indicators_by_ticker",
    "stack_cards.by_ticker",
    "financial_checklists",
    "concept_memory.by_ticker",
    "portfolio_risk",
    "market_radar",
    "market_breadth.universe",
    "market_breadth.sector_rotation_30d",
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


def _newest_bundle_path():
    files = sorted((ROOT / "state").glob("context_full_2*.json"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _gather_sources():
    """Every repo source file that can actually change what a gather emits.

    DISCOVERED, not listed. An earlier version walked all of tools/ and runlib/,
    which tripped on modules that cannot touch the bundle at all — the backtest
    helpers in tools/signal_lanes.py, for one. A staleness alarm that fires on
    unrelated edits gets muted, and a muted guard is the same as no guard, which
    is the failure this file exists to prevent. So the watched set is derived
    from what context_gather actually imports rather than from a directory that
    happens to contain it.

    Discovery runs in a SUBPROCESS, which is not fussiness. sys.modules is
    global and pytest shares one interpreter across the whole suite, so reading
    it in-process picks up whatever every other test file happened to import
    first — the discovered set then depends on test execution ORDER, and this
    check passed alone while failing under the full suite. A guard whose result
    depends on what ran before it is not a guard.
    """
    import subprocess
    probe = (
        "import sys, json;"
        "import runlib.context_gather, runlib.context_tiers;"
        "print(json.dumps([m.__file__ for m in list(sys.modules.values())"
        " if getattr(m, '__file__', None)]))"
    )
    try:
        r = subprocess.run([sys.executable, "-c", probe],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    out = []
    for f in json.loads(r.stdout.strip().splitlines()[-1]):
        p = Path(f)
        try:
            p.relative_to(ROOT)
        except ValueError:
            continue                       # third-party / stdlib
        if ".venv" in p.parts or "tests" in p.parts:
            continue
        out.append(p)
    return out


def _last_commit_time(paths) -> tuple[float, str] | None:
    """(unix time, subject) of the newest COMMIT touching any of `paths`.

    Commit time, not file mtime. mtime measures when a file was last WRITTEN,
    which `touch`, `git checkout`, and every fresh CI clone all change without
    changing a byte of content — a staleness alarm keyed on it fires constantly
    in CI and gets muted, which is the failure mode this guard exists to avoid.
    Commit time is content-anchored and survives a clone.

    The tradeoff is deliberate: uncommitted local edits to the gatherer will not
    trip this. That is the right call — the question being asked is whether the
    committed gatherer has moved past the fixture, and a dirty working tree is
    the developer's own business.
    """
    import subprocess
    rel = [str(Path(p).relative_to(ROOT)) for p in paths]
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ct%x09%s", "--"] + rel,
            cwd=ROOT, capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    ts, _, subject = r.stdout.strip().partition("\t")
    try:
        return float(ts), subject
    except ValueError:
        return None


def _fixture_staleness():
    """(is_stale, message). The bundle is evidence; evidence can go out of date."""
    bundle = _newest_bundle_path()
    sources = _gather_sources()
    if bundle is None or not sources:
        return False, ""
    commit = _last_commit_time(sources)
    if commit is None:
        return False, ""          # no git history to compare against: say nothing
    ts, subject = commit
    if bundle.stat().st_mtime >= ts:
        return False, ""
    return True, (
        f"the newest bundle ({bundle.name}) predates the last commit to the "
        f"gather code (\"{subject[:60]}\") — run a gather to refresh the fixture")


@pytest.mark.parametrize("dotted", DOCUMENTED_PATHS)
def test_documented_path_resolves_in_the_pack(pack, dotted):
    found, _ = _resolve(pack, dotted)
    stale, why = _fixture_staleness()
    assert found, (
        f"CLAUDE.md points the brain at `{dotted}` and it does not exist in the "
        f"slim pack. Either the pack lost it (an allowlist dropped it, or a cap "
        f"trimmed it) or CLAUDE.md is describing a location that never existed. "
        f"Both look identical to the brain: it follows the instruction and finds "
        f"nothing."
        + (f"\n\nNOTE: {why}. A path added to the gatherer after this bundle was "
           f"written will fail here until a fresh run exists — check that before "
           f"concluding the pack drops it." if stale else ""))


def test_the_bundle_this_file_checks_against_is_not_older_than_the_code():
    """The fixture is EVIDENCE, and stale evidence fails in both directions.

    Every assertion in this file resolves paths against the newest bundle found
    on disk. That bundle is produced by tools/ and runlib/, so when it predates
    them the check is no longer measuring the current system:

      * a path ADDED to the gatherer fails here though the pack is correct
        (loud, survivable — it is what surfaced this test);
      * a path REMOVED from the gatherer still resolves in the old bundle and
        PASSES. That is the dangerous direction, and it is silent.

    The second is this repo's recurring failure — a guard that cannot fire
    (reconciles_with_ledger was tested and never called for three days). A guard
    whose fixture has drifted is the same thing wearing a green tick, so the
    drift itself is asserted rather than assumed away.
    """
    stale, why = _fixture_staleness()
    assert not stale, (
        f"{why}. Until then this file cannot verify the CLAUDE.md contract: a "
        f"documented path deleted from the gatherer would still resolve in the "
        f"old bundle and pass. Do not skip this — re-gather.")


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
