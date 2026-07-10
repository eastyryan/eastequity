"""Watchlist Trigger Checker — deterministic watch on "would_buy_at" levels.

The brain writes watchlist entries with free-text entry triggers like
"near $500", "below $170", or "a pullback toward $175". Nothing was watching
those between runs. This tool parses dollar levels out of that text and, when
a ticker's current price is at or within 2% above the parsed level, emits an
alert telling the brain to prioritize deep research on that name this run.

Non-price triggers (earnings dates, "after a correction") are not parseable
and are skipped silently — this only automates what can be automated.
Never raises; returns [] for empty/missing input.

CLI: python -m tools.watchlist_triggers   (self-checks the parser)
"""

from __future__ import annotations

import re

# Dollar figures like $500, $1,800, $52.50 — commas and decimals allowed.
_DOLLAR_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")

# Price is "triggered" at or below 102% of the parsed level: close enough
# that the pullback the brain asked for is effectively here.
TRIGGER_TOLERANCE = 1.02


def parse_price_level(text) -> float | None:
    """Extract the buy level from free-text would_buy_at; None if unparseable.

    Multiple figures (e.g. "$500-$520 zone", "near $175 or above $185") take
    the lowest — that is the level the brain said it wants to buy toward.
    """
    if not isinstance(text, str):
        return None
    levels = []
    for whole, frac in _DOLLAR_RE.findall(text):
        try:
            levels.append(float(whole.replace(",", "") + (frac or "")))
        except ValueError:
            continue
    return min(levels) if levels else None


def check_watchlist_triggers(watchlist: list[dict], prices: dict) -> list[dict]:
    """Return an alert per watchlist name whose price trigger has been reached."""
    alerts: list[dict] = []
    try:
        if not watchlist or not prices:
            return alerts
        for entry in watchlist:
            try:
                if not isinstance(entry, dict):
                    continue
                ticker = str(entry.get("ticker") or "").upper()
                trigger_text = entry.get("would_buy_at")
                level = parse_price_level(trigger_text)
                if not ticker or level is None or level <= 0:
                    continue
                last = prices.get(ticker)
                if last is None:
                    continue
                last = float(last)
                if last <= level * TRIGGER_TOLERANCE:
                    alerts.append({
                        "ticker": ticker,
                        "trigger_text": trigger_text,
                        "parsed_level": level,
                        "last_price": round(last, 2),
                        "note": "price trigger reached - prioritize deep "
                                "research on this name this run",
                    })
            except Exception:
                continue  # one bad entry never kills the run
    except Exception:
        return []
    return alerts


if __name__ == "__main__":
    import json
    cases = [("AMD", "near $500", 505.0), ("ANET", "below $170", 200.0),
             ("GEV", "on a pullback toward the 50-day average, or after 8/4 earnings", 100.0),
             ("SNDK", "$500-$520 zone", 508.0)]
    wl = [{"ticker": t, "would_buy_at": w} for t, w, _ in cases]
    px = {t: p for t, _, p in cases}
    print(json.dumps(check_watchlist_triggers(wl, px), indent=2))
