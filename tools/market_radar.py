"""Market radar — a cheap, TRUE market-wide "what is moving today" sweep.

The universe scanner only sees data/universe.json and the discovery screen only
sees a static candidate pool. This radar has no such cage: it reads Alpaca's
market-wide screeners (top percent movers + most-active by volume, free Basic
plan) and the market-wide Alpaca news feed, tags every symbol with whether it
is already tradeable (in_universe), and surfaces the off-universe names so the
brain can propose them as universe_candidates when a real swing setup is there.

This is a RADAR, not a signal: rows carry only what the screener returned
(price/percent move/volume/news mention counts). Anything the brain wants to
act on must clear the deterministic universe-candidate gates ($1B cap floor,
priceability, format) before it becomes tradeable, and the normal fat-pitch
bar + validator after that.

Fail-soft everywhere: no Alpaca keys / blocked egress -> {"status": "skipped"}.

CLI: python -m tools.market_radar
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

# ETFs/megacap furniture that dominate most-actives every single day and carry
# no discovery information; keep the radar pointed at actual movers.
_ACTIVES_NOISE = {"SPY", "QQQ", "IWM", "DIA", "TQQQ", "SQQQ", "SOXL", "SOXS",
                  "UVXY", "VXX", "TLT", "GLD", "SLV", "HYG", "EEM", "XLF", "FXI"}

# The raw movers screener is dominated by sub-$1 warrants and penny stocks —
# the exact delisting/manipulation zone the system's $1B cap floor exists to
# exclude. A price floor removes most of it cheaply (market cap needs a second
# lookup and is enforced later at universe-candidate time anyway).
MIN_MOVER_PRICE_USD = 5.0


def _clean_symbol(sym) -> str | None:
    """Uppercase symbol matching the repo's plain-ticker format, else None. Pure."""
    s = str(sym or "").upper().strip()
    return s if _TICKER_RE.match(s) else None


def _news_mention_counts(news_rows: list) -> dict:
    """{SYMBOL: mention_count} across market-wide news rows (each row carries a
    symbols[] tag). Pure."""
    counts: dict = {}
    for row in news_rows or []:
        for sym in row.get("symbols") or []:
            s = _clean_symbol(sym)
            if s:
                counts[s] = counts.get(s, 0) + 1
    return counts


def build_radar(movers: dict, actives: list, news_rows: list, universe: set,
                forbidden: set | None = None, max_rows: int = 12) -> dict:
    """Assemble the radar block from raw feed payloads. Pure — unit-testable
    offline. Every row is tagged in_universe; off-universe rows are the point."""
    forbidden = forbidden or set()
    news_counts = _news_mention_counts(news_rows)

    def _row(sym, **extra):
        return {"ticker": sym, "in_universe": sym in universe,
                "news_mentions_24h": news_counts.get(sym, 0), **extra}

    gainers, losers = [], []
    for side, dest in (("gainers", gainers), ("losers", losers)):
        for r in (movers or {}).get(side) or []:
            sym = _clean_symbol(r.get("symbol"))
            price = r.get("price")
            if not sym or sym in forbidden:
                continue
            if not isinstance(price, (int, float)) or price < MIN_MOVER_PRICE_USD:
                continue
            dest.append(_row(sym, pct_change=r.get("percent_change"), price=price))

    most_active = []
    for r in actives or []:
        sym = _clean_symbol(r.get("symbol"))
        if not sym or sym in forbidden or sym in _ACTIVES_NOISE:
            continue
        most_active.append(_row(sym, volume=r.get("volume")))

    # News-trending: symbols with repeated market-wide mentions that are NOT
    # already surfaced by the movers/actives lists.
    surfaced = {r["ticker"] for r in gainers + losers + most_active}
    trending = [_row(sym) for sym, n in
                sorted(news_counts.items(), key=lambda kv: -kv[1])
                if n >= 2 and sym not in surfaced and sym not in forbidden
                and sym not in _ACTIVES_NOISE]

    off_universe = sorted({r["ticker"] for r in
                           gainers + losers + most_active + trending
                           if not r["in_universe"]})
    return {
        "gainers": gainers[:max_rows],
        "losers": losers[:max_rows],
        "most_active": most_active[:max_rows],
        "news_trending": trending[:max_rows],
        "off_universe_symbols": off_universe,
    }


