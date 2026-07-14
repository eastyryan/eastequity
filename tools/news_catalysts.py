"""News & Catalyst Fetcher — swing-relevant headlines + upcoming earnings dates.

Sources (all free/keyless via yfinance):
  * per-ticker news headlines, filtered to the last NEWS_MAX_AGE_DAYS and
    stamped with age_days so the brain sees freshness instead of guessing
  * next earnings date (the single most important swing catalyst clock)
  * analyst consensus snapshot (recommendation mean/key, analyst count, mean
    price target vs price) — quantitative sentiment context, not a signal

Deliberately shallow: it surfaces raw headlines with links; Claude judges
swing relevance and tone. A paid news API can slot in behind the same
interface later.

CLI: python -m tools.news_catalysts NVDA VRT
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

NEWS_MAX_AGE_DAYS = 7


def _parse_pubdate(value):
    """yfinance pubDate (ISO string) or legacy providerPublishTime (epoch) ->
    aware datetime, else None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _filter_recent(items: list, now: datetime, max_age_days: float = NEWS_MAX_AGE_DAYS) -> list:
    """Drop headlines older than max_age_days; stamp age_days on survivors.
    Headlines with an unparseable date are KEPT with age_days=None — fail-open
    but honestly labeled, so the brain knows the age is unknown. Pure function."""
    out = []
    for h in items:
        dt = _parse_pubdate(h.get("published"))
        if dt is None:
            out.append({**h, "age_days": None})
            continue
        age = (now - dt).total_seconds() / 86400.0
        if age <= max_age_days:
            out.append({**h, "age_days": round(age, 1)})
    return out


def _ratings_from_info(info: dict):
    """Analyst consensus snapshot from a yfinance info dict. Pure/testable;
    returns None when nothing useful is present. recommendation_mean scale:
    1=strong buy .. 5=sell."""
    if not isinstance(info, dict):
        return None
    mean = info.get("recommendationMean")
    target = info.get("targetMeanPrice")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    out = {
        "recommendation_key": info.get("recommendationKey"),
        "recommendation_mean": mean,
        "n_analysts": info.get("numberOfAnalystOpinions"),
        "target_mean_price": target,
        "target_vs_price_pct": (round((target / price - 1) * 100, 1)
                                if isinstance(target, (int, float))
                                and isinstance(price, (int, float)) and price > 0
                                else None),
    }
    return out if any(v is not None for v in out.values()) else None


def get_news_and_catalysts(tickers: list[str], max_headlines: int = 6,
                           max_age_days: float = NEWS_MAX_AGE_DAYS) -> dict:
    import yfinance as yf

    now = datetime.now(timezone.utc)
    out = {"status": "ok", "as_of": now.isoformat(),
           "news_max_age_days": max_age_days, "tickers": {}}
    for t in tickers:
        entry: dict = {"headlines": [], "next_earnings": None, "analyst_ratings": None}
        try:
            tk = yf.Ticker(t)
            raw = []
            for item in (tk.news or []):
                content = item.get("content", item)
                raw.append({
                    "title": content.get("title"),
                    "published": content.get("pubDate") or content.get("providerPublishTime"),
                    "url": (content.get("canonicalUrl") or {}).get("url") or content.get("link"),
                })
            entry["headlines"] = _filter_recent(raw, now, max_age_days)[:max_headlines]
            try:
                cal = tk.calendar
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if dates:
                    entry["next_earnings"] = str(dates[0])
            except Exception:
                pass
            try:  # analyst consensus: tk.info is flaky — degrade to None, never fail the entry
                entry["analyst_ratings"] = _ratings_from_info(tk.info)
            except Exception:
                entry["analyst_ratings"] = None
        except Exception as e:
            entry["error"] = str(e)
        out["tickers"][t.upper()] = entry
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_news_and_catalysts(sys.argv[1:] or ["NVDA"]), indent=2, default=str))
