"""Compact market_events block for the context bundle.

Commodity/macro tape (oil, gold, VIX, DXY) via yfinance, plus geopolitical /
market-wide risk flags classified from tools.market_news headlines.

Intended orchestrator wiring (not auto-called yet — wire when ready):
    from tools.market_events import get_market_events
    market_events = get_market_events()

CLI: python -m tools.market_events

Never raises; returns {status, as_of, commodities, flags, flagged_headlines, note}.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

# Keyword buckets for headline classification (lowercase; whole-word-ish match).
# Multi-word phrases are checked as substrings; single tokens as word boundaries.
KEYWORD_BUCKETS: dict[str, list[str]] = {
    "geopolitical": [
        "war", "warfare", "invasion", "missile", "airstrike", "strike on",
        "iran", "ukraine", "russia", "israel", "gaza", "hamas", "hezbollah",
        "china", "taiwan", "beijing", "tariff", "tariffs", "sanction", "sanctions",
        "geopolitic", "nato", "pentagon", "military", "ceasefire", "hostilities",
    ],
    "inflation_rates": [
        "cpi", "pce", "inflation", "deflation", "fed", "fomc", "federal reserve",
        "rate cut", "rate hike", "rate hold", "interest rate", "yields", "yield ",
        "treasury", "bond selloff", "bond rally", "powell", "dot plot", "qt ",
        "quantitative", "hot cpi", "cool cpi", "core cpi", "sticky inflation",
    ],
    "financial_stress": [
        "bank failure", "bank run", "credit crunch", "credit stress", "liquidity",
        "contagion", "bailout", "svb", "regional bank", "banking crisis",
        "default risk", "credit default", "funding stress", "repo stress",
        "systemic risk", "leverage crisis", "margin call",
    ],
    "sector_macro": [
        "semiconductor ban", "chip ban", "export control", "export ban",
        "opec", "oil cut", "oil production", "ai capex", "data center",
        "hyperscaler", "nvidia ban", "china chip", "rare earth",
    ],
}

MAX_FLAGGED = 8

# Primary symbol, fallback, display key.
COMMODITY_SERIES = [
    ("CL=F", "USO", "oil"),
    ("GC=F", "GLD", "gold"),
    ("^VIX", "VIXY", "vix"),
    ("DX-Y.NYB", "UUP", "dxy"),
]


def _wordish_match(text: str, keyword: str) -> bool:
    """True if keyword appears in text. Multi-word / trailing-space phrases use
    substring; bare tokens use word-boundary so 'fed' does not match 'federal'
    unless listed separately. Pure."""
    kw = keyword.casefold().strip()
    if not kw:
        return False
    t = text.casefold()
    if " " in kw or kw.endswith(" "):
        return kw in t
    return re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", t) is not None


def classify_headline(title: str) -> list[str]:
    """Map a headline title to zero or more bucket tags. Pure / offline-testable.

    Returns sorted unique tags from KEYWORD_BUCKETS keys. Empty title -> [].
    """
    if not title or not isinstance(title, str):
        return []
    tags = []
    for bucket, keywords in KEYWORD_BUCKETS.items():
        if any(_wordish_match(title, kw) for kw in keywords):
            tags.append(bucket)
    return tags


def flag_headlines(headlines, max_flags: int = MAX_FLAGGED) -> list[dict]:
    """Classify headlines and keep those with at least one tag.

    Accepts list of dicts with at least ``title`` (and optional ``age_hours``).
    Returns [{title, tags, age_hours}] capped at max_flags, preserving input
    order (caller should pass newest-first). Pure.
    """
    if not isinstance(headlines, list):
        return []
    out = []
    for h in headlines:
        if not isinstance(h, dict):
            continue
        title = h.get("title")
        tags = classify_headline(title if isinstance(title, str) else "")
        if not tags:
            continue
        out.append({
            "title": str(title).strip(),
            "tags": tags,
            "age_hours": h.get("age_hours"),
        })
        if len(out) >= max_flags:
            break
    return out


def summarize_flags(flagged: list[dict]) -> dict:
    """Count of flagged headlines per bucket + active bucket names. Pure."""
    counts = {k: 0 for k in KEYWORD_BUCKETS}
    for item in flagged or []:
        for tag in item.get("tags") or []:
            if tag in counts:
                counts[tag] += 1
    active = [k for k, n in counts.items() if n > 0]
    return {"counts": counts, "active": active, "n_flagged": len(flagged or [])}


def _pct_change(closes) -> tuple:
    """From oldest->newest close list: (last, pct_1d, pct_5d). Missing -> None.
    1d uses prior bar; 5d uses the bar 5 sessions back (needs 6 closes).
    Pure helper over a plain sequence (no pandas required for tests)."""
    if not closes:
        return None, None, None
    vals = [float(c) for c in closes if c is not None]
    if not vals:
        return None, None, None
    last = vals[-1]
    pct_1d = None
    if len(vals) >= 2 and vals[-2]:
        pct_1d = round((last / vals[-2] - 1) * 100, 2)
    pct_5d = None
    if len(vals) >= 6 and vals[-6]:
        pct_5d = round((last / vals[-6] - 1) * 100, 2)
    return round(last, 4), pct_1d, pct_5d


def _fetch_series_closes(symbol: str, period: str = "10d"):
    """yfinance daily closes for one symbol. Fail-soft -> []."""
    try:
        import yfinance as yf
        df = yf.download(symbol, period=period, interval="1d", auto_adjust=True,
                         progress=False, threads=False)
        if df is None or getattr(df, "empty", True):
            return []
        close = df["Close"]
        # MultiIndex columns when single symbol sometimes still nest
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        return [float(x) for x in close.dropna().tolist()]
    except Exception:
        return []


def fetch_commodities() -> dict:
    """Pull oil / gold / VIX / DXY (primary then fallback). Fail-soft per series.

    Returns {key: {symbol, last, pct_1d, pct_5d} | None, ...}.
    """
    out: dict = {}
    for primary, fallback, key in COMMODITY_SERIES:
        row = None
        for sym in (primary, fallback):
            closes = _fetch_series_closes(sym)
            last, p1, p5 = _pct_change(closes)
            if last is not None:
                row = {
                    "symbol": sym,
                    "last": last,
                    "pct_1d": p1,
                    "pct_5d": p5,
                }
                break
        out[key] = row
    return out


def get_market_events(headlines=None, max_flags: int = MAX_FLAGGED,
                      skip_commodities: bool = False) -> dict:
    """Build the market_events context block. Never raises.

    Parameters
    ----------
    headlines : list[dict] | None
        If provided, used for flag classification (testability / reuse).
        Else pulled from tools.market_news.get_market_news().
    max_flags : int
        Cap on flagged_headlines (default 8).
    skip_commodities : bool
        If True, skip yfinance (tests / offline). commodities -> {}.

    Returns
    -------
    dict with keys:
      status, as_of, commodities, flags, flagged_headlines, note
      (+ optional error when fully degraded)
    """
    as_of = datetime.now(timezone.utc).isoformat()
    note = ("commodities via yfinance (CL=F/USO, GC=F/GLD, ^VIX/VIXY, DX-Y.NYB/UUP); "
            "flags from keyword classification of market_news headlines — "
            "heuristic tags, not a risk model")
    try:
        # --- headlines ---
        source_note = "caller"
        if headlines is None:
            try:
                from tools.market_news import get_market_news
                news = get_market_news()
                headlines = news.get("headlines") or []
                source_note = "market_news"
                if news.get("status") != "ok" and not headlines:
                    headlines = []
            except Exception:
                headlines = []
                source_note = "market_news_failed"

        flagged = flag_headlines(headlines, max_flags=max_flags)
        flags = summarize_flags(flagged)

        # --- commodities ---
        commodities: dict = {}
        commodity_ok = False
        if not skip_commodities:
            try:
                commodities = fetch_commodities()
                commodity_ok = any(v is not None for v in commodities.values())
            except Exception:
                commodities = {}
                commodity_ok = False

        any_signal = commodity_ok or bool(flagged) or bool(headlines)
        status = "ok" if any_signal or source_note == "caller" else "degraded"
        # If both legs fully failed and we tried network paths, still return a
        # usable empty shell rather than erroring the gather.
        if not commodity_ok and not headlines and source_note == "market_news_failed":
            status = "degraded"

        out = {
            "status": status,
            "as_of": as_of,
            "commodities": commodities,
            "flags": flags,
            "flagged_headlines": flagged,
            "note": note,
            "headline_source": source_note,
        }
        return out
    except Exception as e:
        return {
            "status": "error",
            "as_of": as_of,
            "commodities": {},
            "flags": {"counts": {k: 0 for k in KEYWORD_BUCKETS},
                      "active": [], "n_flagged": 0},
            "flagged_headlines": [],
            "note": note,
            "error": str(e)[:200],
        }


if __name__ == "__main__":
    try:
        from pathlib import Path
        from tools.envload import load_env
        load_env()
    except Exception:
        pass
    print(json.dumps(get_market_events(), indent=2, default=str))
