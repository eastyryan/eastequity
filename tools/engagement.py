"""Engagement discipline — make passing on your own setup cost something.

WHY THIS EXISTS (2026-08-03).

Every other fix made this month was PERMISSIVE: the earnings blackout was
narrowed, the beta gate stopped failing closed on a new name, dead-indicator
vetoes were caught, a repair round was added, a second full scan was scheduled.
All of that removes obstacles. None of it creates any reason to ACT, and the
record says the obstacles were never the binding constraint:

  * 165 runs produced 11 proposals. Of the 30 logged rejections, ZERO failed on
    risk_reward_ratio or target upside and only 2 on confidence — the coded entry
    floors almost never bit. The self-censorship happened upstream, before a
    proposal was written.
  * A fired watchlist trigger cost nothing. The only consequence anywhere in the
    codebase was a shadow-book record for later grading. ANET and GE reached
    their own stated levels across four separate runs on 07-23/24 and each pass
    was free, so the same name could sit at the same unmet trigger indefinitely.
  * The shadow book has been BINDING for weeks — 56 closed, 23 regret_miss vs 33
    good_skip, against a threshold of 8 — and nothing changed, because "binding"
    meant the text appeared in the context pack.

So this module adds the missing half. It never forces a trade — a fabricated
setup teaches the learning loops garbage, which is strictly worse than silence.
What it forces is a DECISION and a COMMITMENT:

  1. `validate_trigger_reviews` — a fired trigger must resolve to buy, drop, or
     requalify (with a NEW level). "Hold, unchanged" for the fifth run is no
     longer an available answer.
  2. `decay_watchlist` — a name that hits its level `max_unbought_hits` times
     without being bought is REMOVED from the stored watchlist. If it comes back
     it comes back re-underwritten from scratch, not inherited.
  3. `deployment_status` — consecutive days at ~all cash. Past the threshold the
     run owes either a proposal or a specific, dated condition it is waiting for.

Pure helpers (offline-testable): no network, no clock beyond the date passed in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

TRIGGER_ACTIONS = ("buy", "drop", "requalify")
MIN_TRIGGER_REASON_CHARS = 25
MIN_NEW_TRIGGER_CHARS = 5


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 1. Trigger accountability
# ---------------------------------------------------------------------------
def validate_trigger_reviews(trigger_reviews: Any,
                             fired_tickers: list[str] | None,
                             bought_tickers: list[str] | None = None,
                             ) -> tuple[list[dict], list[str]]:
    """One resolution per fired trigger: buy | drop | requalify.

    A name whose trigger fired AND which the run actually proposed a BUY for
    needs no review — the action speaks. Everything else owes a row.

    `requalify` means "I still want this name but my level was wrong": it must
    carry a `new_trigger`, and that trigger is run through the dead-indicator
    sanitizer by the caller, so you cannot requalify onto a confirmation leg the
    ablation study already killed.
    """
    issues: list[str] = []
    fired = [str(t).upper() for t in (fired_tickers or []) if t]
    bought = {str(t).upper() for t in (bought_tickers or []) if t}
    owed = [t for t in fired if t not in bought]
    if not owed:
        return [], issues

    if trigger_reviews is None:
        return [], [f"trigger_reviews_missing_need_{len(owed)}_got_0"]
    if not isinstance(trigger_reviews, list):
        return [], ["trigger_reviews_not_a_list"]

    clean: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(trigger_reviews):
        if not isinstance(row, dict):
            issues.append(f"trigger_reviews[{i}]_not_object")
            continue
        t = str(row.get("ticker") or "").upper()
        if not t:
            issues.append(f"trigger_reviews[{i}]_missing_ticker")
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in TRIGGER_ACTIONS:
            issues.append(f"trigger_reviews_action_invalid:{t}:{action or 'none'}")
            continue
        reason = str(row.get("reason") or "").strip()
        if len(reason) < MIN_TRIGGER_REASON_CHARS:
            issues.append(f"trigger_reviews_reason_weak:{t}")
            continue
        new_trigger = str(row.get("new_trigger") or "").strip()
        if action == "requalify" and len(new_trigger) < MIN_NEW_TRIGGER_CHARS:
            # Requalifying without a new level is just "hold" wearing a new name —
            # the exact answer this gate exists to remove.
            issues.append(f"trigger_reviews_requalify_without_new_trigger:{t}")
            continue
        if t in seen:
            continue
        seen.add(t)
        clean.append({"ticker": t, "action": action, "reason": reason[:400],
                      "new_trigger": new_trigger or None})

    missing = [t for t in owed if t not in seen]
    if missing:
        issues.append(
            f"trigger_reviews_missing_need_{len(owed)}_got_{len(seen)}"
            f":{','.join(sorted(missing)[:6])}")
    return clean, issues


# ---------------------------------------------------------------------------
# 2. Watchlist decay
# ---------------------------------------------------------------------------
def count_unbought_hits(outcomes: Any, today: Any = None) -> dict[str, int]:
    """Distinct DAYS each watched name sat at its own trigger without being bought.

    Reads dashboard/data/watchlist_outcomes.json rows, which already carry
    hit_buy_level / hit_date / acted and reset hit state whenever the stated
    level changes. Counting DAYS rather than runs matters: five runs on one day
    is one miss, not five.
    """
    out: dict[str, int] = {}
    rows = outcomes if isinstance(outcomes, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        t = str(row.get("ticker") or "").upper()
        if not t or row.get("acted"):
            continue
        days = row.get("unbought_hit_days")
        if isinstance(days, list):
            out[t] = len({str(d)[:10] for d in days if d})
        elif isinstance(days, int):
            out[t] = days
        elif row.get("hit_buy_level"):
            out[t] = 1
    return out


def decay_watchlist(watchlist: Any, unbought_hits: dict[str, int] | None,
                    *, max_unbought_hits: int = 3,
                    ) -> tuple[list[dict], list[dict]]:
    """Force-drop names that keep reaching their level and keep not being bought.

    Returns (kept, dropped). A dropped name is not banned — it is un-inherited.
    If it belongs on the list it must be re-argued from scratch next run, which
    is the point: the ANET/GE loop ran because a stale entry cost nothing to
    carry forward.
    """
    hits = unbought_hits or {}
    kept: list[dict] = []
    dropped: list[dict] = []
    for row in (watchlist if isinstance(watchlist, list) else []):
        if not isinstance(row, dict):
            continue
        t = str(row.get("ticker") or "").upper()
        n = hits.get(t, 0)
        if t and n >= max_unbought_hits and str(row.get("status")) != "buy":
            dropped.append({
                "ticker": t, "unbought_hits": n,
                "would_buy_at": row.get("would_buy_at"),
                "note": (f"reached its own stated level on {n} separate days "
                         f"without being bought — force-dropped so it must be "
                         f"re-underwritten from scratch rather than inherited"),
            })
            continue
        kept.append(row)
    return kept, dropped


# ---------------------------------------------------------------------------
# 3. Cash-drag accountability
# ---------------------------------------------------------------------------
def deployment_status(positions: Any, equity: Any, prior: dict | None,
                      today: Any = None, *,
                      flat_threshold_pct: float = 0.05,
                      commitment_after_days: int = 3) -> dict:
    """How long the book has been sitting in cash, and what that now requires.

    `prior` is the previous run's block (state/engagement.json), so the counter
    survives restarts without re-deriving history. Returns a new block to persist
    plus the flags the bundle and process gates read.

    This never demands a trade. Past the threshold it demands a COMMITMENT: name
    the specific, dated condition being waited for, so "nothing clears the bar"
    stops being a renewable answer that carries no information.
    """
    day = _as_date(today) or _as_date((prior or {}).get("as_of"))
    try:
        eq = float(equity)
    except (TypeError, ValueError):
        eq = 0.0
    mv = 0.0
    for p in (positions if isinstance(positions, list) else []):
        if not isinstance(p, dict):
            continue
        try:
            mv += float(p.get("market_value_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    deployed = (mv / eq) if eq > 0 else 0.0
    flat = deployed < flat_threshold_pct

    prior = prior or {}
    flat_since = _as_date(prior.get("flat_since"))
    prior_day = _as_date(prior.get("as_of"))

    if not flat:
        flat_since = None
        flat_days = 0
    else:
        if flat_since is None:
            flat_since = day
        # Count DISTINCT DAYS, so a day with seven runs advances the counter once.
        flat_days = ((day - flat_since).days + 1) if (day and flat_since) else 1
        if day and prior_day and day == prior_day:
            flat_days = max(int(prior.get("flat_days") or 0), flat_days)

    return {
        "as_of": day.isoformat() if day else None,
        "deployed_pct": round(deployed * 100, 1),
        "cash_pct": round(max(0.0, 1 - deployed) * 100, 1),
        "open_positions": len([p for p in (positions or [])
                               if isinstance(p, dict)]),
        "flat": flat,
        "flat_since": flat_since.isoformat() if flat_since else None,
        "flat_days": flat_days,
        "requires_commitment": bool(flat and flat_days >= commitment_after_days),
        "commitment_after_days": commitment_after_days,
        "note": (
            f"The book has been effectively all cash for {flat_days} consecutive "
            f"day(s). At {commitment_after_days}+ a no-trade run owes a "
            f"`waiting_for` object: the specific, DATED condition being waited "
            f"for and the name it applies to. This is not a demand to trade — a "
            f"fabricated setup is worse than silence — it is a demand that "
            f"'nothing clears the bar' stop being a renewable answer carrying no "
            f"information."
        ) if flat else (
            f"Deployed {round(deployed * 100, 1)}% of equity across "
            f"{len([p for p in (positions or []) if isinstance(p, dict)])} "
            f"position(s)."),
    }


def validate_waiting_for(waiting_for: Any, *, required: bool) -> tuple[dict | None, list[str]]:
    """The commitment owed once the book has been flat past the threshold.

    Shape: {"ticker": "X", "condition": "...", "by_date": "YYYY-MM-DD"}.
    A condition without a date is a mood; a date is what makes it gradeable next
    week. Accepts a list and takes the first well-formed entry.
    """
    if not required:
        return (waiting_for if isinstance(waiting_for, dict) else None), []
    if isinstance(waiting_for, list):
        waiting_for = next((w for w in waiting_for if isinstance(w, dict)), None)
    if not isinstance(waiting_for, dict):
        return None, ["waiting_for_missing"]
    condition = str(waiting_for.get("condition") or "").strip()
    by_date = _as_date(waiting_for.get("by_date"))
    issues: list[str] = []
    if len(condition) < 25:
        issues.append("waiting_for_condition_weak")
    if by_date is None:
        issues.append("waiting_for_missing_by_date")
    if issues:
        return None, issues
    return {
        "ticker": str(waiting_for.get("ticker") or "").upper() or None,
        "condition": condition[:400],
        "by_date": by_date.isoformat(),
    }, []


def stale_commitments(prior_waiting: Any, today: Any = None) -> list[dict]:
    """Commitments whose by_date has passed — the thing that makes this bite.

    A dated condition nobody revisits is the same as no condition. These are fed
    back so the next run must say what happened: did it fire and get acted on, or
    did it not fire and the name should go?
    """
    day = _as_date(today)
    out: list[dict] = []
    rows = prior_waiting if isinstance(prior_waiting, list) else (
        [prior_waiting] if isinstance(prior_waiting, dict) else [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        by = _as_date(row.get("by_date"))
        if by and day and by < day:
            out.append({**row, "days_overdue": (day - by).days,
                        "note": "this dated condition has expired — say plainly "
                                "whether it fired and you acted, or it did not "
                                "and the name should be dropped"})
    return out
