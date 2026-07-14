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

Optional upgrade: when FINNHUB_API_KEY is set (free tier, 60 calls/min —
https://finnhub.io) each ticker also pulls Finnhub company-news for the last
week, mapped to the same headline shape, deduped by title, and merged ahead
of the recency filter. No key -> byte-identical yfinance-only behavior.

CLI: python -m tools.news_catalysts NVDA VRT
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

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


def _headlines_from_finnhub(payload) -> list[dict]:
    """Finnhub /company-news rows -> our headline shape
    [{title, published (iso|None), url}]. Non-list/garbage -> []. Pure."""
    if not isinstance(payload, list):
        return []
    out = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("headline"):
            continue
        ts = row.get("datetime")
        published = None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            try:
                published = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
        out.append({"title": str(row["headline"]).strip(),
                    "published": published, "url": row.get("url")})
    return out


def _title_key(title) -> str:
    """Casefold + strip punctuation + collapse whitespace -> dedupe key. Pure."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(title or "").casefold())).strip()


def _merge_headlines(base: list, extra: list) -> list:
    """Append extra headlines to base, skipping extras whose title duplicates a
    base title (or an earlier extra). Base rows pass through UNTOUCHED so the
    no-key path stays byte-identical. Pure."""
    seen = {_title_key(h.get("title")) for h in base if _title_key(h.get("title"))}
    out = list(base)
    for h in extra:
        key = _title_key(h.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _fetch_finnhub_company_news(ticker: str, api_key: str, now: datetime) -> list[dict]:
    """Finnhub company-news for the last NEWS_MAX_AGE_DAYS, in headline shape.
    Fail-soft: any error -> []."""
    try:
        from tools.net import get_with_throttle_retry
        r = get_with_throttle_retry(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker.upper(),
                    "from": (now - timedelta(days=NEWS_MAX_AGE_DAYS)).date().isoformat(),
                    "to": now.date().isoformat(),
                    "token": api_key},
            timeout=(4, 8), retries=1)
        return _headlines_from_finnhub(r.json())
    except Exception:
        return []


def _beat_streak(surprise_pcts: list) -> int:
    """Consecutive positive EPS surprises counting back from the most recent
    quarter. None/zero/negative breaks the streak. Pure function."""
    streak = 0
    for s in reversed(surprise_pcts):
        if isinstance(s, (int, float)) and s > 0:
            streak += 1
        else:
            break
    return streak


def _surprise_pct(value):
    """Normalize yfinance surprisePercent: modern versions emit a fraction
    (0.053 = +5.3%); values already looking like percents pass through. NaN and
    non-numbers -> None."""
    if not isinstance(value, (int, float)) or value != value:
        return None
    return round(value * 100, 1) if abs(value) <= 1.5 else round(value, 1)


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
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
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
            if finnhub_key:  # optional merge; no key -> byte-identical behavior
                raw = _merge_headlines(raw, _fetch_finnhub_company_news(t, finnhub_key, now))
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
            try:  # sell-side scoreboard: did management beat the number, quarter by quarter
                eh = tk.earnings_history
                quarters = []
                if eh is not None and len(eh):
                    for idx, row in eh.iterrows():
                        actual = row.get("epsActual")
                        est = row.get("epsEstimate")
                        quarters.append({
                            "period": str(idx)[:10],
                            "eps_actual": (round(float(actual), 2)
                                           if actual == actual and actual is not None else None),
                            "eps_estimate": (round(float(est), 2)
                                             if est == est and est is not None else None),
                            "surprise_pct": _surprise_pct(row.get("surprisePercent")),
                        })
                if quarters:
                    entry["earnings_surprises"] = {
                        "quarters": quarters[-8:],
                        "beat_streak": _beat_streak([q["surprise_pct"] for q in quarters]),
                    }
            except Exception:
                pass
        except Exception as e:
            entry["error"] = str(e)
        out["tickers"][t.upper()] = entry
    return out


if __name__ == "__main__":
    import sys
    try:  # pick up FINNHUB_API_KEY for CLI runs; library callers load env themselves
        from pathlib import Path

        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass
    print(json.dumps(get_news_and_catalysts(sys.argv[1:] or ["NVDA"]), indent=2, default=str))
