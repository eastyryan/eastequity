"""Alpaca Market Data (Basic plan) — batched snapshot/quote reads. Fail-soft.

One snapshots request covers ~100 symbols, so the FULL universe (~182 names)
is 2 requests — trivially inside the free tier's 200 req/min. Every helper
returns {} / None on any failure (missing keys, network, sandbox egress
block); callers fall back to yfinance or the last committed snapshot.

Free-tier caveats the callers must respect:
  * Real-time quotes/trades are IEX-ONLY (~2-3% of consolidated volume):
    great for a fresh mark, NOT a volume signal. Keep RVOL/OBV/etc. on
    daily bars.
  * After hours the IEX book empties (bid/ask 0) — price extraction prefers
    latestTrade, then the minute bar, then the daily bar close.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_BASE = "https://data.alpaca.markets"
_CHUNK = 100          # symbols per snapshots request (URL stays well under limits)
_TIMEOUT = 8          # seconds; the feeder tick must never hang


class FeedFailure(Exception):
    """A feed call that did not complete. NOT the same as 'the feed said nothing'.

    THE INCIDENT (audited 2026-07-19): get_news / get_movers / get_most_actives
    all return []/{} on network error, non-200 AND on a genuinely quiet market.
    Their callers wrapped them in try/except to detect outages — but since these
    functions never raise, that detection was dead code. Proven under a simulated
    total outage, tools/market_radar.py reported:

        status  : degraded_all_feeds_empty
        coverage: {"requested": 3, "succeeded": 3, "failed": 0,
                   "sources": {"movers":"ok","actives":"ok","news":"ok"}}

    Three dead feeds counted as three successes, and the `if ok_sources == 0`
    guard could never fire. The same pattern made market_news record a dead
    Alpaca feed as {"ok": True, "items": 0}.

    The *_result() helpers below return (data, error) so a caller can tell "the
    market was quiet" from "we never got an answer". The bare helpers are kept as
    back-compat wrappers and still fail soft — callers that must distinguish the
    two cases use the _result variants.
    """


def _keys() -> tuple[str, str] | None:
    """API keys from the environment (.env is loaded by runlib.core/orchestrator;
    also loaded here fail-soft so standalone `python -m tools.live_prices` works)."""
    k, s = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if not (k and s):
        try:
            from tools.envload import load_env
            load_env()
            k = os.environ.get("ALPACA_API_KEY", "")
            s = os.environ.get("ALPACA_SECRET_KEY", "")
        except Exception:
            pass
    return (k, s) if k and s else None


def has_keys() -> bool:
    return _keys() is not None


def _headers() -> dict | None:
    ks = _keys()
    if not ks:
        return None
    return {"APCA-API-KEY-ID": ks[0], "APCA-API-SECRET-KEY": ks[1]}


def _iso_to_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:  # Alpaca stamps RFC3339 with nanoseconds — trim to microseconds
        ts = str(ts).replace("Z", "+00:00")
        if "." in ts:
            # Alpaca stamps nanosecond fractions; fromisoformat (py3.9) takes ≤6 digits.
            head, tail = ts.split(".", 1)
            tz_idx = min([i for i, c in enumerate(tail) if c in "+-"] or [len(tail)])
            frac6 = "".join(c for c in tail[:tz_idx] if c.isdigit())[:6] or "0"
            ts = f"{head}.{frac6}{tail[tz_idx:]}"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def get_snapshots(tickers) -> dict:
    """{TICKER: snapshot} for every symbol Alpaca returned. {} on any failure.

    Snapshot values are Alpaca's raw dicts: latestTrade {p, t}, latestQuote
    {ap, bp, as, bs, t}, minuteBar/dailyBar/prevDailyBar {o,h,l,c,v,t}.
    """
    hdrs = _headers()
    syms = sorted({str(t).upper() for t in tickers if t})
    if not hdrs or not syms:
        return {}
    out: dict = {}
    for i in range(0, len(syms), _CHUNK):
        chunk = syms[i:i + _CHUNK]
        try:
            r = requests.get(f"{DATA_BASE}/v2/stocks/snapshots",
                             params={"symbols": ",".join(chunk)},
                             headers=hdrs, timeout=_TIMEOUT)
            if r.status_code != 200:
                continue
            body = r.json()
            if isinstance(body, dict):
                # v2 returns {SYM: snapshot}; tolerate a nested {"snapshots": {...}}
                snaps = body.get("snapshots") if "snapshots" in body else body
                for sym, snap in (snaps or {}).items():
                    if isinstance(snap, dict):
                        out[str(sym).upper()] = snap
        except Exception:
            continue
    return out


def snapshot_price(snap: dict) -> tuple[float | None, str | None, str]:
    """(price, asof_iso, which) from one snapshot: latestTrade → minuteBar →
    dailyBar → prevDailyBar. Pure; never raises."""
    try:
        lt = snap.get("latestTrade") or {}
        if lt.get("p"):
            return float(lt["p"]), lt.get("t"), "latest_trade"
        mb = snap.get("minuteBar") or {}
        if mb.get("c"):
            return float(mb["c"]), mb.get("t"), "minute_bar"
        for key, tag in (("dailyBar", "daily_bar"), ("prevDailyBar", "prev_daily_bar")):
            b = snap.get(key) or {}
            if b.get("c"):
                return float(b["c"]), b.get("t"), tag
    except (TypeError, ValueError):
        pass
    return None, None, "none"


def get_prices(tickers) -> dict:
    """{TICKER: {"price": float, "asof": iso|None, "via": str}} — batched,
    best-effort, {} when keys/network are unavailable."""
    out: dict = {}
    for sym, snap in get_snapshots(tickers).items():
        px, asof, via = snapshot_price(snap)
        if px and px > 0:
            out[sym] = {"price": round(px, 4), "asof": asof, "via": via}
    return out


def _age_hours(published_iso: str | None, now: datetime | None = None) -> float | None:
    dt = _iso_to_dt(published_iso)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return round((now - dt).total_seconds() / 3600.0, 1)


def get_news(symbols=None, hours: float = 24.0, limit: int = 50) -> list[dict]:
    """Back-compat wrapper: rows only, [] on failure OR on a quiet feed.

    Use get_news_result() when you need to tell those two apart — see FeedFailure.
    """
    return get_news_result(symbols, hours, limit)[0]


def get_news_result(symbols=None, hours: float = 24.0,
                    limit: int = 50) -> tuple[list[dict], str | None]:
    """Alpaca News API (Benzinga-sourced, included in the free Basic plan).

    symbols=None -> market-wide feed; a list scopes to those tickers. Rows are
    normalized to the repo headline shape {title, source, published, url,
    age_hours, symbols[]} and filtered to the last `hours`.

    Returns (rows, error). error is None when the call COMPLETED — including when
    it completed and the market was quiet, which is why an empty list alone can
    never mean failure. A non-None error means we did not get an answer.
    """
    hdrs = _headers()
    if not hdrs:
        return [], "no_api_keys"
    params: dict = {"limit": max(1, min(int(limit), 50)), "sort": "desc",
                    "include_content": "false"}
    if symbols:
        syms = sorted({str(s).upper() for s in symbols if s})
        if not syms:
            return [], None          # nothing asked for is not a failure
        params["symbols"] = ",".join(syms[:_CHUNK])
    try:
        r = requests.get(f"{DATA_BASE}/v1beta1/news", params=params,
                         headers=hdrs, timeout=_TIMEOUT)
        if r.status_code != 200:
            return [], f"http_{r.status_code}"
        rows = (r.json() or {}).get("news") or []
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("headline"):
            continue
        published = row.get("created_at") or row.get("updated_at")
        age = _age_hours(published, now)
        if age is not None and age > hours:
            continue
        out.append({"title": str(row["headline"]).strip(),
                    "source": f"alpaca_{str(row.get('source') or 'news').lower()}",
                    "published": published, "url": row.get("url"),
                    "age_hours": age,
                    "symbols": [str(s).upper() for s in (row.get("symbols") or [])]})
    return out, None


def get_movers(top: int = 10) -> dict:
    """Back-compat wrapper: {} on failure OR on an empty screener.
    Use get_movers_result() to tell those apart — see FeedFailure."""
    return get_movers_result(top)[0]


def get_movers_result(top: int = 10) -> tuple[dict, str | None]:
    """Market-wide top gainers/losers by percent change (Alpaca screener,
    free plan). {"gainers": [...], "losers": [...]} with rows
    {symbol, percent_change, change, price}.

    Returns (data, error); error is None when the call completed."""
    hdrs = _headers()
    if not hdrs:
        return {}, "no_api_keys"
    try:
        r = requests.get(f"{DATA_BASE}/v1beta1/screener/stocks/movers",
                         params={"top": max(1, min(int(top), 50))},
                         headers=hdrs, timeout=_TIMEOUT)
        if r.status_code != 200:
            return {}, f"http_{r.status_code}"
        body = r.json() or {}
    except Exception as e:
        return {}, f"{type(e).__name__}: {str(e)[:120]}"
    out = {}
    for side in ("gainers", "losers"):
        rows = []
        for row in body.get(side) or []:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            rows.append({"symbol": str(row["symbol"]).upper(),
                         "percent_change": row.get("percent_change"),
                         "change": row.get("change"), "price": row.get("price")})
        out[side] = rows
    if body.get("last_updated"):
        out["last_updated"] = body["last_updated"]
    if not (out.get("gainers") or out.get("losers")):
        return {}, None          # screener answered with nothing — not a failure
    return out, None


def get_most_actives(top: int = 20, by: str = "volume") -> list[dict]:
    """Back-compat wrapper: [] on failure OR on an empty screener.
    Use get_most_actives_result() to tell those apart — see FeedFailure."""
    return get_most_actives_result(top, by)[0]


def get_most_actives_result(top: int = 20,
                            by: str = "volume") -> tuple[list[dict], str | None]:
    """Market-wide most-active stocks (Alpaca screener, free plan).
    by: "volume" | "trades". Returns ([{symbol, volume, trade_count}], error);
    error is None when the call completed."""
    hdrs = _headers()
    if not hdrs:
        return [], "no_api_keys"
    try:
        r = requests.get(f"{DATA_BASE}/v1beta1/screener/stocks/most-actives",
                         params={"top": max(1, min(int(top), 100)),
                                 "by": by if by in ("volume", "trades") else "volume"},
                         headers=hdrs, timeout=_TIMEOUT)
        if r.status_code != 200:
            return [], f"http_{r.status_code}"
        rows = (r.json() or {}).get("most_actives") or []
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        out.append({"symbol": str(row["symbol"]).upper(),
                    "volume": row.get("volume"), "trade_count": row.get("trade_count")})
    return out, None


def freshest_trade_age_minutes(prices: dict, now: datetime | None = None) -> float | None:
    """Age of the NEWEST latest-trade timestamp across a get_prices() result —
    used to label a snapshot taken while the market is closed. Pure."""
    now = now or datetime.now(timezone.utc)
    ages = []
    for rec in (prices or {}).values():
        dt = _iso_to_dt(rec.get("asof"))
        if dt is not None:
            ages.append((now - dt).total_seconds() / 60.0)
    return round(min(ages), 1) if ages else None


if __name__ == "__main__":
    px = get_prices(["SPY", "DELL", "MU"])
    print(json.dumps(px, indent=2))
    print("freshest trade age (min):", freshest_trade_age_minutes(px))
