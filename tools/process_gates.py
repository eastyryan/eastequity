"""Process gates — watchlist status + full-run structured no-trade.

These are learning-phase discipline checks. They do not block the run; they
return machine-readable issues the orchestrator journals and attaches to the
context for the next cycle.

Pure helpers (offline-testable).
"""

from __future__ import annotations

import re
from typing import Any

VALID_WATCH_STATUS = frozenset({"drop", "hold", "buy"})

# Tickers that look like scan ideas mentioned in free-text no_trade_reason.
_TICKER_TOKEN = re.compile(r"\b([A-Z]{1,5})\b")

# Issues that BLOCK new BUYs, as opposed to being journaled and forgotten.
#
# Until 2026-07-19 this module blocked nothing at all — its own note said so, and
# the single behavioural consequence anywhere in the codebase was appending a string
# to no_trade_reason, which only fired when there were already no proposals. A run
# could fail every gate and still execute BUYs.
#
# The split is deliberate and narrow. A gate blocks when it is about the RISK OF THE
# BOOK, not journaling hygiene:
#   factor_response_*  - factor_map raised requires_factor_response because
#                        concentration is high or extreme, and the model owes a
#                        concrete plan before it may add MORE risk.
#   seat_reviews_*     - capital must re-earn its seat. No new risk until the risk
#                        already carried has been re-underwritten this run.
# Watchlist status and rejected_ideas stay advisory: they are learning-discipline
# artifacts, and killing a genuine fat pitch over a missing enum would be a worse
# error than the one being corrected.
BLOCKING_ISSUE_PREFIXES = (
    "factor_response_missing",
    "factor_response_plan_weak",
    "factor_response_actions_empty",
    "seat_reviews_missing",
    "seat_reviews_action_invalid",
    "seat_reviews_reason_weak",
)


def normalize_watchlist(watchlist: list | None) -> tuple[list[dict], list[str]]:
    """Ensure each entry has status in {drop,hold,buy}.

    Returns (normalized_list, issues).
    Missing/invalid status -> default 'hold' + issue string.
    """
    issues: list[str] = []
    out: list[dict] = []
    if not watchlist:
        return [], ["watchlist_empty"]
    for i, raw in enumerate(watchlist):
        if not isinstance(raw, dict):
            issues.append(f"watchlist[{i}]_not_object")
            continue
        w = dict(raw)
        t = str(w.get("ticker") or "").upper()
        if not t:
            issues.append(f"watchlist[{i}]_missing_ticker")
            continue
        w["ticker"] = t
        st = str(w.get("status") or "").strip().lower()
        if st not in VALID_WATCH_STATUS:
            issues.append(f"watchlist_status_missing_or_invalid:{t}")
            w["status"] = "hold"
        else:
            w["status"] = st
        out.append(w)
    return out, issues


