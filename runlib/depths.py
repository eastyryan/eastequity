"""Tiered run depths — make most cycles fast; keep one deep + weekly breadth.

Depths
------
light
    Pre-market / overnight. Prices for holdings + watchlist only. News on
    holdings. Exits allowed; new BUYs discarded. No universe scan.

holdings_watchlist
    The four intraday trading slots. Mini-scan of holdings + watchlist only
    (indicators, ATR, triggers). Deep research on those names + any watchlist
    trigger alerts. No full 180+ ticker download. New BUYs allowed (validator
    still enforces universe membership).

full
    Classic cycle: full universe scan + deep research on holdings, top setups,
    contrarian/deep-value lanes, and promoted fat-pitch names. Slowest.

weekly_market
    Broad market check-in (Sunday). Scans the curated universe (all sectors)
    plus discovery candidates for sector heat maps / leaders. Deep research
    only on holdings + a few standout discoveries. No trading — commentary
    and published market_checkin.json only.

evening_review
    Alias for news-only style full-context review without new trades
    (orchestrator still uses --news-only for the 5:30pm slot).

Config: autonomy_config.json -> schedule.slot_depths maps "HHMM" -> depth.
"""

from __future__ import annotations

from typing import Any

# Canonical depth ids (CLI, config, context["run_depth"]).
DEPTHS = (
    "light",
    "holdings_watchlist",
    "full",
    "weekly_market",
    "evening_review",
)

# Default weekday map — four fast holdings/watchlist runs, one full, one light,
# one evening review. KEEP IN SYNC with scripts/run_cycle.sh comments.
DEFAULT_SLOT_DEPTHS: dict[str, str] = {
    "0600": "light",
    "0900": "holdings_watchlist",
    "1000": "holdings_watchlist",
    "1200": "holdings_watchlist",
    "1400": "holdings_watchlist",
    "1600": "full",
    "1730": "evening_review",
}

# Slots that force a FULL deep dive when a universe name reports earnings.
# morning (9am ET) catches overnight + pre-market (BMO) prints; evening
# (5:30pm ET) catches that afternoon's after-hours (AMC) prints — 5:30 rather
# than 4:00 because after-hours releases are often not out by the 4pm slot.
DEFAULT_EARNINGS_SLOTS: dict[str, str] = {
    "0900": "morning",
    "1730": "evening",
}

_DESCRIPTIONS = {
    "light": (
        "Light check: holdings + watchlist prices and news; exits allowed; "
        "no full scan; new BUYs discarded."
    ),
    "holdings_watchlist": (
        "Focused cycle: mini-scan + deep research on holdings and watchlist "
        "(plus trigger alerts). No full-universe download — much faster."
    ),
    "full": (
        "Full research cycle: entire trading universe scanned, multi-lane "
        "setups, deep research on focus + promoted fat pitches."
    ),
    "weekly_market": (
        "Weekly market check-in: all-sector universe scan + discovery breadth, "
        "sector leaders heat map; deep research only on holdings + standouts; "
        "no trading."
    ),
    "evening_review": (
        "Evening review: research and commentary refresh; no trading "
        "(orchestrator news-only path)."
    ),
}


def depth_description(depth: str) -> str:
    return _DESCRIPTIONS.get(depth, f"Unknown depth: {depth}")


def depth_allows_new_buys(depth: str) -> bool:
    """Whether the brain's BUY proposals should be kept this cycle."""
    return depth in ("holdings_watchlist", "full")


def slot_depth_from_hhmm(hhmm: str, cfg: dict | None = None,
                         tolerance_minutes: int = 75) -> str:
    """Map a clock time 'HHMM' (ET) to a depth using config or defaults.

    Scheduled runs never land exactly on the slot (cloud routines fire a few
    minutes past the hour; the gather workflow runs 20 min before each slot),
    so this matches the NEAREST configured slot within tolerance_minutes.
    Outside every slot's window it falls back to "full" (a manual off-schedule
    run gets the complete cycle rather than a silently thinned one).
    """
    slots = ((cfg or {}).get("schedule") or {}).get("slot_depths") or DEFAULT_SLOT_DEPTHS

    def _mins(s: str) -> int:
        return int(s[:2]) * 60 + int(s[2:])

    try:
        now_m = _mins(str(hhmm).strip().zfill(4))
    except (TypeError, ValueError):
        return "full"
    best_depth, best_dist = None, None
    for key, depth in slots.items():
        # Skip annotation keys like "_note" and anything malformed.
        if not (isinstance(key, str) and len(key) == 4 and key.isdigit()):
            continue
        if depth not in DEPTHS:
            continue
        dist = abs(now_m - _mins(key))
        dist = min(dist, 1440 - dist)  # wrap around midnight
        if best_dist is None or dist < best_dist:
            best_depth, best_dist = depth, dist
    if best_depth is not None and best_dist <= tolerance_minutes:
        return str(best_depth)
    return "full"


