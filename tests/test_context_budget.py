"""The brain must be able to READ the inputs it is blocked on.

THE INCIDENT
------------
Measured 2026-07-19 on state/context_20260719-a0990f.json (53,623 lines):

    hard_limits            line     10   read
    digest                 line    170   read
    stack_cards            line 45,296   never read
    factor_map             line 50,322   never read  <- requires_factor_response
    portfolio_competition  line 50,347   never read  <- drives seat_reviews
    reasoning_process      line 52,928   never read  <- the entire learning_pack

The brain reads the pack with the Read tool, which returns ~2,000 lines. Both
process gates reject EVERY BUY in a run when unanswered, so the system was
blocking itself on questions it could not see, and all six learning loops were
writing to an address the brain never read.

Byte-size budgets cannot catch this. The pack was ~1.9MB before and the old
tiering "saved" 1.25% — the number moved and the bug was untouched, because
bytes were never what truncated the brain. THE BUDGET UNIT IS LINES.

WHAT THESE TESTS PIN
--------------------
1. Every BLOCKING_KEYS entry starts before READ_WINDOW_LINES.  <- the bug
2. The pack cannot silently regrow past MAX_PACK_LINES.
3. Every cap discloses what it dropped (no silent omissions).
4. The caps preserve the specific fields downstream code and CLAUDE.md depend on.

Test 1 is the one that matters. If it fails, the brain is flying blind on a gate
that will reject its own proposals — do not raise the constant to make it pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runlib.context_tiers import (
    BLOCKING_KEYS,
    DECISION_CRITICAL,
    MAX_PACK_LINES,
    READ_WINDOW_LINES,
    key_start_lines,
    pack_line_count,
    slim_context_for_brain,
)

ROOT = Path(__file__).resolve().parent.parent


def _newest_full_bundle() -> dict | None:
    """Newest full archive on disk, or None when the repo has no runs yet."""
    files = sorted(
        (ROOT / "state").glob("context_full_2*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for path in files[:3]:
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data:
                return data
        except Exception:
            continue
    return None


def _synthetic_bundle() -> dict:
    """A bundle with every blocking key present and bulky research maps.

    Deliberately oversized on the research side: the ordering guarantee must
    hold because of insertion order, not because the test data happened to be
    small. Without the bulk this test would pass on a pack that fails in prod.
    """
    tickers = [f"TK{i:02d}" for i in range(40)]
    return {
        "run_date": "2026-07-19", "as_of_et": "2026-07-19", "trading_mode": "paper",
        "run_depth": "full", "run_depth_note": "n/a", "allows_new_buys": True,
        "hard_limits": {f"limit_{i}": {"value": i, "note": "x" * 40} for i in range(40)},
        "digest": {"note": "READ FIRST", "by_ticker": {
            t: {f"f{i}": i for i in range(20)} for t in tickers}},
        "factor_map": {"requires_factor_response": True, "concentration_level": "high"},
        "portfolio_competition": {"by_ticker": {t: {"competition_hint": "contested"}
                                                for t in tickers[:5]}},
        "reasoning_process": {"process_checklist": [{"id": f"s{i}"} for i in range(8)],
                              "price_freshness": {"prices_meta_sample": {
                                  t: {"price_as_of": "2026-07-18"} for t in tickers}}},
        "portfolio": {"positions": [{"ticker": t, "mv": 100} for t in tickers[:5]]},
        "stop_engineering": {"floors": {t: {"min_stop_distance_pct": 0.05}
                                        for t in tickers}},
        "position_stop_cushion": {"by_ticker": {}},
        "risk_halts": [], "forced_exits": [], "corporate_actions": [],
        "data_quality": {"stale": False}, "price_freshness_live": {"stale": False},
        "fundamentals_freshness": {
            "by_ticker": {t: {"current_through": "2026-03-31", "stale": False}
                          for t in tickers},
            "stale_tickers": []},
        "track_record": {"closed_trades": [], "performance": {}},
        "macro_regime": {"score": 1}, "market_events": {"vix": 15},
        # Bulk research, all of it past the window by construction.
        "deep_fundamentals": {t: {
            "status": "ok", "ticker": t,
            "quality_ratios": {"ratios": {"market_cap_usd": {"value": 5e9}}},
            "quarterly_facts": {f"series_{i}": [{"v": i, "end": "2026-03-31"}] * 8
                                for i in range(12)},
        } for t in tickers},
        "sec_filings": {t: {
            "status": "ok", "ticker": t, "fundamentals_current_through": "2026-03-31",
            "recent_filings": [{"form": "8-K", "filed": "2026-07-0%d" % (i % 9)}
                               for i in range(20)],
            "quarterly_fundamentals": {f"line_{i}": [{"val": i}] * 10 for i in range(9)},
        } for t in tickers},
        "filing_texts": {t: {
            "mdna": {"excerpt": "x" * 9000, "section": "mdna_results_of_operations"},
            "earnings_release": {"excerpt": "y" * 9000,
                                 "contains_guidance_language": True},
        } for t in tickers},
        "ownership_flow": {"by_ticker": {t: {
            "ticker": t, "alignment": "institutions_buying_price_rising",
            "read_for_swing": "sponsorship agrees with trend",
            "institutional": {"detail": ["z"] * 30},
            "retail_proxy": {"detail": ["z"] * 20},
        } for t in tickers}},
    }


@pytest.fixture(scope="module")
def packs():
    """Slim packs from the synthetic bundle and, when present, the newest real one."""
    out = [("synthetic", slim_context_for_brain(_synthetic_bundle()))]
    real = _newest_full_bundle()
    if real is not None:
        out.append(("newest-real-bundle", slim_context_for_brain(real)))
    return out


# ---------------------------------------------------------------------------
# 1. THE INVARIANT — blocking inputs must be inside the read window.
# ---------------------------------------------------------------------------

def test_blocking_keys_are_inside_the_read_window(packs):
    for label, pack in packs:
        starts = key_start_lines(pack)
        late = {k: starts[k] for k in BLOCKING_KEYS
                if k in pack and starts[k] > READ_WINDOW_LINES}
        assert not late, (
            f"[{label}] blocking inputs start past the brain's ~{READ_WINDOW_LINES}-line "
            f"Read window: {late}. These gates REJECT every BUY when unanswered, so the "
            f"brain would be blocked on inputs it cannot see — the 2026-07-19 bug. "
            f"Cap or reorder the block above them; do NOT raise READ_WINDOW_LINES, "
            f"which is a property of the Read tool, not a knob."
        )


def test_the_two_process_gates_are_readable(packs):
    """factor_map and portfolio_competition specifically — these were at ~50,300."""
    for label, pack in packs:
        starts = key_start_lines(pack)
        for gate in ("factor_map", "portfolio_competition"):
            if gate in pack:
                assert starts[gate] <= READ_WINDOW_LINES, (
                    f"[{label}] {gate} at line {starts[gate]} — it drives a BLOCKING "
                    f"process gate and must be readable in a default Read.")


def test_learning_pack_is_readable(packs):
    """All six learning loops write into reasoning_process; it sat at line 52,928."""
    for label, pack in packs:
        starts = key_start_lines(pack)
        assert starts["reasoning_process"] <= READ_WINDOW_LINES, (
            f"[{label}] reasoning_process at line {starts['reasoning_process']} — "
            f"every learning loop feeds it, and past the window they are all dead text.")
        lp = pack["reasoning_process"]["learning_pack"]
        assert isinstance(lp, dict) and lp, "learning_pack must survive slimming"


def test_decision_critical_keys_precede_research(packs):
    """Ordering is the mechanism; assert it directly, not just its consequence."""
    for label, pack in packs:
        starts = key_start_lines(pack)
        present = [k for k in DECISION_CRITICAL if k in pack]
        last_critical = max(starts[k] for k in present)
        for research in ("deep_fundamentals", "sec_filings", "filing_texts",
                         "universe_scan", "insider_activity"):
            if research in pack:
                assert starts[research] > last_critical, (
                    f"[{label}] {research} (line {starts[research]}) is emitted before "
                    f"the end of the decision-critical set (line {last_critical}) — "
                    f"research bulk is crowding the window it must sit behind.")


# ---------------------------------------------------------------------------
# 2. THE PACK CANNOT SILENTLY REGROW.
# ---------------------------------------------------------------------------

def test_pack_stays_under_the_line_ceiling(packs):
    for label, pack in packs:
        n = pack_line_count(pack)
        assert n <= MAX_PACK_LINES, (
            f"[{label}] slim pack is {n} lines, over the {MAX_PACK_LINES} ceiling. "
            f"A new block was probably wired in without a cap. Find it in "
            f"_pack_budget and cap it; raise the ceiling only with a measurement.")


def test_budget_block_reports_its_own_true_offsets(packs):
    """_pack_budget is part of the pack it measures, so it must be self-consistent.

    A budget that misreports its own offsets is the same silent-omission failure
    wearing a measurement costume — it would tell the brain a block was readable
    when it was not.
    """
    for label, pack in packs:
        budget = pack["_pack_budget"]
        starts = key_start_lines(pack)
        assert budget["total_lines"] == pack_line_count(pack), (
            f"[{label}] _pack_budget.total_lines disagrees with the serialized pack")
        for entry in budget["keys_beyond_read_window"]:
            key, _, line = entry.rpartition("@")
            assert starts[key] == int(line), (
                f"[{label}] _pack_budget claims {key}@{line}, actual {starts[key]}")
            assert int(line) > READ_WINDOW_LINES


# ---------------------------------------------------------------------------
# 3. NO SILENT OMISSIONS — every cap discloses itself.
# ---------------------------------------------------------------------------

def test_every_cap_is_disclosed(packs):
    for label, pack in packs:
        trimmed = pack["_pack_budget"]["trimmed"]
        assert trimmed, f"[{label}] caps ran but nothing was disclosed in _pack_budget"
        assert all(isinstance(t, str) and t for t in trimmed)


def test_dropped_blocks_leave_a_marker_in_place():
    """A trimmed block must say so where the data used to be, not only centrally."""
    pack = slim_context_for_brain(_synthetic_bundle())
    df = pack["deep_fundamentals"]
    sample = df[next(iter(df))]
    # The series survive (see test_deep_fundamentals_keeps_every_series); it is
    # the older PERIODS that go, and that removal must be stamped in place.
    assert "quarterly_facts" in sample
    assert "_trimmed" in sample, (
        "dropping periods without an in-place marker is a silent omission")
    assert "quarterly_facts" in json.dumps(sample["_trimmed"]), (
        "the marker must name what was dropped")

    of = pack["ownership_flow"]["by_ticker"]
    assert "_trimmed" in of[next(iter(of))]


def test_truncated_prose_is_marked_inside_the_text():
    """A cut excerpt must not be able to read as a complete disclosure."""
    pack = slim_context_for_brain(_synthetic_bundle())
    blk = pack["filing_texts"][next(iter(pack["filing_texts"]))]["mdna"]
    assert blk["excerpt_truncated_for_pack"] is True
    assert "TRUNCATED" in blk["excerpt"], (
        "truncation must be visible in the excerpt itself — metadata alone lets a "
        "half-sentence read as the end of the filing")
    assert blk["excerpt_full_chars"] == 9000


# ---------------------------------------------------------------------------
# 4. THE CAPS PRESERVE WHAT DOWNSTREAM CODE ACTUALLY READS.
# ---------------------------------------------------------------------------

def test_market_cap_survives_the_deep_fundamentals_cap():
    """orchestrator.py reads deep_fundamentals[T].quality_ratios.ratios.market_cap_usd
    to enforce the $1B floor. Capping this block must not disarm that check."""
    pack = slim_context_for_brain(_synthetic_bundle())
    sample = pack["deep_fundamentals"]["TK00"]
    assert sample["quality_ratios"]["ratios"]["market_cap_usd"]["value"] == 5e9


def test_deep_fundamentals_keeps_every_series():
    """REGRESSION: a first pass deleted quarterly_facts outright, reading
    "TRUST THE RATIOS" as licence to drop the inputs. But deferred_revenue and
    remaining_performance_obligation are NOT in quality_ratios — they live here,
    and financial_checklists tells the brain to walk RPO on every SaaS name.
    Cap the HISTORY, never the series."""
    bundle = _synthetic_bundle()
    bundle["deep_fundamentals"]["TK00"]["quarterly_facts"] = {
        "deferred_revenue": [{"v": i} for i in range(8)],
        "remaining_performance_obligation": [{"v": i} for i in range(8)],
        "share_buybacks": [{"v": i} for i in range(8)],
    }
    qf = slim_context_for_brain(bundle)["deep_fundamentals"]["TK00"]["quarterly_facts"]
    for series in ("deferred_revenue", "remaining_performance_obligation",
                   "share_buybacks"):
        assert series in qf, f"{series} was dropped — the brain is told to read it"
        assert len(qf[series]) >= 1


def test_sec_filings_keeps_enough_periods_for_yoy():
    """A year-over-year read needs the current quarter AND the year-ago quarter."""
    bundle = _synthetic_bundle()
    bundle["sec_filings"]["TK00"]["quarterly_fundamentals"] = {
        "revenue": [{"val": i} for i in range(12)]}
    rows = slim_context_for_brain(bundle)["sec_filings"]["TK00"][
        "quarterly_fundamentals"]["revenue"]
    assert len(rows) >= 5, (
        f"only {len(rows)} periods kept — YoY is uncomputable from this block")


def test_fundamentals_freshness_never_drops_a_stale_name():
    """The cap may only ever remove PROVABLY FRESH rows.

    stale_tickers drives the validator's fundamentals_stale:<TICKER> rejection,
    and CLAUDE.md calls freshness a HARD RULE. A cap that ate a stale row would
    let the brain cite last quarter's numbers as current.
    """
    bundle = _synthetic_bundle()
    bundle["fundamentals_freshness"] = {
        "by_ticker": {
            "FRESH": {"current_through": "2026-03-31", "stale": False},
            "STALE": {"current_through": "2025-12-31", "stale": True},
            "LISTED": {"current_through": "2025-12-31", "stale": False},
            "UNKNOWN": {"current_through": "2026-03-31"},          # no stale flag
            "WARNED": {"stale": False, "stale_fundamentals_warning": "carried fwd"},
        },
        "stale_tickers": ["LISTED"],
    }
    kept = slim_context_for_brain(bundle)["fundamentals_freshness"]["by_ticker"]
    assert "STALE" in kept, "explicitly stale row was dropped"
    assert "LISTED" in kept, "row named in stale_tickers was dropped"
    assert "UNKNOWN" in kept, "unknown staleness must fail safe and be kept"
    assert "WARNED" in kept, "row carrying a stale-fundamentals warning was dropped"
    assert "FRESH" not in kept, "provably fresh row should have been trimmed"


def test_sec_filings_keeps_freshness_fields():
    pack = slim_context_for_brain(_synthetic_bundle())
    sample = pack["sec_filings"]["TK00"]
    assert sample["fundamentals_current_through"] == "2026-03-31"
    assert sample["status"] == "ok"
    assert len(sample["recent_filings"]) <= 8


def test_guidance_prose_gets_a_larger_budget():
    """CLAUDE.md tells the brain to QUOTE the exact guidance sentence, so an
    excerpt carrying guidance language must not be cut as hard as boilerplate."""
    pack = slim_context_for_brain(_synthetic_bundle())
    ft = pack["filing_texts"]["TK00"]
    assert len(ft["earnings_release"]["excerpt"]) > len(ft["mdna"]["excerpt"])


def test_watchlist_feedback_is_where_claude_md_says_it_is():
    """CLAUDE.md: "read reasoning_process.watchlist_feedback". It previously only
    existed one level deeper, at reasoning_process.learning_pack.watchlist_feedback,
    so the brain following its own instructions found nothing."""
    pack = slim_context_for_brain(_synthetic_bundle())
    rp = pack["reasoning_process"]
    assert "watchlist_feedback" in rp
    assert "watchlist_feedback" not in rp["learning_pack"], (
        "duplicated between parent and learning_pack — two copies can drift")


def test_slim_does_not_mutate_the_full_bundle():
    """The orchestrator keeps the full context in memory and hands it to the
    validator AFTER writing the slim pack. A cap that mutated shared nested
    state would silently strip the validator's own inputs."""
    bundle = _synthetic_bundle()
    before = json.dumps(bundle, sort_keys=True, default=str)
    slim_context_for_brain(bundle)
    assert json.dumps(bundle, sort_keys=True, default=str) == before, (
        "slim_context_for_brain mutated the full bundle in place")