def validate_rejected_ideas(
    rejected: Any,
    *,
    min_n: int = 2,
    # 20, not 12: CLAUDE.md has told the brain ">=20 chars" for a seat-review
    # reason since the gate was written, so the prompt was STRICTER than the
    # code. A prompt that overstates its enforcement teaches the model to lean
    # on a rail that is not there; aligning the code is the safe direction to
    # resolve it, since the brain is already writing to this bar.
    min_reason_chars: int = 20,
    universe: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Normalize rejected_ideas for full-run no-trade discipline.

    Each item: {ticker, reason}. Returns (clean_list, issues).
    """
    issues: list[str] = []
    if rejected is None:
        return [], [f"rejected_ideas_missing_need_{min_n}"]
    if not isinstance(rejected, list):
        return [], ["rejected_ideas_not_list"]
    clean: list[dict] = []
    uni = {u.upper() for u in (universe or set())}
    for i, row in enumerate(rejected):
        if not isinstance(row, dict):
            issues.append(f"rejected_ideas[{i}]_not_object")
            continue
        t = str(row.get("ticker") or "").upper()
        reason = str(row.get("reason") or "").strip()
        if not t:
            issues.append(f"rejected_ideas[{i}]_missing_ticker")
            continue
        if uni and t not in uni:
            # Still accept — may be discovery name; flag only.
            issues.append(f"rejected_ideas_off_universe:{t}")
        if len(reason) < min_reason_chars:
            issues.append(f"rejected_ideas_reason_weak:{t}")
            continue
        clean.append({"ticker": t, "reason": reason[:400]})
    if len(clean) < min_n:
        issues.append(f"rejected_ideas_need_{min_n}_got_{len(clean)}")
    return clean, issues


def infer_rejected_from_no_trade_text(
    text: str | None,
    candidate_tickers: list[str] | None,
    *,
    min_n: int = 2,
) -> list[dict]:
    """Best-effort: pull candidate tickers mentioned in no_trade_reason.

    Used only as a soft backfill so older-style free-text reasons still
    partially satisfy the gate when the model forgot the array.
    """
    if not text or not candidate_tickers:
        return []
    cands = [str(t).upper() for t in candidate_tickers if t]
    found = []
    upper = text.upper()
    for t in cands:
        if t in upper or f"${t}" in upper:
            # Grab a short window around the mention as reason proxy
            idx = upper.find(t)
            snippet = text[max(0, idx - 20): idx + 80].strip()
            found.append({"ticker": t, "reason": snippet or f"mentioned in no_trade_reason ({t})"})
        if len(found) >= min_n:
            break
    return found


_SEAT_ACTIONS = ("keep", "trim", "swap", "reduce", "sell")
_MIN_SEAT_REASON_CHARS = 20


def normalize_seat_reviews(seat_reviews, held_tickers=None) -> tuple[list[dict], list[str]]:
    """Validate one seat_review per open holding. Returns (clean, issues).

    "Capital must re-earn its seat": on a trading depth with an open book, every
    holding owes a keep/trim/swap/reduce/sell with a real reason. Restored
    2026-07-19 after a `git reset --hard` wiped it; the BLOCKING_ISSUE_PREFIXES
    above depend on the issue codes emitted here, so without this the gate that
    blocks new BUYs had nothing to fire on.
    """
    issues: list[str] = []
    held = [str(t).upper() for t in (held_tickers or []) if t]
    if seat_reviews is None:
        if held:
            issues.append(f"seat_reviews_missing_need_{len(held)}_got_0")
        return [], issues
    if not isinstance(seat_reviews, list):
        issues.append("seat_reviews_not_a_list")
        return [], issues

    clean: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(seat_reviews):
        if not isinstance(row, dict):
            issues.append(f"seat_reviews[{i}]_not_object")
            continue
        t = str(row.get("ticker") or "").upper()
        if not t:
            issues.append(f"seat_reviews[{i}]_missing_ticker")
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in _SEAT_ACTIONS:
            issues.append(f"seat_reviews_action_invalid:{t}:{action or 'none'}")
            continue
        reason = str(row.get("reason") or "").strip()
        if len(reason) < _MIN_SEAT_REASON_CHARS:
            issues.append(f"seat_reviews_reason_weak:{t}")
            continue
        if t in seen:
            continue
        seen.add(t)
        clean.append({"ticker": t, "action": action, "reason": reason,
                      "challenger_considered": row.get("challenger_considered")})

    missing = [t for t in held if t not in seen]
    if missing:
        issues.append(f"seat_reviews_missing_holding:{','.join(sorted(missing))}")
    return clean, issues


def audit_brain_process(
    parsed: dict,
    *,
    depth: str,
    proposals: list | None = None,
    top_scan_tickers: list[str] | None = None,
    universe: set[str] | None = None,
    held_tickers: list[str] | None = None,
    require_seat_reviews: bool = False,
    require_factor_response: bool = False,
    factor_concentration_level: str | None = None,
    momentum_status: str | None = None,
    fired_triggers: list[str] | None = None,
    unbought_hits: dict[str, int] | None = None,
    requires_commitment: bool = False,
    max_unbought_hits: int = 3,
) -> dict:
    """Full process audit for a parsed brain response.

    Returns {
      watchlist: normalized,
      rejected_ideas: normalized,
      issues: [...],
      process_ok: bool,
      full_run_no_trade_ok: bool | None,
    }
    """
    proposals = proposals if proposals is not None else (parsed.get("proposals") or [])
    watchlist, w_issues = normalize_watchlist(parsed.get("watchlist"))
    issues = list(w_issues)

    rejected_raw = parsed.get("rejected_ideas")
    rejected, r_issues = validate_rejected_ideas(
        rejected_raw, min_n=2, universe=universe)

    full_run = depth in ("full",)
    no_buys = not any(str(p.get("action", "")).upper() == "BUY"
                      for p in proposals if isinstance(p, dict))
    full_run_no_trade_ok: bool | None = None

    if full_run and no_buys and not proposals:
        # Prefer explicit rejected_ideas; else try to infer from prose + scan tops.
        if r_issues and not rejected:
            inferred = infer_rejected_from_no_trade_text(
                parsed.get("no_trade_reason"), top_scan_tickers, min_n=2)
            if len(inferred) >= 2:
                rejected = inferred
                issues.append("rejected_ideas_inferred_from_no_trade_text")
            else:
                issues.extend(r_issues)
        elif r_issues:
            # Have some rows but not enough
            issues.extend([i for i in r_issues if i not in issues])
        full_run_no_trade_ok = len(rejected) >= 2 and not any(
            i.startswith("rejected_ideas_need_") for i in issues)
        if not full_run_no_trade_ok:
            issues.append("full_run_no_trade_missing_structured_rejections")
    elif full_run and no_buys and proposals:
        # Only sells/holds — structured rejections optional
        full_run_no_trade_ok = True
    else:
        # Trading depth with buys, or non-full depth
        full_run_no_trade_ok = None if not full_run else True

    # Watchlist: empty is ok on light news runs; flag empty on trading depths
    if depth in ("full", "holdings_watchlist") and not watchlist:
        issues.append("watchlist_empty_on_trading_depth")

    # Seat re-earn: every open holding owes a review on a trading depth.
    seat_reviews: list[dict] = []
    if require_seat_reviews or parsed.get("seat_reviews") is not None:
        seat_reviews, s_issues = normalize_seat_reviews(
            parsed.get("seat_reviews"), held_tickers if require_seat_reviews else None)
        issues.extend(s_issues)

    # Factor / unwind acknowledgment when concentration is high or extreme.
    factor_response = None
    if require_factor_response or parsed.get("factor_response") is not None:
        try:
            from tools.factor_map import validate_factor_response
            factor_response, f_issues = validate_factor_response(
                parsed.get("factor_response"),
                required=bool(require_factor_response),
                expected_level=factor_concentration_level,
            )
            issues.extend(f_issues)
        except Exception:
            pass  # pure module; never halt the audit on an import failure

    # SIGNAL DISCIPLINE (2026-08-03). Two documented failures that prose in
    # CLAUDE.md did not stop: writing watchlist triggers whose confirmation leg is
    # a measured-dead indicator (then never being able to clear them), and citing a
    # momentum unwind as grounds to stand down when it is a coded size haircut.
    # Both are now mechanical. Triggers are REWRITTEN, not just complained about —
    # the stripped version is what gets stored, so next cycle's alert fires on the
    # condition that actually survives measurement. Fail-soft: a broken import
    # leaves the audit exactly as it was.
    signal_notes: dict = {}
    try:
        from tools.signal_discipline import (
            sanitize_trigger, dead_indicator_veto, unwind_used_as_halt)

        trigger_fixes = []
        for w in watchlist:
            res = sanitize_trigger(w.get("would_buy_at"))
            if not res["changed"]:
                continue
            w["would_buy_at"] = res["trigger"]
            if res.get("original"):
                w["would_buy_at_original"] = res["original"]
            trigger_fixes.append({
                "ticker": w.get("ticker"),
                "original": res["original"],
                "rewritten": res["trigger"],
                "stripped": res["stripped"],
                "emptied": res["empty"],
            })
            issues.append(
                f"trigger_dead_confirmation_leg_stripped:{w.get('ticker')}")
        if trigger_fixes:
            signal_notes["trigger_fixes"] = trigger_fixes

        dead_vetoes = []
        for item in rejected:
            if not isinstance(item, dict):
                continue
            hit = dead_indicator_veto(item.get("reason"))
            if hit:
                hit["ticker"] = item.get("ticker")
                dead_vetoes.append(hit)
                issues.append(f"dead_indicator_veto:{item.get('ticker')}")
        if dead_vetoes:
            signal_notes["dead_indicator_vetoes"] = dead_vetoes

        halt = unwind_used_as_halt(
            parsed.get("no_trade_reason"), rejected,
            momentum_status=momentum_status)
        if halt and no_buys:
            signal_notes["unwind_used_as_halt"] = halt
            issues.append("unwind_used_as_halt")
    except Exception:
        pass  # pure module; never halt the audit on an import failure

    # ENGAGEMENT DISCIPLINE (2026-08-03). Everything above is about the QUALITY of
    # a decision; none of it costs anything to make no decision at all. A fired
    # trigger used to produce exactly one consequence anywhere in the codebase — a
    # shadow-book row for later grading — so ANET and GE could reach their own
    # stated levels across four runs on 07-23/24 for free. These gates never force
    # a trade; they force a resolution and, once the book has been flat for days, a
    # dated commitment. Fail-soft on import.
    engagement: dict = {}
    try:
        from tools.engagement import (
            decay_watchlist, validate_trigger_reviews, validate_waiting_for)
        from tools.signal_discipline import sanitize_trigger as _sanitize

        bought = [str(p.get("ticker") or "").upper() for p in proposals
                  if isinstance(p, dict)
                  and str(p.get("action", "")).upper() == "BUY"]
        t_reviews, t_issues = validate_trigger_reviews(
            parsed.get("trigger_reviews"), fired_triggers, bought)
        issues.extend(t_issues)

        # A requalified level must survive the same dead-indicator sanitizer as
        # any other trigger — otherwise "requalify" becomes the escape hatch that
        # smuggles a MACD/volume confirmation leg back in.
        for row in t_reviews:
            if row.get("action") != "requalify" or not row.get("new_trigger"):
                continue
            res = _sanitize(row["new_trigger"])
            if res["changed"]:
                row["new_trigger_original"] = row["new_trigger"]
                row["new_trigger"] = res["trigger"]
                issues.append(
                    f"trigger_requalify_dead_leg_stripped:{row.get('ticker')}")
            if res["empty"]:
                issues.append(
                    f"trigger_requalify_not_testable:{row.get('ticker')}")
        if t_reviews:
            engagement["trigger_reviews"] = t_reviews

        # Apply this run's own drop/requalify decisions before decay, so a name
        # the model just resolved is not also force-dropped for the same miss.
        resolved = {r["ticker"]: r for r in t_reviews}
        watchlist = [w for w in watchlist
                     if resolved.get(str(w.get("ticker") or "").upper(), {})
                     .get("action") != "drop"]
        for w in watchlist:
            r = resolved.get(str(w.get("ticker") or "").upper())
            if r and r.get("action") == "requalify" and r.get("new_trigger"):
                w["would_buy_at"] = r["new_trigger"]

        watchlist, decayed = decay_watchlist(
            watchlist, unbought_hits, max_unbought_hits=max_unbought_hits)
        if decayed:
            engagement["force_dropped"] = decayed
            issues.extend(f"watchlist_force_dropped:{d['ticker']}" for d in decayed)

        waiting, w_issues = validate_waiting_for(
            parsed.get("waiting_for"), required=bool(requires_commitment and no_buys))
        issues.extend(w_issues)
        if waiting:
            engagement["waiting_for"] = waiting
    except Exception:
        pass  # pure module; never halt the audit on an import failure

    process_ok = not any(
        i.startswith("watchlist_status_") or i.startswith("full_run_no_trade")
        or i.startswith("rejected_ideas_need")
        or i.startswith("seat_reviews_missing")
        or i.startswith("seat_reviews_action_invalid")
        or i.startswith("seat_reviews_reason_weak")
        or i.startswith("factor_response_missing")
        or i.startswith("factor_response_plan_weak")
        or i.startswith("factor_response_actions_empty")
        # Engagement failures are process failures. They do NOT block a BUY —
        # blocking on these would punish the run for trying to act, which is the
        # opposite of the point — but a run that ignored its own fired trigger or
        # owes a dated commitment is not process_ok.
        or i.startswith("trigger_reviews_missing")
        or i.startswith("trigger_reviews_action_invalid")
        or i.startswith("trigger_reviews_reason_weak")
        or i.startswith("trigger_reviews_requalify_without_new_trigger")
        or i.startswith("waiting_for_")
        for i in issues
    )
    blocking = [i for i in issues if i.startswith(BLOCKING_ISSUE_PREFIXES)]
    return {
        "watchlist": watchlist,
        "rejected_ideas": rejected,
        "seat_reviews": seat_reviews,
        "factor_response": factor_response,
        "issues": issues,
        "signal_discipline": signal_notes,
        "engagement": engagement,
        "process_ok": process_ok,
        "blocking_issues": blocking,
        "blocks_new_buys": bool(blocking),
        "full_run_no_trade_ok": full_run_no_trade_ok,
        "note": "Process gates for learning discipline. Issues are journaled; "
                "they do not halt the run. Fix the schema next cycle.",
    }