def earnings_deep_dive_session(hhmm: str | None, cfg: dict | None = None,
                               tolerance_minutes: int = 75) -> str | None:
    """Which earnings session a clock time 'HHMM' (ET) maps to, or None.

    Feature toggle + slot map come from schedule.earnings_deep_dive; falls back
    to the default 9am(morning)/5:30pm(evening) slots. Returns "morning" or
    "evening" only when the feature is enabled AND hhmm is within tolerance of a
    session slot (scheduled runs land minutes off the slot). Pure — no network.
    """
    edd = ((cfg or {}).get("schedule") or {}).get("earnings_deep_dive")
    if edd is None:
        slots = DEFAULT_EARNINGS_SLOTS
    else:
        if not edd.get("enabled", True):
            return None
        slots = edd.get("slots") or DEFAULT_EARNINGS_SLOTS

    def _mins(s: str) -> int:
        return int(s[:2]) * 60 + int(s[2:])

    try:
        now_m = _mins(str(hhmm).strip().zfill(4))
    except (TypeError, ValueError, AttributeError):
        return None
    best, best_dist = None, None
    for key, sess in slots.items():
        if not (isinstance(key, str) and len(key) == 4 and key.isdigit()):
            continue
        if sess not in ("morning", "evening"):
            continue
        dist = abs(now_m - _mins(key))
        dist = min(dist, 1440 - dist)  # wrap around midnight
        if best_dist is None or dist < best_dist:
            best, best_dist = str(sess), dist
    if best is not None and best_dist <= tolerance_minutes:
        return best
    return None


def resolve_depth(
    *,
    explicit: str | None = None,
    light_flag: bool = False,
    news_only: bool = False,
    weekly_market: bool = False,
    hhmm: str | None = None,
    cfg: dict | None = None,
) -> str:
    """Resolve the effective run depth from CLI flags + optional clock slot.

    Precedence (highest first):
      explicit --depth > --weekly-market > --light > --news-only > slot map > full
    """
    if explicit:
        d = str(explicit).strip().lower().replace("-", "_")
        if d in DEPTHS:
            return d
        # Tolerate aliases
        aliases = {
            "hw": "holdings_watchlist",
            "holdings": "holdings_watchlist",
            "watchlist": "holdings_watchlist",
            "market": "weekly_market",
            "weekly": "weekly_market",
            "evening": "evening_review",
        }
        if d in aliases:
            return aliases[d]
        raise ValueError(f"unknown run depth '{explicit}'; allowed: {', '.join(DEPTHS)}")
    if weekly_market:
        return "weekly_market"
    if light_flag:
        return "light"
    if news_only:
        return "evening_review"
    if hhmm:
        return slot_depth_from_hhmm(hhmm, cfg)
    return "full"


def focus_ticker_budget(depth: str) -> dict[str, Any]:
    """How many names get expensive deep research per depth."""
    if depth == "light":
        return {
            "top_setups": 0,
            "contrarian": 0,
            "deep_value": 0,
            "supplier_pullbacks": 0,
            "promote_extra": 0,
            "discovery_deep": 0,
            "scan_mode": "none",
            "enrich_scan": False,
            "full_universe_scan": False,
            "filings_sweep": False,
            "deep_research": "holdings_only",
        }
    if depth == "holdings_watchlist":
        return {
            "top_setups": 0,
            "contrarian": 0,
            "deep_value": 0,
            "supplier_pullbacks": 0,
            "promote_extra": 0,
            "discovery_deep": 0,
            "scan_mode": "focus_only",  # mini-scan held+watchlist
            "enrich_scan": False,       # skip slow .info loop
            "full_universe_scan": False,
            # One EDGAR index call is cheap vs deep×N; catch material filings
            # mid-day even when we skip the full price scan.
            "filings_sweep": True,
            # Cap auto-promotions from tape headlines + 8-Ks into deep research.
            "tape_promote_max": 5,
            "deep_research": "holdings_watchlist_triggers",
        }
    if depth == "weekly_market":
        return {
            "top_setups": 10,
            "contrarian": 3,
            "deep_value": 2,
            "supplier_pullbacks": 2,
            "promote_extra": 2,
            "discovery_deep": 5,
            "scan_mode": "full",
            "enrich_scan": False,  # breadth over depth; no per-name info spam
            "full_universe_scan": True,
            "filings_sweep": True,
            "tape_promote_max": 5,
            "deep_research": "holdings_plus_standouts",
            "weekly": True,
        }
    # full (default)
    return {
        "top_setups": 5,
        "contrarian": 3,
        "deep_value": 2,
        "supplier_pullbacks": 0,  # lanes still in scan; deep only if promoted
        "promote_extra": 2,
        "discovery_deep": 0,
        "scan_mode": "full",
        "enrich_scan": True,
        "full_universe_scan": True,
        "filings_sweep": True,
        "tape_promote_max": 5,
        "deep_research": "focus_set",
        "weekly": False,
    }
