"""Price-source seam — one place to swap/upgrade the last-close provider.

get_closes() prefers Polygon (exchange-grade, keyed via POLYGON_API_KEY) and
falls back to yfinance per missing name; with no key it is pure yfinance,
matching the old behavior exactly. Adopted first by the orchestrator's
_light_prices; the batched universe scan can migrate later without touching
its callers.
"""

from __future__ import annotations

import os


def _polygon_last_closes(tickers: list, api_key: str) -> dict:
    """Previous-session close per ticker from Polygon's /prev aggregate.
    Per-name fail-soft; throttle responses retry with bounded backoff."""
    from tools.net import get_with_throttle_retry
    out: dict = {}
    for t in tickers:
        try:
            r = get_with_throttle_retry(
                f"https://api.polygon.io/v2/aggs/ticker/{t}/prev",
                params={"adjusted": "true", "apiKey": api_key},
                timeout=(5, 10), retries=1)
            results = r.json().get("results") or []
            close = results[0].get("c") if results else None
            if isinstance(close, (int, float)) and close > 0:
                out[t] = round(float(close), 2)
        except Exception:
            continue
    return out


def _yf_last_closes(tickers: list) -> dict:
    """Batched yfinance last closes (the keyless default)."""
    try:
        import yfinance as yf
        data = yf.download(tickers, period="5d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
        out: dict = {}
        for t in tickers:
            try:
                df = data[t].dropna() if len(tickers) > 1 else data.dropna()
                out[t] = round(float(df["Close"].iloc[-1]), 2)
            except Exception:
                continue
        return out
    except Exception:
        return {}


def get_closes(tickers: list) -> dict:
    """Last close per ticker: Polygon when POLYGON_API_KEY is set (yfinance
    filling any coverage gaps), else pure yfinance."""
    tickers = list(dict.fromkeys(str(t).upper() for t in tickers if t))
    if not tickers:
        return {}
    out: dict = {}
    api_key = os.environ.get("POLYGON_API_KEY")
    if api_key:
        out = _polygon_last_closes(tickers, api_key)
    missing = [t for t in tickers if t not in out]
    if missing:
        out.update(_yf_last_closes(missing))
    return out
