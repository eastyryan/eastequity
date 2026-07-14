"""Watchlist Trigger Checker — deterministic watch on "would_buy_at" levels.

The brain writes watchlist entries with free-text entry triggers like
"near $500", "below $170", or "a pullback toward $175". Nothing was watching
those between runs. This tool parses dollar levels out of that text and, when
a ticker's current price is within a tight band (+-2%) of the parsed level,
emits an alert telling the brain to prioritize deep research on that name.

The band is SYMMETRIC on purpose. The old rule fired for any price at or below
102% of the level, so a name trading well under its buy level (e.g. AMD ~3.9%
below a $555 trigger on 2026-07-14) produced a "trigger reached" alert that the
note itself called "within 2%" - a false positive that wastes a research pass.
A price far below the buy zone is usually a breakdown to reassess from the scan,
not a limit fill; the brain still sees it in the universe scan either way.

Non-price triggers (earnings dates, "after a correction") are not parseable
and are skipped silently — this only automates what can be automated.
Never raises; returns [] for empty/missing input.

CLI: python -m tools.watchlist_triggers   (self-checks the parser)
"""

from __future__ import annotations

import re

# Dollar figures like $500, $1,800, $52.50 — commas and decimals allowed.
_DOLLAR_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")

# Price is "triggered" when it is within +-TRIGGER_TOLERANCE_PCT of the parsed
# level (a symmetric proximity band): the pullback the brain asked for is
# effectively here, in either direction, but not arbitrarily far past it.
TRIGGER_TOLERANCE_PCT = 0.02


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
                dist_pct = (last / level - 1) * 100
                if abs(dist_pct) <= TRIGGER_TOLERANCE_PCT * 100:
                    alerts.append({
                        "ticker": ticker,
                        "trigger_text": trigger_text,
                        "parsed_level": level,
                        "last_price": round(last, 2),
                        "distance_from_level_pct": round(dist_pct, 2),
                        "note": "price within 2% of your stated buy level - "
                                "prioritize deep research on this name this run",
                    })
            except Exception:
                continue  # one bad entry never kills the run
    except Exception:
        return []
    return alerts


if __name__ == "__main__":
    import json
    cases = [("AMD", "near $500", 505.0),          # 1.0% above -> FIRES
             ("AMDX", "near $555", 533.5),          # 3.9% below -> does NOT fire (the 7/14 false positive)
             ("ANET", "below $170", 200.0),         # 17.6% above -> does NOT fire
             ("GEV", "on a pullback toward the 50-day average, or after 8/4 earnings", 100.0),
             ("SNDK", "$500-$520 zone", 508.0)]     # 1.6% above parsed $500 -> FIRES
    wl = [{"ticker": t, "would_buy_at": w} for t, w, _ in cases]
    px = {t: p for t, _, p in cases}
    print(json.dumps(check_watchlist_triggers(wl, px), indent=2))
