"""Tiered context packs — full archive vs slim brain-facing bundle.

Full context is written to state/ for audit and cloud act-on.
The brain receives a SLIM pack so CLAUDE non-negotiables are not drowned.

THE INCIDENT THIS FILE EXISTS TO PREVENT
----------------------------------------
Measured 2026-07-19: the slim pack was 53,623 lines. The brain reads it with the
Read tool, which returns ~2,000 lines. Every BLOCKING input sat past that window:

    hard_limits            line     10   read
    digest                 line    170   read
    stack_cards            line 45,296   NEVER READ
    factor_map             line 50,322   NEVER READ  <- requires_factor_response
    portfolio_competition  line 50,347   NEVER READ  <- drives seat_reviews
    reasoning_process      line 52,928   NEVER READ  <- the entire learning_pack

Both gates reject every BUY in a run when unanswered. The brain was being blocked
for failing to answer questions it could not see, and all six learning loops wrote
to an address it never read. The old tiering saved 1.25% (1,904,722 vs 1,928,796
bytes) because _trim_map_to_digest is a no-op on maps context_gather already scoped
to focus tickers — it was measuring the wrong thing (bytes) against the wrong budget.

THE BUDGET IS LINES, NOT BYTES. Bytes were never what truncated the brain.

Two invariants, both pinned by tests/test_context_budget.py:
  1. Every key in DECISION_CRITICAL starts before READ_WINDOW_LINES.
  2. The whole pack stays under MAX_PACK_LINES so it cannot silently regrow.

TRIMMING MUST NEVER BE SILENT. Every cap below records what it dropped and where
the full copy lives, in-place under a `_trimmed` key and aggregated into
`_pack_budget`. A silent omission is the exact failure class this system spent
three days eliminating; a pack that quietly loses a block is worse than a big one.

Tiers
-----
always   — portfolio, regime, limits, digest, data quality, process checklist
focus    — deep research only for focus tickers (already true of deep_* maps)
learning — top-N compact learning signals (not full history dumps)
full     — everything (archive only)

Public API:
  slim_context_for_brain(full) -> dict
  compact_learning_pack(full, n) -> dict
  pack_line_count(pack) -> int
  key_start_lines(pack) -> dict[str, int]
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


# The Read tool returns this many lines by default. Everything the brain is
# BLOCKED on must start before it, or the gate is unanswerable by construction.
READ_WINDOW_LINES = 2000

# Ceiling on the whole pack. NOT a truncation point and NOT an aspiration — a
# regression alarm sized above the largest observed focus set, so a new block
# wired in without a cap trips the test instead of silently landing in the pack.
#
# READ THIS BEFORE LOWERING IT. The bug this module fixes was ORDER, not size.
# Blocks past the read window cost disk and paging, not tokens — the brain only
# pays for what it actually reads, and it reads the first ~2,000 lines. A first
# pass at these caps chased the byte count down to 31,737 lines and in doing so
# deleted quarterly_facts entirely, which removed deferred_revenue and
# remaining_performance_obligation — lines financial_checklists explicitly tells
# the brain to walk on every SaaS name. That traded real signal for a saving the
# system does not collect. Inside the window every line competes; outside it,
# only genuine redundancy should go.
#
# Measured 2026-07-19: 53,623 lines before, ~41,600 after on a 28-name focus set
# (~26,200 on a 16-name set — the pack scales with focus-set size, which is why
# this ceiling is set against the larger one). The win to look at is not this
# number; it is that the last BLOCKING key moved from line 52,928 to line 1,673.
MAX_PACK_LINES = 48000

# Emitted in insertion order, before any research map. dict preserves order and
# the pack is serialized with json.dumps(..., indent=2), so insertion order IS
# line order.
#
# TIER 1 — BLOCKING. Deterministic code REJECTS a proposal when one of these is
# unanswered or breached. If any of these falls outside the read window the gate
# becomes unanswerable by construction, which is precisely the bug measured on
# 2026-07-19. tests/test_context_budget.py pins every one of them under
# READ_WINDOW_LINES; that assertion is the whole point of this module.
BLOCKING_KEYS = (
    "_context_tier", "_tier_note", "_pack_budget",
    "run_date", "as_of_et", "trading_mode", "run_depth", "run_depth_note",
    "allows_new_buys", "full_context_path",
    "hard_limits",              # live validator floors
    "digest",                   # "READ FIRST" per its own note
    "factor_map",               # BLOCKING: requires_factor_response
    "portfolio_competition",    # BLOCKING: drives seat_reviews
    "reasoning_process",        # process_checklist + the whole learning_pack
    "portfolio",
    "stop_engineering",         # min stop distance — rejects stop_inside_noise_band
    "position_stop_cushion",
    "risk_halts", "forced_exits", "corporate_actions",
    "data_quality", "stale_data_notice", "price_freshness_live",
    "fundamentals_freshness",   # HARD RULE; drives fundamentals_stale:<TICKER>
)

# TIER 2 — the regime read and book context. Step 1 of the Required Process
# ("Regime") lives here, so it is emitted immediately after the blocking set and
# normally lands inside the window too. It is NOT pinned by the hard assertion:
# these do not gate a proposal, and pinning them would force the digest to be
# gutted to make room on a large focus set. Whatever falls past the window is
# named with its exact offset in _pack_budget.keys_beyond_read_window.
# Ordered by value-per-line, not by importance in the abstract. The regime read
# is step 1 of the Required Process and costs ~150 lines; track_record is 187
# lines of history that is 2 trades deep today and whose actionable summary
# (phase, inflated-bucket flag, losing sectors) is already inside
# reasoning_process.learning_pack.calibration_status. So regime, live trigger
# alerts and book risk get the remaining window space, and the history follows.
DECISION_CONTEXT = (
    "macro_regime", "benchmark_close", "market_events",
    "watchlist_trigger_alerts", "tape_focus_promotions",
    "portfolio_risk",
    "track_record",
    "ownership_flow",
    "earnings_deep_dive", "trigger_run_note", "operator_note",
    "lessons_learned",
)

DECISION_CRITICAL = BLOCKING_KEYS + DECISION_CONTEXT


# Keys always kept at top level for the brain
ALWAYS_KEYS = (
    "run_date", "as_of_et", "trading_mode", "run_depth", "run_depth_note",
    "allows_new_buys", "hard_limits", "digest", "macro_regime", "portfolio",
    "position_histories", "position_stop_cushion", "stop_engineering",
    "watchlist_trigger_alerts", "tape_focus_promotions", "market_events",
    "market_news", "market_radar", "data_quality", "stale_data_notice",
    "benchmark_close",
    # FOUND BY THE BRAIN ITSELF, 2026-07-19 weekly run. momentum_health was in
    # neither ALWAYS_KEYS nor FOCUS_KEYS, so it was gathered, written to the full
    # archive, READ BY THE VALIDATOR (which halves new-BUY size on an unwind) and
    # stripped from the slim pack. CLAUDE.md names it a required input to the
    # regime step on every run. That run's archive carried
    # {"status": "unwind", "momentum_unwind": true} while the brain, unable to see
    # the block, reconstructed the unwind by hand from sector_relative_strength
    # and filed it as its improvement note.
    #
    # The silent-omission shape again: fail-open treats a missing block as
    # "unknown, no adjustment", which is indistinguishable in the pack from a
    # genuine `unknown` verdict on live data. Absent and inconclusive must not
    # look the same.
    "momentum_health",
    "risk_halts", "forced_exits", "corporate_actions", "lessons_learned",
    "track_record",  # compact closed trades already truncated in gather
    "earnings_deep_dive",     # why a full run was forced (earnings reporter)
    "price_freshness_live",   # holdings/watchlist live-price staleness guard
    "trigger_run_note",       # why an event-driven run was spawned
    "operator_note",          # ad-hoc note passed in via --note
)

# Focus research maps (trimmed to focus keys already)
FOCUS_KEYS = (
    "universe_scan", "sec_filings", "deep_fundamentals", "filing_texts",
    "filing_texts_note", "fundamentals_freshness", "news_and_catalysts",
    "insider_activity", "smart_money_13f", "options_signals", "partnerships",
    "guidance_ledger", "fundamental_screen", "todays_8ks", "portfolio_risk",
    "stack_cards", "financial_checklists", "concept_memory", "discovery_screen",
    "market_checkin",
    # Wired into the bundle 2026-07-19. Omitting them here would have been the
    # second half of the same bug: the blocks would exist in the full archive and
    # be stripped from the slim pack the brain actually reads, so factor_map's
    # requires_factor_response would still never reach a decision.
    "factor_map", "portfolio_competition", "ownership_flow",
)

# Learning-related keys that get compacted into reasoning_process.learning_pack
LEARNING_SOURCE_KEYS = (
    "reasoning_process",
)


def _top(lst, n: int) -> list:
    if not isinstance(lst, list):
        return []
    return lst[:n]


def compact_learning_pack(full: dict, *, n: int = 5) -> dict:
    """Top-N learning signals for the brain (not full shadow/exit history)."""
    rp = full.get("reasoning_process") if isinstance(full.get("reasoning_process"), dict) else {}
    exits = {}
    try:
        # May live under reasoning_process after gather, or we rebuild from tools
        exits = rp  # shadow/adopted live here
    except Exception:
        exits = {}

    shadow = rp.get("shadow_learning") if isinstance(rp.get("shadow_learning"), dict) else {}
    adopted = rp.get("adopted_lessons") if isinstance(rp.get("adopted_lessons"), dict) else {}
    cal = rp.get("calibration_status") if isinstance(rp.get("calibration_status"), dict) else {}

    # Exit lessons + runners may be nested in exit_lessons from brain_facing
    # gather puts exit lessons inside reasoning_process via brain_reasoning_bundle
    # which only has exit_lessons from tools - check structure
    exit_block = rp.get("exit_lessons") if isinstance(rp.get("exit_lessons"), dict) else {}
    runner = exit_block.get("runner_learning") if isinstance(exit_block.get("runner_learning"), dict) else {}
    if not runner and isinstance(full.get("exit_lessons"), dict):
        exit_block = full["exit_lessons"]
        runner = exit_block.get("runner_learning") or {}

    # Prefer pulling live if missing
    if not shadow or shadow.get("status") == "error":
        try:
            from tools.shadow_portfolio import brain_facing_shadow_learning
            shadow = brain_facing_shadow_learning(n * 2)
        except Exception:
            pass
    if not runner:
        try:
            from tools.post_exit_runners import brain_facing_runner_learning
            runner = brain_facing_runner_learning(n * 2)
        except Exception:
            pass
    if not exit_block or exit_block.get("status") == "error":
        try:
            from tools.exit_autopsy import brain_facing_exit_lessons
            exit_block = brain_facing_exit_lessons(n * 2)
            runner = exit_block.get("runner_learning") or runner
        except Exception:
            pass
    if not adopted:
        try:
            from tools.learning_adopt import brain_facing_adopted_lessons
            adopted = brain_facing_adopted_lessons(n * 2)
        except Exception:
            pass
    if not cal:
        try:
            from tools.calibration_gate import brain_facing_calibration_status
            cal = brain_facing_calibration_status()
        except Exception:
            pass
    knowledge = rp.get("knowledge_base") if isinstance(rp.get("knowledge_base"), dict) else {}
    if not knowledge or knowledge.get("status") == "error":
        try:
            from tools.knowledge_base import brain_facing_knowledge_base
            knowledge = brain_facing_knowledge_base(n)
        except Exception:
            pass

    left = _top(runner.get("left_on_table") or [], n)
    # Slim attribution on left_on_table
    left_slim = []
    for row in left:
        if not isinstance(row, dict):
            continue
        left_slim.append({
            "ticker": row.get("ticker"),
            "exit_date": row.get("exit_date"),
            "gain_at_exit_pct": row.get("gain_at_exit_pct"),
            "max_extension_pct": row.get("max_extension_pct"),
            "why_left_on_table": row.get("why_left_on_table"),
            "hold_lesson": (row.get("hold_lesson") or "")[:280],
            "attribution_primary": (row.get("attribution") or {}).get("primary_driver"),
            "attribution_evidence": _top(
                (row.get("attribution") or {}).get("evidence") or [], 2),
        })

    return {
        "note": (
            f"LEARNING PACK (top {n} each). Full histories live in state/ archive "
            "and data/*.json — do not need them unless drilling. Use this pack for "
            "decisions: regrets, good skips, binding exits, left-on-table with WHY, "
            "adopted lessons, calibration phase."
        ),
        "calibration_status": {
            "phase": cal.get("phase"),
            "total_closed_trades": cal.get("total_closed_trades"),
            "high_conf_inflated": cal.get("high_conf_inflated"),
            "losing_sectors": _top(cal.get("losing_sectors") or [], n),
            "confidence_cap_when_inflated": cal.get("confidence_cap_when_inflated"),
        },
        "shadow": {
            "binding": shadow.get("binding"),
            # These four are DISCLOSURES, and this block is a cherry-picking
            # allowlist: anything not named here is dropped before the brain ever
            # sees it. Adding a caveat to shadow_portfolio.py and forgetting this
            # list is the same bug as factor_map being present in the archive and
            # stripped from the slim pack — the warning exists and never arrives.
            "enforcement": shadow.get("enforcement"),
            "enforcement_note": shadow.get("enforcement_note"),
            "verdict_distribution": shadow.get("verdict_distribution"),
            "verdict_skew_warning": shadow.get("verdict_skew_warning"),
            "measurement_quality": shadow.get("measurement_quality"),
            "stats": shadow.get("stats"),
            "regret_misses": _top(shadow.get("regret_misses") or [], n),
            "good_skips": _top(shadow.get("good_skips") or [], n),
            "open_running_best": _top(shadow.get("open_running_best") or [], n),
        },
        "exits": {
            "binding_lessons": _top(exit_block.get("binding_lessons") or [], n),
            "recent": _top(exit_block.get("recent") or [], n),
            "ungraded_count": exit_block.get("ungraded_count"),
        },
        "runners": {
            "binding": runner.get("binding"),
            "stats": runner.get("stats"),
            "left_on_table": left_slim,
            "good_lock_ins": _top(runner.get("good_lock_ins") or [], n),
            "open_winners_still_running": _top(
                runner.get("open_winners_still_running") or [], n),
            "hold_winners_playbook": runner.get("hold_winners_playbook"),
        },
        "adopted_lessons": {
            "lessons": _top(adopted.get("lessons") or [], n),
            "hard_pending": _top(adopted.get("hard_pending") or [], min(n, 3)),
            "n_adopted": adopted.get("n_adopted"),
        },
        "knowledge_base": {
            "note": knowledge.get("note"),
            "recent": _top(knowledge.get("recent") or [], n),
            "n_active": knowledge.get("n_active"),
            "discipline_counts": knowledge.get("discipline_counts"),
        },
        "process_checklist": rp.get("process_checklist"),
        "watchlist_feedback": _compact_watchlist_feedback(
            rp.get("watchlist_feedback") or {}, n),
        "theme_exposure": rp.get("theme_exposure"),
        "theme_concentration_cap_pct": rp.get("theme_concentration_cap_pct"),
        "price_freshness": rp.get("price_freshness"),
        "demand_driver_map_note": rp.get("demand_driver_map_note"),
        # Slim map: only focus + held tickers if present
        "demand_driver_map_focus": _focus_driver_map(full, rp.get("demand_driver_map") or {}),
    }


def _compact_watchlist_feedback(wf: dict, n: int) -> dict:
    if not isinstance(wf, dict):
        return {}
    return {
        "note": wf.get("note"),
        "stats": wf.get("stats"),
        "hits_not_bought": _top(wf.get("hits_not_bought") or [], n),
        "opportunity_cost": _top(wf.get("opportunity_cost") or [], n),
    }


def _focus_driver_map(full: dict, dmap: dict) -> dict:
    if not isinstance(dmap, dict):
        return {}
    focus = set()
    for pos in (full.get("portfolio") or {}).get("positions") or []:
        if isinstance(pos, dict) and pos.get("ticker"):
            focus.add(str(pos["ticker"]).upper())
    for t in (full.get("concept_memory") or {}).get("by_ticker") or {}:
        focus.add(str(t).upper())
    for t in (full.get("stack_cards") or {}).get("by_ticker") or {}:
        focus.add(str(t).upper())
    # digest keys
    for t in (full.get("digest") or {}).get("by_ticker") or {}:
        focus.add(str(t).upper())
    if not focus:
        # fallback: first 30 of map is still too big — return empty note
        return {"_note": "no focus tickers resolved; use stack_cards.demand_driver"}
    return {t: dmap[t] for t in focus if t in dmap}


def _trim_universe_scan(scan: dict) -> dict:
    """Keep scan useful but drop bulk atr/prices for non-surfaced if huge."""
    if not isinstance(scan, dict):
        return scan
    out = dict(scan)
    # Keep prices + meta (needed for triggers) but cap top lists
    for key in ("top_setups", "contrarian_setups", "deep_value_200w", "supplier_pullbacks"):
        if isinstance(out.get(key), list) and len(out[key]) > 15:
            out[key] = out[key][:15]
    # atr_by_ticker can be 180 names — keep but OK; or slim to focus
    return out


def _trim_map_to_digest(full: dict, m: dict, extra: int = 0) -> dict:
    """Keep map entries for digest/focus tickers only."""
    if not isinstance(m, dict):
        return m
    keep = set((full.get("digest") or {}).get("by_ticker") or {})
    keep |= set((full.get("stack_cards") or {}).get("by_ticker") or {})
    for pos in (full.get("portfolio") or {}).get("positions") or []:
        if isinstance(pos, dict) and pos.get("ticker"):
            keep.add(str(pos["ticker"]).upper())
    if not keep:
        return m
    # Preserve non-ticker meta keys
    out = {}
    for k, v in m.items():
        ku = str(k).upper()
        if ku in keep or k in ("status", "note", "as_of", "window_days"):
            out[k] = v
        elif not re_looks_like_ticker(k):
            out[k] = v
    return out


def re_looks_like_ticker(k: str) -> bool:
    k = str(k)
    return k.isalpha() and 1 <= len(k) <= 5 and k.upper() == k


# ---------------------------------------------------------------------------
# Per-block caps.
#
# Every one of these is STRUCTURAL — it keeps named fields and drops named
# fields. None of them truncates by length or by line count, because a blind
# truncator cannot know whether the bytes it dropped were load-bearing.
# Each records what it removed under `_trimmed` so the omission is visible to
# the brain, and each names the archive path where the full copy lives.
# ---------------------------------------------------------------------------

def _mark(block: dict, dropped: str, where: str = "full_context_path") -> dict:
    """Stamp a visible record of what this cap removed."""
    t = block.setdefault("_trimmed", {})
    t[dropped] = f"omitted from the slim pack — full copy in {where}"
    return block


def _cap_deep_fundamentals(m: dict, log: list, *, keep_periods: int = 2) -> dict:
    """Keep quality_ratios whole; cap quarterly_facts HISTORY, never its series.

    An earlier version of this cap deleted quarterly_facts outright, reading
    CLAUDE.md's "TRUST THE RATIOS - do not recompute them" as licence to drop the
    inputs. That was wrong, and the same paragraph says why: deep_fundamentals is
    "opex, balance sheet, DEFERRED REVENUE/RPO, buybacks, PLUS pre-computed
    quality_ratios". deferred_revenue and remaining_performance_obligation are NOT
    in quality_ratios — they live here, and RPO is the forward-demand line
    financial_checklists tells the brain to walk on every SaaS name. Dropping the
    series removed data the prompt instructs the brain to read.

    So: every series survives, only older periods go. Note this block sits BEYOND
    the read window regardless, so trimming it buys disk, not tokens — which is
    why the cap is now deliberately light. quality_ratios.ratios.market_cap_usd is
    load-bearing (orchestrator.py:777, the $1B floor) and is never touched.
    """
    if not isinstance(m, dict):
        return m
    out, n, dropped_total = {}, 0, 0
    for t, e in m.items():
        if not isinstance(e, dict):
            out[t] = e
            continue
        e = deepcopy(e)
        qf = e.get("quarterly_facts")
        if isinstance(qf, dict):
            capped, dropped = {}, 0
            for series, rows in qf.items():
                if isinstance(rows, list) and len(rows) > keep_periods:
                    dropped += len(rows) - keep_periods
                    capped[series] = rows[:keep_periods]
                else:
                    capped[series] = rows
            e["quarterly_facts"] = capped
            if dropped:
                dropped_total += dropped
                _mark(e, f"quarterly_facts history ({dropped} older periods; every "
                         f"series kept, newest {keep_periods} periods each)")
                n += 1
        out[t] = e
    if n:
        log.append(f"deep_fundamentals: capped quarterly_facts to the newest "
                   f"{keep_periods} periods for {n} tickers ({dropped_total} rows); "
                   f"all series incl. deferred_revenue/RPO retained")
    return out


def _cap_sec_filings(m: dict, log: list, *, keep_periods: int = 5,
                     keep_filings: int = 8) -> dict:
    """Keep filing identity + freshness; cap the statement history and filing list.

    keep_periods is 5, not 2, on purpose: a year-over-year read needs the current
    quarter AND the year-ago quarter, so anything under 5 makes YoY uncomputable
    from this block. CLAUDE.md has the brain checking period_days and reasoning
    about YTD flow rows, which needs the sequence, not a snapshot.

    fundamentals_current_through and stale_fundamentals_warning are HARD RULE
    inputs (validator rejects `fundamentals_stale:<TICKER>`) and are never touched.
    """
    if not isinstance(m, dict):
        return m
    out, n = {}, 0
    for t, e in m.items():
        if not isinstance(e, dict):
            out[t] = e
            continue
        e = deepcopy(e)
        qf = e.get("quarterly_fundamentals")
        if isinstance(qf, dict):
            capped, dropped = {}, 0
            for series, rows in qf.items():
                if isinstance(rows, list) and len(rows) > keep_periods:
                    dropped += len(rows) - keep_periods
                    capped[series] = rows[:keep_periods]
                else:
                    capped[series] = rows
            e["quarterly_fundamentals"] = capped
            if dropped:
                _mark(e, f"quarterly_fundamentals history ({dropped} older periods; "
                         f"newest {keep_periods} kept per series)")
                n += 1
        rf = e.get("recent_filings")
        if isinstance(rf, list) and len(rf) > keep_filings:
            _mark(e, f"recent_filings ({len(rf) - keep_filings} older filings; "
                     f"newest {keep_filings} kept)")
            e["recent_filings"] = rf[:keep_filings]
        out[t] = e
    if n:
        log.append(f"sec_filings: capped statement history to the newest "
                   f"{keep_periods} periods for {n} tickers")
    return out


def _cap_filing_texts(m: dict, log: list, *, plain: int = 3000,
                      guidance: int = 6000) -> dict:
    """Cap MD&A / earnings-release prose, keeping more when guidance is present.

    CLAUDE.md tells the brain to QUOTE the exact guidance sentence when
    contains_guidance_language is true, so those excerpts get a much larger
    budget. Truncation is stamped inline in the excerpt itself, not only in
    metadata, so a half-sentence can never read as the end of the disclosure.

    The budgets are generous because the MD&A excerpt is already targeted at the
    Results-of-Operations + Liquidity discussion — the section CLAUDE.md says to
    "mine for segment trends and guidance" — and because this block sits past the
    read window, where a shorter excerpt costs real signal and saves no tokens.
    An earlier 1,100-char cap was roughly 180 words, which is not a discussion.
    """
    if not isinstance(m, dict):
        return m
    out, n = {}, 0
    for t, e in m.items():
        if not isinstance(e, dict):
            out[t] = e
            continue
        e = deepcopy(e)
        for sec in ("mdna", "earnings_release"):
            blk = e.get(sec)
            if not isinstance(blk, dict):
                continue
            ex = blk.get("excerpt")
            if not isinstance(ex, str):
                continue
            cap = guidance if blk.get("contains_guidance_language") else plain
            if len(ex) > cap:
                blk["excerpt"] = (
                    ex[:cap]
                    + f"\n\n[TRUNCATED for the brain pack: {len(ex) - cap} more "
                      f"characters of this {sec} section are in full_context_path. "
                      f"Read them before quoting this section as complete.]")
                blk["excerpt_truncated_for_pack"] = True
                blk["excerpt_full_chars"] = len(ex)
                n += 1
        out[t] = e
    if n:
        log.append(f"filing_texts: truncated {n} prose excerpts "
                   f"({plain} chars, {guidance} when guidance language is present)")
    return out


def _cap_ownership_flow(block: dict, log: list) -> dict:
    """Keep the two fields CLAUDE.md says to read first, plus the synthesis.

    CLAUDE.md: "Read by_ticker[T].alignment and read_for_swing first." The
    institutional / retail_proxy / price_action sub-blocks are the evidence those
    two fields were computed from — ~1.7KB per ticker, 2,575 lines in total, which
    made this the single largest block in the decision-critical set.
    """
    if not isinstance(block, dict):
        return block
    bt = block.get("by_ticker")
    if not isinstance(bt, dict):
        return block
    block = deepcopy(block)
    keep = ("ticker", "alignment", "read_for_swing", "synthesis")
    out, n = {}, 0
    for t, e in bt.items():
        if not isinstance(e, dict):
            out[t] = e
            continue
        slim_e = {k: e[k] for k in keep if k in e}
        dropped = [k for k in e if k not in keep and not k.startswith("_")]
        if dropped:
            _mark(slim_e, f"supporting evidence ({', '.join(dropped)})")
            n += 1
        out[t] = slim_e
    block["by_ticker"] = out
    if n:
        log.append(f"ownership_flow: kept alignment + read_for_swing + synthesis for "
                   f"{n} tickers, dropped the supporting evidence blocks "
                   f"(institutional / retail_proxy / price_action detail)")
    return block


def _cap_fundamentals_freshness(block: dict, log: list) -> dict:
    """Keep the STALE names in full; drop the fresh ones' per-ticker rows.

    stale_tickers drives the validator's `fundamentals_stale:<TICKER>` rejection,
    so the actionable subset is kept whole. The fresh names' rows duplicate
    digest.by_ticker[T].latest_quarter_end / .fundamentals_stale, which the brain
    already reads at line ~341 — and CLAUDE.md's rule ("state the quarter-end date
    next to every fundamental figure") is satisfied from the digest.
    """
    if not isinstance(block, dict):
        return block
    bt = block.get("by_ticker")
    if not isinstance(bt, dict):
        return block
    block = deepcopy(block)
    stale = {str(t).upper() for t in (block.get("stale_tickers") or [])}
    kept, dropped = {}, []
    for t, e in bt.items():
        # Drop ONLY on positive confirmation of freshness. A row whose shape we
        # do not recognise, or whose `stale` flag is absent/None, is KEPT — the
        # cost of keeping a fresh row is a few lines; the cost of dropping a
        # stale one is the brain citing last quarter's numbers as current.
        provably_fresh = (
            isinstance(e, dict)
            and e.get("stale") is False
            and not e.get("stale_fundamentals_warning")
            and str(t).upper() not in stale
        )
        if provably_fresh:
            dropped.append(t)
        else:
            kept[t] = e
    if dropped:
        block["by_ticker"] = kept
        block["_trimmed"] = {
            "fresh_ticker_rows": (
                f"{len(dropped)} names with non-stale fundamentals omitted "
                f"({', '.join(sorted(dropped)[:8])}"
                f"{'...' if len(dropped) > 8 else ''}). Their quarter-end is in "
                f"digest.by_ticker[T].latest_quarter_end. Every STALE name is kept "
                f"here in full — this cap can only ever drop fresh names.")
        }
        log.append(f"fundamentals_freshness: dropped {len(dropped)} fresh-ticker rows, "
                   f"kept all {len(kept)} stale/warned names")
    return block


def _cap_price_freshness(block: Any, log: list, *, keep: int = 5) -> Any:
    """Cap prices_meta_sample — it is a SAMPLE, and the real thing is elsewhere.

    The aggregate fields (n_priced, bars_older_than_et_today, scan_fetched_at_utc)
    are the staleness signal and are untouched. The per-ticker sample is a
    illustrative subset of universe_scan.prices_meta, which CLAUDE.md already
    points the brain at for per-name freshness ("use
    universe_scan.prices_meta[T].price_as_of"). 62 lines of illustration inside
    the read window is a poor trade against the real map further down.
    """
    if not isinstance(block, dict):
        return block
    sample = block.get("prices_meta_sample")
    if not isinstance(sample, dict) or len(sample) <= keep:
        return block
    block = deepcopy(block)
    names = list(sample)
    block["prices_meta_sample"] = {t: sample[t] for t in names[:keep]}
    block["_trimmed"] = (
        f"prices_meta_sample cut to {keep} of {len(names)} illustrative rows. "
        f"This is a sample, not the source: per-ticker freshness is in "
        f"universe_scan.prices_meta[TICKER].price_as_of. Aggregates above "
        f"(n_priced, bars_older_than_et_today) cover the whole set.")
    log.append(f"reasoning_process.price_freshness: sample cut to {keep} of {len(names)} rows")
    return block


def pack_line_count(pack: dict) -> int:
    """Lines the brain's Read tool will see. The budget unit for this module."""
    return len(json.dumps(pack, indent=2, default=str).split("\n"))


def key_start_lines(pack: dict) -> dict:
    """1-indexed line where each top-level key starts in the serialized pack.

    This is the measurement the whole module is budgeted against — the brain
    reads lines, so a key's LINE offset is what decides whether it is read.
    """
    lines = json.dumps(pack, indent=2, default=str).split("\n")
    starts: dict[str, int] = {}
    for i, line in enumerate(lines):
        if line.startswith('  "') and line[3:].find('"') > 0:
            k = line[3:3 + line[3:].find('"')]
            starts.setdefault(k, i + 1)
    return starts


def slim_context_for_brain(full: dict, *, learning_n: int = 5) -> dict:
    """Build brain-facing context from full gather bundle.

    Emission order IS read order (dict preserves insertion, json.dumps indents),
    so DECISION_CRITICAL keys are written first and every blocking input lands
    inside the brain's ~2,000-line Read window. Research maps follow, capped
    structurally, each stamping what it dropped.
    """
    if not isinstance(full, dict):
        return {}

    trim_log: list[str] = []

    # Build every value first, then emit in DECISION_CRITICAL order.
    built: dict[str, Any] = {}

    for k in ALWAYS_KEYS:
        if k in full:
            built[k] = full[k]
    # Reasoning process: replace bulk with learning pack + essentials.
    #
    # compact_learning_pack() re-emits several keys the parent already carries
    # (process_checklist, price_freshness, theme_exposure, ...). Two copies cost
    # ~140 lines inside the read window and can drift apart. Keep them at the
    # PARENT level, which is where CLAUDE.md points the brain, and strip the
    # duplicates from the nested copy.
    #
    # watchlist_feedback is hoisted for the same reason and it is a real bug fix:
    # CLAUDE.md says "read reasoning_process.watchlist_feedback (hits_not_bought,
    # large moves not acted)" but it only ever existed at
    # reasoning_process.learning_pack.watchlist_feedback — one level deeper than
    # the documented path. The brain following its own instructions found nothing.
    rp = full.get("reasoning_process") if isinstance(full.get("reasoning_process"), dict) else {}
    lp = compact_learning_pack(full, n=learning_n)
    _PARENT_OWNED = (
        "process_checklist", "theme_exposure", "theme_concentration_cap_pct",
        "price_freshness", "demand_driver_map_note", "watchlist_feedback",
    )
    watchlist_feedback = lp.get("watchlist_feedback")
    lp = {k: v for k, v in lp.items() if k not in _PARENT_OWNED}
    lp["_deduped"] = (
        "process_checklist, theme_exposure, theme_concentration_cap_pct, "
        "price_freshness, demand_driver_map_note and watchlist_feedback live one "
        "level up on reasoning_process — not repeated here.")
    built["reasoning_process"] = {
        "run_depth": rp.get("run_depth"),
        "process_checklist": rp.get("process_checklist"),
        "watchlist_status_required": rp.get("watchlist_status_required"),
        "full_run_no_trade_rule": rp.get("full_run_no_trade_rule"),
        "theme_exposure": rp.get("theme_exposure"),
        "theme_concentration_cap_pct": rp.get("theme_concentration_cap_pct"),
        "price_freshness": _cap_price_freshness(rp.get("price_freshness"), trim_log),
        "demand_driver_map_note": rp.get("demand_driver_map_note"),
        "watchlist_feedback": watchlist_feedback,
        "learning_pack": lp,
    }

    # Track record: keep performance + last 10 closed only. breakdowns is left
    # whole — gather already emits only buckets with trades in them, so there is
    # nothing here to prune that would not be real evidence.
    tr = full.get("track_record")
    if isinstance(tr, dict):
        built["track_record"] = {
            "note": tr.get("note"),
            "closed_trades": _top(tr.get("closed_trades") or [], 10),
            "performance": tr.get("performance"),
            "breakdowns": tr.get("breakdowns"),
            "calibration": tr.get("calibration"),
        }

    # Focus research (trim large maps).
    for k in FOCUS_KEYS:
        if k not in full:
            continue
        v = full[k]
        if k == "universe_scan":
            built[k] = _trim_universe_scan(v)
        elif k == "deep_fundamentals":
            built[k] = _cap_deep_fundamentals(_trim_map_to_digest(full, v), trim_log)
        elif k == "sec_filings":
            built[k] = _cap_sec_filings(_trim_map_to_digest(full, v), trim_log)
        elif k == "filing_texts":
            built[k] = _cap_filing_texts(_trim_map_to_digest(full, v), trim_log)
        elif k == "ownership_flow":
            built[k] = _cap_ownership_flow(v, trim_log)
        elif k == "fundamentals_freshness":
            built[k] = _cap_fundamentals_freshness(v, trim_log)
        elif k in ("news_and_catalysts", "insider_activity", "options_signals",
                   "partnerships"):
            if isinstance(v, dict) and any(re_looks_like_ticker(x) for x in list(v)[:5]):
                built[k] = _trim_map_to_digest(full, v)
            else:
                built[k] = v
        else:
            built[k] = v

    if full.get("full_context_path"):
        built["full_context_path"] = full["full_context_path"]

    # ---- Emit in read order -------------------------------------------------
    slim: dict[str, Any] = {
        "_context_tier": "brain_slim_v2",
        "_tier_note": (
            "SLIM brain pack, ordered so everything BINDING is inside the first "
            f"~{READ_WINDOW_LINES} lines — the default Read window. Keys appear in "
            "decision order: limits, digest, factor_map, portfolio_competition, "
            "reasoning_process (learning_pack), book, then research detail. "
            "Research maps below are structurally capped; every cap stamps a "
            "`_trimmed` note naming what it dropped. Full archive: full_context_path."
        ),
        "_pack_budget": {},  # placeholder, filled once the pack is measurable
    }
    for k in DECISION_CRITICAL:
        if k in built:
            slim[k] = built[k]
    for k, v in built.items():
        if k not in slim:
            slim[k] = v

    # ---- Measure and disclose ----------------------------------------------
    # The budget block is itself part of the pack, so writing it shifts every
    # offset below it. Iterate to a fixed point (converges in 2 passes; the cap
    # is a safety net, not an expected path) so the disclosed line numbers are
    # the ones the brain will actually see — a budget that misreports its own
    # offsets is the same silent-omission failure wearing a measurement costume.
    for _ in range(5):
        starts = key_start_lines(slim)
        beyond = sorted(
            ((v, k) for k, v in starts.items() if v > READ_WINDOW_LINES),
            key=lambda p: p[0])
        budget = {
            "note": (
                "Measured line budget for this pack. Your Read tool returns "
                f"~{READ_WINDOW_LINES} lines by default; anything starting past that "
                "is NOT read unless you page to it with Read(offset=N). Every input "
                "that can BLOCK a proposal is above that line by construction, so a "
                "default Read is sufficient to answer every gate. The entries below "
                "are what you did NOT get — page to them when a name matters."
            ),
            "total_lines": pack_line_count(slim),
            "read_window_lines": READ_WINDOW_LINES,
            # Compact "key@line" strings: an object per key cost ~90 lines of the
            # very window this block exists to protect.
            "keys_beyond_read_window": [f"{k}@{ln}" for ln, k in beyond],
            "trimmed": trim_log,
            "full_archive": full.get("full_context_path"),
        }
        if slim["_pack_budget"] == budget:
            break
        slim["_pack_budget"] = budget
    return slim


def write_tiered_context(full: dict, path_slim, path_full=None, *, learning_n: int = 5) -> dict:
    """Write full + slim JSON; return slim. Paths are Path-like."""
    from runlib.core import json_safe
    import json
    from pathlib import Path
    path_slim = Path(path_slim)
    path_slim.parent.mkdir(parents=True, exist_ok=True)
    if path_full is None:
        path_full = path_slim.with_name(
            path_slim.name.replace("context_", "context_full_"))
        if path_full == path_slim:
            path_full = path_slim.parent / f"full_{path_slim.name}"
    path_full = Path(path_full)
    full = dict(full)
    full["full_context_path"] = str(path_full)
    path_full.write_text(json.dumps(json_safe(full), indent=2, default=str))
    slim = slim_context_for_brain(full, learning_n=learning_n)
    slim["full_context_path"] = str(path_full)
    path_slim.write_text(json.dumps(json_safe(slim), indent=2, default=str))
    return slim
