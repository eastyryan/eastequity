"""Portfolio competition — every open seat re-earns capital vs challengers.

Deterministic pre-compute for the brain: for each holding, surface the best
alternate uses of that capital from the current scan (same-theme peers and
cross-theme fat pitches). Score gaps produce a competition_hint so the agent
can keep / trim / swap / sell with evidence — pure opportunity-cost ranking,
not a trade signal by itself.

Pure helpers are offline-testable. No network.

CLI: python -m tools.portfolio_competition
"""

from __future__ import annotations

from typing import Any

from tools.demand_drivers import build_driver_map, driver_for_ticker

NOTE = (
    "PROFIT DISCIPLINE: capital only stays where it still beats the next best idea. "
    "For every open seat, read competition_hint + challengers. Respond in "
    "seat_reviews (JSON) with keep|trim|swap|reduce|sell and a reason that names "
    "the challenger you considered (or why none clear the bar). A seat under "
    "pressure is not an automatic sell — but 'thesis still holds' alone is not "
    "enough when a higher-scoring alternative is available."
)

VALID_SEAT_ACTIONS = frozenset({"keep", "trim", "swap", "reduce", "sell"})

# How many challengers to attach per holding
_MAX_CHALLENGERS = 3
# Score gap (challenger - holding) above this → under_pressure
_UNDER_PRESSURE_GAP = 0.5
# Absolute score gap under this with both scored → contested
_CONTESTED_GAP = 0.5


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def collect_scan_rows(scan: dict | None) -> list[dict]:
    """Flatten published scanner lanes into one list of row dicts."""
    if not isinstance(scan, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for key in (
        "top_setups", "contrarian_setups", "deep_value_200w", "supplier_pullbacks",
    ):
        for r in scan.get(key) or []:
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").upper()
            if not t or t in seen:
                continue
            seen.add(t)
            row = dict(r)
            row["ticker"] = t
            row["_lane"] = key
            out.append(row)
    return out


def setup_score(row: dict | None) -> float | None:
    """Prefer scanner swing_setup_score; else a small composite from RS + structure."""
    if not isinstance(row, dict):
        return None
    s = _f(row.get("swing_setup_score"))
    if s is not None:
        return round(s, 3)
    # Lightweight fallback when enrich/light scan omitted the score
    parts = []
    rs = _f(row.get("rel_strength_1m_pct"))
    if rs is not None:
        parts.append(max(min(rs / 10.0, 1.5), -1.0))
    mom = _f(row.get("momentum_1m_pct"))
    if mom is not None:
        parts.append(max(min(mom / 15.0, 1.0), -0.5))
    if row.get("above_50dma") is True:
        parts.append(0.4)
    if row.get("above_200dma") is True:
        parts.append(0.3)
    vs = row.get("volume_signal") if isinstance(row.get("volume_signal"), dict) else {}
    read = vs.get("read") or ""
    if read in ("pocket_pivot", "confirmed_breakout", "no_supply_pullback"):
        parts.append(0.5)
    if not parts:
        return None
    return round(sum(parts), 3)


def _row_index(rows: list[dict]) -> dict[str, dict]:
    return {str(r.get("ticker") or "").upper(): r for r in rows if r.get("ticker")}


def pick_challengers(
    holding_ticker: str,
    holding_driver: str,
    rows: list[dict],
    held: set[str],
    driver_map: dict[str, str],
    *,
    max_n: int = _MAX_CHALLENGERS,
) -> list[dict]:
    """Select up to max_n challengers: best same-driver + best other-driver."""
    ht = holding_ticker.upper()
    candidates: list[dict] = []
    for r in rows:
        t = str(r.get("ticker") or "").upper()
        if not t or t == ht or t in held:
            continue
        d = str(driver_map.get(t) or driver_for_ticker(t) or "other").lower()
        sc = setup_score(r)
        candidates.append({
            "ticker": t,
            "demand_driver": d,
            "setup_score": sc,
            "swing_setup_score": r.get("swing_setup_score"),
            "rel_strength_1m_pct": r.get("rel_strength_1m_pct"),
            "rel_strength_3m_pct": r.get("rel_strength_3m_pct"),
            "momentum_1m_pct": r.get("momentum_1m_pct"),
            "last_close": r.get("last_close"),
            "volume_read": (r.get("volume_signal") or {}).get("read")
            if isinstance(r.get("volume_signal"), dict) else None,
            "lane": r.get("_lane") or r.get("lane"),
            "same_theme": d == holding_driver,
        })

    same = sorted(
        (c for c in candidates if c["same_theme"] and c["setup_score"] is not None),
        key=lambda c: c["setup_score"], reverse=True)
    other = sorted(
        (c for c in candidates if not c["same_theme"] and c["setup_score"] is not None),
        key=lambda c: c["setup_score"], reverse=True)
    # Also allow unscored names at the end if we have none scored
    unscored = [c for c in candidates if c["setup_score"] is None]

    picked: list[dict] = []
    seen: set[str] = set()

    def _add(c: dict, why: str):
        if c["ticker"] in seen or len(picked) >= max_n:
            return
        seen.add(c["ticker"])
        out = dict(c)
        out["why"] = why
        picked.append(out)

    if same:
        _add(same[0], "same demand_driver, higher/competitive scan setup score")
    if other:
        _add(other[0], "different demand_driver — diversification / opportunity-cost peer")
    # Fill remaining with next-best overall
    rest = sorted(
        (c for c in candidates if c["ticker"] not in seen and c["setup_score"] is not None),
        key=lambda c: c["setup_score"], reverse=True)
    for c in rest:
        _add(c, "top remaining scan setup")
        if len(picked) >= max_n:
            break
    if len(picked) < max_n:
        for c in unscored:
            _add(c, "scan presence only (no setup score)")
            if len(picked) >= max_n:
                break
    return picked


def competition_hint(
    holding_score: float | None,
    challengers: list[dict],
) -> dict:
    """Map scores → defensible | contested | under_pressure | no_challenger | sparse."""
    scored = [c for c in challengers if isinstance(c.get("setup_score"), (int, float))]
    if not challengers:
        return {
            "label": "no_challenger",
            "score_gap_vs_best": None,
            "best_challenger": None,
            "note": "No alternate names available in this scan — re-underwrite vs plan only.",
        }
    if holding_score is None and not scored:
        return {
            "label": "sparse",
            "score_gap_vs_best": None,
            "best_challenger": challengers[0]["ticker"] if challengers else None,
            "note": "Insufficient scan scores — judge on thesis/geometry, not rank.",
        }
    if not scored:
        return {
            "label": "sparse",
            "score_gap_vs_best": None,
            "best_challenger": challengers[0]["ticker"],
            "note": "Challengers lack scores — qualitative comparison only.",
        }
    best = max(scored, key=lambda c: c["setup_score"])
    gap = None
    if holding_score is not None:
        # positive gap => holding leads; negative => challenger leads
        gap = round(holding_score - float(best["setup_score"]), 3)
        if gap <= -_UNDER_PRESSURE_GAP:
            label = "under_pressure"
            note = (
                f"{best['ticker']} scores {best['setup_score']} vs seat {holding_score} "
                f"(gap {gap}). Capital must re-earn: keep only with a clear edge the "
                "score misses (structure, catalyst, ownership, invalidators still clean)."
            )
        elif abs(gap) <= _CONTESTED_GAP:
            label = "contested"
            note = (
                f"Seat and {best['ticker']} are close on setup score. Prefer the cleaner "
                "geometry / catalyst / theme fit; trim is allowed if size is large."
            )
        else:
            label = "defensible"
            note = (
                f"Seat still ranks ahead of best challenger {best['ticker']} on setup "
                "score — still re-check thesis invalidators before keep."
            )
    else:
        label = "under_pressure" if float(best["setup_score"]) >= 2.0 else "contested"
        gap = None
        note = (
            f"Seat missing scan score; best challenger {best['ticker']} @ "
            f"{best['setup_score']}. Default to scrutinize keep."
        )
    return {
        "label": label,
        "score_gap_vs_best": gap,
        "best_challenger": best["ticker"],
        "note": note,
    }


def build_seat_card(
    pos: dict,
    rows_by_ticker: dict[str, dict],
    all_rows: list[dict],
    held: set[str],
    driver_map: dict[str, str],
) -> dict:
    """One holding's competition card (pure)."""
    t = str(pos.get("ticker") or "").upper()
    driver = str(
        pos.get("demand_driver")
        or driver_map.get(t)
        or driver_for_ticker(t)
        or "other"
    ).lower()
    row = rows_by_ticker.get(t) or {}
    h_score = setup_score(row) if row else None
    challengers = pick_challengers(t, driver, all_rows, held, driver_map)
    hint = competition_hint(h_score, challengers)

    try:
        mv = float(pos.get("market_value_usd") or 0)
    except (TypeError, ValueError):
        mv = 0.0
    try:
        qty = float(pos.get("qty") or pos.get("quantity") or 0)
        avg = float(pos.get("avg_cost") or pos.get("avg_entry_price") or 0)
        last = float(pos.get("last_price") or row.get("last_close") or 0)
        pnl_pct = round((last / avg - 1) * 100, 2) if avg > 0 and last > 0 else None
    except (TypeError, ValueError):
        pnl_pct = None

    return {
        "ticker": t,
        "demand_driver": driver,
        "market_value_usd": round(mv, 2) if mv else pos.get("market_value_usd"),
        "unrealized_pnl_pct": pnl_pct,
        "setup_score": h_score,
        "scan_present": bool(row),
        "rel_strength_1m_pct": row.get("rel_strength_1m_pct"),
        "volume_read": (row.get("volume_signal") or {}).get("read")
        if isinstance(row.get("volume_signal"), dict) else None,
        "challengers": challengers,
        "competition_hint": hint["label"],
        "score_gap_vs_best": hint["score_gap_vs_best"],
        "best_challenger": hint["best_challenger"],
        "hint_note": hint["note"],
        "required_response": (
            f"seat_reviews entry for {t}: action keep|trim|swap|reduce|sell + "
            f"reason naming challenger considered (best={hint['best_challenger']})."
        ),
    }


def build_portfolio_competition(
    portfolio: dict | None,
    scan: dict | None = None,
    driver_map: dict[str, str] | None = None,
    *,
    watchlist_tickers: list[str] | None = None,
) -> dict:
    """Full competition pack for the context bundle."""
    positions = []
    if isinstance(portfolio, dict):
        positions = [p for p in (portfolio.get("positions") or []) if isinstance(p, dict)]
    if not positions:
        return {
            "status": "empty_book",
            "note": NOTE,
            "seats": [],
            "n_seats": 0,
            "under_pressure": [],
            "how_to_respond": "No open seats — competition block is idle.",
        }

    dmap = driver_map if isinstance(driver_map, dict) else build_driver_map()
    rows = collect_scan_rows(scan)
    # Inject bare watchlist tickers as weak challenger seeds when scan missed them
    if watchlist_tickers:
        have = {r["ticker"] for r in rows}
        for w in watchlist_tickers:
            tu = str(w or "").upper()
            if tu and tu not in have:
                rows.append({"ticker": tu, "_lane": "watchlist_seed"})
    by_t = _row_index(rows)
    held = {str(p.get("ticker") or "").upper() for p in positions if p.get("ticker")}

    seats = [
        build_seat_card(p, by_t, rows, held, dmap)
        for p in positions if p.get("ticker")
    ]
    under = [s["ticker"] for s in seats if s.get("competition_hint") == "under_pressure"]
    contested = [s["ticker"] for s in seats if s.get("competition_hint") == "contested"]

    return {
        "status": "ok",
        "note": NOTE,
        "n_seats": len(seats),
        "seats": seats,
        "under_pressure": under,
        "contested": contested,
        "how_to_respond": (
            "Output seat_reviews: one object per open ticker with "
            "{ticker, action: keep|trim|swap|reduce|sell, reason, challenger_considered}. "
            "If action is swap, also propose SELL_TO_CLOSE (or reduce) and the BUY that "
            "takes the capital — or explain why swap waits. under_pressure seats need an "
            "explicit keep justification or a size/capital action."
        ),
    }


def validate_seat_reviews(
    seat_reviews: Any,
    held_tickers: list[str] | None,
    *,
    min_reason_chars: int = 12,
) -> tuple[list[dict], list[str]]:
    """Normalize brain seat_reviews. Returns (clean, issues)."""
    held = [str(t).upper() for t in (held_tickers or []) if t]
    issues: list[str] = []
    if not held:
        return [], []
    if seat_reviews is None:
        return [], [f"seat_reviews_missing_need_{len(held)}"]
    if not isinstance(seat_reviews, list):
        return [], ["seat_reviews_not_list"]

    clean: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(seat_reviews):
        if not isinstance(row, dict):
            issues.append(f"seat_reviews[{i}]_not_object")
            continue
        t = str(row.get("ticker") or "").upper()
        action = str(row.get("action") or "").strip().lower()
        reason = str(row.get("reason") or "").strip()
        ch = row.get("challenger_considered")
        ch_s = str(ch).upper() if ch else None
        if not t:
            issues.append(f"seat_reviews[{i}]_missing_ticker")
            continue
        if action not in VALID_SEAT_ACTIONS:
            issues.append(f"seat_reviews_action_invalid:{t}")
            continue
        if len(reason) < min_reason_chars:
            issues.append(f"seat_reviews_reason_weak:{t}")
            continue
        if t in seen:
            issues.append(f"seat_reviews_duplicate:{t}")
            continue
        seen.add(t)
        entry = {"ticker": t, "action": action, "reason": reason[:500]}
        if ch_s:
            entry["challenger_considered"] = ch_s
        clean.append(entry)

    missing = [t for t in held if t not in seen]
    for t in missing:
        issues.append(f"seat_reviews_missing_holding:{t}")
    extra = [t for t in seen if t not in held]
    for t in extra:
        issues.append(f"seat_reviews_unknown_holding:{t}")

    return clean, issues


if __name__ == "__main__":
    import json
    from tools.portfolio_state import get_portfolio_state
    port = get_portfolio_state()
    print(json.dumps(build_portfolio_competition(port, scan=None), indent=2))
