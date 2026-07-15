"""Promote universe names mentioned on the tape (or filing 8-Ks) into the focus set.

Balance knobs for focused runs:
  * market_news / market_events headlines that mention a universe ticker
  * universe-wide 8-K filers from the daily EDGAR sweep

Pure helpers so tests need no network. Cap extras so a busy news day cannot
inflate deep research to the whole universe.
"""

from __future__ import annotations

import re
from typing import Iterable

# Cashtag $NVDA or bare ticker as a whole word. Avoid matching inside longer
# words ("META" in "METAPHOR") via word boundaries; cashtags are unambiguous.
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
# Standalone 1-5 letter tokens that look like tickers (all caps preferred but
# we uppercase everything we extract).
_BARE_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")

# Common English / finance words that are also valid ticker shapes — never
# promote these from bare-word matches (cashtags still win).
_STOP = frozenset({
    "A", "I", "AM", "AN", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS", "IT",
    "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HAD", "HER",
    "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM", "HIS", "HOW", "ITS",
    "MAY", "NEW", "NOW", "OLD", "SEE", "WAY", "WHO", "BOY", "DID", "LET", "PUT",
    "SAY", "SHE", "TOO", "USE", "CEO", "CFO", "CTO", "IPO", "ETF", "GDP", "CPI",
    "PPI", "FED", "SEC", "FDA", "FTC", "DOE", "AI", "API", "GDP", "YOY", "QOQ",
    "TTM", "EPS", "PE", "EV", "USA", "UK", "EU", "NYSE", "NASDAQ", "DOW", "SPX",
    "SPY", "QQQ", "IWM", "VIX", "USD", "CEO", "BUY", "SELL", "HOLD", "LONG",
    "SHORT", "CALL", "PUTS", "BULL", "BEAR", "RISK", "RATE", "RATES", "OIL",
    "GOLD", "TECH", "CHIP", "CHIPS", "BANK", "BANKS", "FUND", "FUNDS", "STOCK",
    "STOCKS", "SHARE", "SHARES", "MARKET", "MARKETS", "TRADE", "TRADES", "NEWS",
    "TODAY", "WEEK", "YEAR", "HIGH", "LOW", "OPEN", "CLOSE", "GAIN", "LOSS",
    "FROM", "WITH", "THAT", "THIS", "WILL", "HAVE", "BEEN", "WERE", "WHEN",
    "WHAT", "THAN", "THEN", "THEM", "THEY", "ALSO", "JUST", "INTO", "OVER",
    "AFTER", "BEFORE", "ABOUT", "COULD", "WOULD", "SHOULD", "UNDER", "FIRST",
})


def extract_cashtags(text: str) -> set[str]:
    """$NVDA-style mentions only. Pure."""
    if not text:
        return set()
    return {m.group(1).upper() for m in _CASHTAG_RE.finditer(str(text))}


def extract_candidate_tickers(text: str) -> set[str]:
    """Cashtags + bare ALL-CAPS tokens, minus stopwords. Pure."""
    if not text:
        return set()
    s = str(text)
    out = extract_cashtags(s)
    for m in _BARE_TICKER_RE.finditer(s):
        tok = m.group(1).upper()
        if tok in _STOP or len(tok) < 1:
            continue
        out.add(tok)
    return out


def universe_mentions_in_headlines(
    headlines: Iterable[dict] | None,
    universe: set[str] | list[str] | None,
    *,
    max_names: int = 6,
) -> list[dict]:
    """Return [{ticker, titles, source_kind}] for universe names hit by headlines.

    Ranked by hit count (more headlines = higher priority), then ticker.
    Pure; headlines are dicts with optional 'title' (and ignored extras).
    """
    uni = {str(t).upper() for t in (universe or []) if t}
    if not uni or not headlines:
        return []
    hits: dict[str, list[str]] = {}
    for h in headlines:
        if not isinstance(h, dict):
            continue
        title = h.get("title") or ""
        if not title:
            continue
        found = extract_candidate_tickers(title) & uni
        # Prefer cashtag hits when both exist — already in found if cashtag used.
        for t in found:
            hits.setdefault(t, []).append(str(title)[:160])
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out = []
    for t, titles in ranked[: max(0, int(max_names))]:
        out.append({
            "ticker": t,
            "titles": titles[:3],
            "n_headlines": len(titles),
            "source_kind": "tape_headline",
        })
    return out


def universe_8k_promotions(
    todays_8ks: dict | None,
    universe: set[str] | list[str] | None,
    *,
    max_names: int = 6,
) -> list[dict]:
    """Universe tickers that filed an 8-K on the swept index day. Pure."""
    uni = {str(t).upper() for t in (universe or []) if t}
    if not uni or not isinstance(todays_8ks, dict):
        return []
    filers = todays_8ks.get("filers") or todays_8ks.get("tickers") or []
    # Support both shapes: list of dicts or list of ticker strings
    seen: list[str] = []
    detail: dict[str, list[str]] = {}
    if isinstance(filers, list):
        for row in filers:
            if isinstance(row, dict):
                t = str(row.get("ticker") or "").upper()
                form = str(row.get("form") or "8-K")
            else:
                t = str(row).upper()
                form = "8-K"
            if t and t in uni and t not in detail:
                detail[t] = [form]
                seen.append(t)
            elif t in detail:
                detail[t].append(form)
    # Also accept by_ticker map
    by_t = todays_8ks.get("by_ticker") or {}
    if isinstance(by_t, dict):
        for t, info in by_t.items():
            t = str(t).upper()
            if t in uni and t not in detail:
                detail[t] = [str((info or {}).get("form") or "8-K")]
                seen.append(t)
    out = []
    for t in seen[: max(0, int(max_names))]:
        out.append({
            "ticker": t,
            "titles": detail.get(t) or ["8-K"],
            "n_headlines": len(detail.get(t) or []),
            "source_kind": "universe_8k",
        })
    return out


def promote_focus_from_tape(
    already: list[str] | set[str],
    *,
    headlines: list[dict] | None = None,
    todays_8ks: dict | None = None,
    universe: set[str] | list[str] | None = None,
    max_tape: int = 4,
    max_8k: int = 4,
    max_total_extra: int = 5,
) -> tuple[list[str], list[dict]]:
    """Pick up to max_total_extra new focus tickers from tape + 8-Ks.

    Returns (tickers_to_add, promotion_records_for_bundle).
    Preference: 8-K filers first (material corporate event), then tape mentions.
    Pure.
    """
    have = {str(t).upper() for t in (already or []) if t}
    promotions: list[dict] = []
    for rec in universe_8k_promotions(todays_8ks, universe, max_names=max_8k):
        if rec["ticker"] not in have:
            promotions.append(rec)
    for rec in universe_mentions_in_headlines(headlines, universe, max_names=max_tape):
        if rec["ticker"] not in have and all(p["ticker"] != rec["ticker"] for p in promotions):
            promotions.append(rec)
    promotions = promotions[: max(0, int(max_total_extra))]
    add = [p["ticker"] for p in promotions]
    return add, promotions
