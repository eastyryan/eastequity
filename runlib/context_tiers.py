"""Tiered context packs — full archive vs slim brain-facing bundle.

Full context is written to state/ for audit and cloud act-on.
The brain receives a SLIM pack so CLAUDE non-negotiables are not drowned.

Tiers
-----
always   — portfolio, regime, limits, digest, data quality, process checklist
focus    — deep research only for focus tickers (already true of deep_* maps)
learning — top-N compact learning signals (not full history dumps)
full     — everything (archive only)

Public API:
  slim_context_for_brain(full) -> dict
  learning_pack(full, limits) -> dict
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


# Keys always kept at top level for the brain
ALWAYS_KEYS = (
    "run_date", "as_of_et", "trading_mode", "run_depth", "run_depth_note",
    "allows_new_buys", "hard_limits", "digest", "macro_regime", "portfolio",
    "position_histories", "position_stop_cushion", "stop_engineering",
    "watchlist_trigger_alerts", "tape_focus_promotions", "market_events",
    "market_news", "market_radar", "data_quality", "stale_data_notice",
    "benchmark_close",
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


def slim_context_for_brain(full: dict, *, learning_n: int = 5) -> dict:
    """Build brain-facing context from full gather bundle."""
    if not isinstance(full, dict):
        return {}
    slim: dict[str, Any] = {
        "_context_tier": "brain_slim_v1",
        "_tier_note": (
            "This is the SLIM brain pack. Full archive is at full_context_path if set. "
            "Learning signals are in reasoning_process.learning_pack (top-N). "
            "Deep maps are limited to focus/digest tickers."
        ),
    }
    for k in ALWAYS_KEYS:
        if k in full:
            slim[k] = full[k]

    # Focus research (trim large maps)
    for k in FOCUS_KEYS:
        if k not in full:
            continue
        v = full[k]
        if k == "universe_scan":
            slim[k] = _trim_universe_scan(v)
        elif k in ("sec_filings", "deep_fundamentals", "filing_texts", "news_and_catalysts",
                   "insider_activity", "options_signals", "partnerships"):
            # These are often {ticker: ...} or nested tickers
            if isinstance(v, dict) and any(re_looks_like_ticker(x) for x in list(v)[:5]):
                slim[k] = _trim_map_to_digest(full, v)
            else:
                slim[k] = v
        else:
            slim[k] = v

    # Reasoning process: replace bulk with learning pack + essentials
    rp = full.get("reasoning_process") if isinstance(full.get("reasoning_process"), dict) else {}
    slim_rp = {
        "run_depth": rp.get("run_depth"),
        "process_checklist": rp.get("process_checklist"),
        "watchlist_status_required": rp.get("watchlist_status_required"),
        "full_run_no_trade_rule": rp.get("full_run_no_trade_rule"),
        "theme_exposure": rp.get("theme_exposure"),
        "theme_concentration_cap_pct": rp.get("theme_concentration_cap_pct"),
        "price_freshness": rp.get("price_freshness"),
        "demand_driver_map_note": rp.get("demand_driver_map_note"),
        "learning_pack": compact_learning_pack(full, n=learning_n),
    }
    # Keep demand_driver_map_focus inside learning_pack already
    slim["reasoning_process"] = slim_rp

    # Track record: keep performance + last 10 closed only
    tr = full.get("track_record")
    if isinstance(tr, dict):
        slim["track_record"] = {
            "note": tr.get("note"),
            "closed_trades": _top(tr.get("closed_trades") or [], 10),
            "performance": tr.get("performance"),
            "breakdowns": tr.get("breakdowns"),
            "calibration": tr.get("calibration"),
        }

    if full.get("full_context_path"):
        slim["full_context_path"] = full["full_context_path"]

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
