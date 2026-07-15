"""Research Feedback — opportunity-cost signals + required reasoning order.

Surfaces watchlist foresight (names that hit buy levels or ran without action)
and a static process checklist so the brain's context bundle always includes
both "what you watched but missed" and "how you must reason this run."

Wire points (orchestrator imports these — do not call from here):
  * gather_context: include brain_facing_watchlist_feedback(),
    brain_facing_process_checklist(), and (from exit_autopsy)
    brain_facing_exit_lessons() in the context bundle.
  * Exit autopsies themselves are written on forced exit / SELL_TO_CLOSE fill
    via tools.exit_autopsy (see that module's docstring).

All public APIs are fail-soft and never raise.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTCOMES_FILE = ROOT / "dashboard" / "data" / "watchlist_outcomes.json"

# Absolute move since first watched that counts as "you left money on the table"
# when the name was never bought (opportunity-cost surface).
_LARGE_MOVE_PCT = 5.0


def load_watchlist_outcomes() -> list[dict]:
    """Load dashboard/data/watchlist_outcomes.json; [] on any failure."""
    try:
        if not OUTCOMES_FILE.exists():
            return []
        data = json.loads(OUTCOMES_FILE.read_text())
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        return []
    except Exception:
        return []


def _move_pct(row: dict) -> float | None:
    try:
        v = row.get("move_pct_since_watched")
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _compact_outcome(row: dict, why: str) -> dict:
    return {
        "ticker": row.get("ticker"),
        "why": why,
        "currently_watched": bool(row.get("currently_watched")),
        "hit_buy_level": bool(row.get("hit_buy_level")),
        "acted": bool(row.get("acted")),
        "move_pct_since_watched": row.get("move_pct_since_watched"),
        "price_when_added": row.get("price_when_added"),
        "latest_price": row.get("latest_price"),
        "would_buy_at": row.get("would_buy_at"),
        "one_line": row.get("one_line"),
        "first_watched": row.get("first_watched"),
        "hit_date": row.get("hit_date"),
        "dropped_date": row.get("dropped_date"),
        "parsed_level": row.get("parsed_level"),
    }


def brain_facing_watchlist_feedback(limit: int = 12) -> dict:
    """Opportunity-cost style notes from persisted watchlist outcomes.

    Highlights:
      * currently watched, hit buy level, never acted  -> hits_not_bought
      * large move_pct_since_watched without acting    -> opportunity_cost
      * dropped (not currently watched) with big moves -> opportunity_cost

    Shape:
      {"note": "...", "opportunity_cost": [...], "hits_not_bought": [...],
       "stats": {...}}
    """
    try:
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 12

        rows = load_watchlist_outcomes()
        hits_not_bought: list[dict] = []
        opportunity_cost: list[dict] = []
        seen_oc: set[str] = set()

        n_total = len(rows)
        n_acted = sum(1 for r in rows if r.get("acted"))
        n_hit = sum(1 for r in rows if r.get("hit_buy_level"))
        n_hit_not_acted = 0
        n_watched = sum(1 for r in rows if r.get("currently_watched"))
        n_dropped = sum(1 for r in rows if not r.get("currently_watched"))

        for r in rows:
            ticker = str(r.get("ticker") or "").upper()
            if not ticker:
                continue
            acted = bool(r.get("acted"))
            hit = bool(r.get("hit_buy_level"))
            watched = bool(r.get("currently_watched"))
            move = _move_pct(r)

            if watched and hit and not acted:
                n_hit_not_acted += 1
                hits_not_bought.append(
                    _compact_outcome(
                        r,
                        "buy level hit while still on watchlist — not bought",
                    )
                )

            # Opportunity cost: large move without acting (watched or dropped).
            if not acted and move is not None and abs(move) >= _LARGE_MOVE_PCT:
                if watched:
                    why = (
                        f"still watched, moved {move:+.1f}% since added, never bought"
                    )
                else:
                    why = (
                        f"dropped from watchlist, moved {move:+.1f}% since first watched, "
                        "never bought"
                    )
                if ticker not in seen_oc:
                    seen_oc.add(ticker)
                    opportunity_cost.append(_compact_outcome(r, why))

            # Dropped with big moves already covered above; also surface dropped
            # hit_buy_level never acted even if move < threshold (foresight miss).
            if (not watched) and hit and not acted and ticker not in seen_oc:
                # Only add to OC if not already there from large-move path.
                if move is None or abs(move) < _LARGE_MOVE_PCT:
                    seen_oc.add(ticker)
                    opportunity_cost.append(
                        _compact_outcome(
                            r,
                            "hit buy level then dropped — never bought",
                        )
                    )

        # Rank opportunity cost by |move| desc; hits_not_bought by hit_date / ticker.
        def _abs_move(item: dict) -> float:
            try:
                return abs(float(item.get("move_pct_since_watched") or 0))
            except (TypeError, ValueError):
                return 0.0

        opportunity_cost.sort(key=_abs_move, reverse=True)
        hits_not_bought.sort(
            key=lambda x: (x.get("hit_date") or "", x.get("ticker") or ""),
            reverse=True,
        )

        if limit:
            opportunity_cost = opportunity_cost[:limit]
            hits_not_bought = hits_not_bought[:limit]

        return {
            "note": (
                "Watchlist foresight / opportunity-cost feedback. hits_not_bought = "
                "names still on the watchlist whose stated buy level was reached but "
                "you never bought. opportunity_cost = large moves (or hit-then-dropped) "
                "without action — grade whether the pass was discipline or a miss. "
                "Do NOT chase a name just because it moved; use this to sharpen when "
                "you write would_buy_at and when you act on triggers."
            ),
            "opportunity_cost": opportunity_cost,
            "hits_not_bought": hits_not_bought,
            "stats": {
                "tracked": n_total,
                "currently_watched": n_watched,
                "dropped": n_dropped,
                "acted": n_acted,
                "hit_buy_level": n_hit,
                "hit_buy_level_not_acted_still_watched": n_hit_not_acted,
                "large_move_threshold_pct": _LARGE_MOVE_PCT,
            },
        }
    except Exception:
        return {
            "note": "watchlist feedback unavailable",
            "opportunity_cost": [],
            "hits_not_bought": [],
            "stats": {},
        }


def brain_facing_process_checklist() -> dict:
    """Static required reasoning order for the brain (context bundle).

    Order: regime -> book -> idea -> geometry -> kill.
    """
    return {
        "note": (
            "Required reasoning order every run. Work top-down; do not propose a BUY "
            "before completing the steps above it. Cite the relevant context-bundle "
            "blocks in commentary when a step changes your aggression."
        ),
        "order": ["regime", "book", "idea", "geometry", "kill"],
        "steps": [
            {
                "id": "regime",
                "title": "Macro regime check",
                "must": (
                    "Read macro_regime (+ market_events / market_news tape). State "
                    "whether the regime supports adding long swing exposure. Hostile "
                    "or rolling leadership -> raise the bar, prefer cash / exits."
                ),
                "context_keys": ["macro_regime", "market_events", "market_news"],
            },
            {
                "id": "book",
                "title": "Portfolio / book review",
                "must": (
                    "Re-underwrite every open position with fresh research: HOLD or "
                    "SELL_TO_CLOSE. Thesis broken = exit. Cash test: if remaining "
                    "expected return no longer clearly beats cash yield, rotate. "
                    "Honor stops, stall flags, and forced-exit aftermath."
                ),
                "context_keys": [
                    "portfolio",
                    "position_stop_cushion",
                    "forced_exits",
                    "exit_lessons",
                ],
            },
            {
                "id": "idea",
                "title": "Idea selection",
                "must": (
                    "From universe_scan / watchlist / tape promotions, pick at most a "
                    "handful of candidates. Prefer fat pitches over marginal RR. Empty "
                    "proposals with a clear no_trade_reason is a valid outcome."
                ),
                "context_keys": [
                    "universe_scan",
                    "watchlist_triggers",
                    "tape_focus_promotions",
                    "watchlist_feedback",
                    "discovery",
                ],
            },
            {
                "id": "geometry",
                "title": "Entry geometry & underwrite",
                "must": (
                    "For each candidate: charts, volume, stop/target geometry, "
                    "fundamentals freshness, filings, catalysts. WebSearch mandatory "
                    "before proposing a BUY. Variant perception required. Size and "
                    "RR must clear validator floors as a MINIMUM, not a target."
                ),
                "context_keys": [
                    "deep_fundamentals",
                    "sec_filings",
                    "filing_texts",
                    "news_and_catalysts",
                    "options_signals",
                    "stop_engineering",
                    "portfolio_risk",
                ],
            },
            {
                "id": "kill",
                "title": "Kill conditions",
                "must": (
                    "State what kills the trade and how you would know early "
                    "(risk_map). Pre-plan stop, horizon, and invalidation before "
                    "size. If kill conditions are fuzzy, do not buy — watchlist it."
                ),
                "context_keys": ["hard_limits", "track_record", "lessons_learned"],
            },
        ],
    }
