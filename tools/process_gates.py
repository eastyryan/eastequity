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
    min_reason_chars: int = 12,
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


def audit_brain_process(
    parsed: dict,
    *,
    depth: str,
    proposals: list | None = None,
    top_scan_tickers: list[str] | None = None,
    universe: set[str] | None = None,
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

    process_ok = not any(
        i.startswith("watchlist_status_") or i.startswith("full_run_no_trade")
        or i.startswith("rejected_ideas_need")
        for i in issues
    )
    return {
        "watchlist": watchlist,
        "rejected_ideas": rejected,
        "issues": issues,
        "process_ok": process_ok,
        "full_run_no_trade_ok": full_run_no_trade_ok,
        "note": "Process gates for learning discipline. Issues are journaled; "
                "they do not halt the run. Fix the schema next cycle.",
    }