def get_market_radar(top_movers: int = 10, top_actives: int = 20,
                     news_hours: float = 24.0) -> dict:
    """Fetch movers + most-actives + market-wide news from Alpaca and build the
    radar block. Fail-soft: missing keys or dead feeds -> status skipped/degraded,
    never a raise into a trading run."""
    now = datetime.now(timezone.utc)
    out = {
        "status": "ok", "as_of": now.isoformat(),
        "note": ("Market-WIDE radar (Alpaca screeners + news), not limited to the "
                 "universe. off_universe_symbols are candidates for the "
                 "universe_candidates output field IF a real swing setup exists — "
                 "they are not tradeable until deterministically added ($1B cap, "
                 "priceable, liquid) and are never a buy signal by themselves."),
    }
    try:
        from tools import alpaca_data
        if not alpaca_data.has_keys():
            return {**out, "status": "skipped", "reason": "no_alpaca_keys"}
    except Exception as e:
        return {**out, "status": "error", "error": str(e)[:200]}

    # THREE INDEPENDENT SUB-FEEDS, fetched independently. They used to share one
    # try-block, which had two failure modes the caller could not see: a raise in
    # get_movers() killed actives + news that would have succeeded, and a partial
    # outage (movers dead, actives fine) was indistinguishable from a genuinely
    # narrow tape because only the ALL-empty case was ever labeled. The radar is
    # the system's only look OUTSIDE the universe cage, so an undisclosed
    # half-outage silently narrows the opportunity set back to the cage.
    sources: dict = {}
    movers: dict = {}
    actives: list = []
    news_rows: list = []
    # The *_result helpers return (data, error). The try/except around the bare
    # helpers used to be dead code: they return []/{} on network error and non-200
    # rather than raising, so `except` never fired and a total outage reported
    # succeeded=3, failed=0 with status "degraded_all_feeds_empty" — a claim about
    # a quiet market rather than a fact about a dead fetch. The try/except is kept
    # for genuinely unexpected faults; the error channel is what now does the work.
    try:
        # top is generous because the price floor strips the warrant/penny rows.
        movers, err = alpaca_data.get_movers_result(top=max(top_movers * 3, 30))
        sources["movers"] = "ok" if err is None else f"error: {err[:100]}"
    except Exception as e:
        sources["movers"] = f"error: {str(e)[:100]}"
    try:
        # by="trades": share-volume actives are dominated by penny names; trade
        # count surfaces the liquid large caps actually being fought over today.
        actives, err = alpaca_data.get_most_actives_result(top=top_actives, by="trades")
        sources["actives"] = "ok" if err is None else f"error: {err[:100]}"
    except Exception as e:
        sources["actives"] = f"error: {str(e)[:100]}"
    try:
        news_rows, err = alpaca_data.get_news_result(hours=news_hours, limit=50)
        sources["news"] = "ok" if err is None else f"error: {err[:100]}"
    except Exception as e:
        sources["news"] = f"error: {str(e)[:100]}"

    ok_sources = sum(1 for v in sources.values() if v == "ok")
    out["coverage"] = {"requested": len(sources), "succeeded": ok_sources,
                       "failed": len(sources) - ok_sources, "sources": sources,
                       "unit": "alpaca_screener_feed"}

    universe, forbidden = set(), set()
    try:
        import validator
        universe = validator.load_universe()
        forbidden = set(validator.load_config()["hard_rules"]
                        .get("forbidden_ticker_patterns", []))
    except Exception:
        out["status"] = "degraded_universe_unreadable"

    if ok_sources == 0:
        # Every screener call raised: this is a dead feed, and "no movers today"
        # would be a lie about the market rather than a fact about the fetch.
        return {**out, "status": "error",
                "error": "all Alpaca radar sub-feeds failed: " + "; ".join(
                    f"{k}={v}" for k, v in sources.items())}
    if not (movers or actives or news_rows):
        return {**out, "status": "degraded_all_feeds_empty"}

    out.update(build_radar(movers, actives, news_rows, universe, forbidden))
    # A partial outage stays USABLE but must say so: the missing sub-feed's
    # symbols are unscanned, not unattractive.
    if ok_sources < len(sources) and out.get("status") == "ok":
        out["status"] = "degraded"
    return out


if __name__ == "__main__":
    try:
        from pathlib import Path

        from tools.envload import load_env
        load_env()
    except Exception:
        pass
    print(json.dumps(get_market_radar(), indent=2, default=str))
