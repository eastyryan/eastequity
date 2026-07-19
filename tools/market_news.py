"""Market-wide headlines — keyless RSS sweep of major financial news feeds.

Complements tools/news_catalysts.py (per-ticker): this answers "what is the
TAPE talking about today?" — macro prints, sector rotations, big single-name
moves the per-ticker feeds miss because the mover isn't a focus name.

Sources (all keyless RSS, each fetched with a short timeout and fail-soft):
  * Yahoo Finance top news
  * CNBC top news
  * MarketWatch top stories
Plus an OPTIONAL Finnhub general-news merge when FINNHUB_API_KEY is set
(free tier, 60 calls/min — https://finnhub.io). No key -> RSS only.
Plus an OPTIONAL Alpaca News merge (Benzinga-sourced, included in the free
Basic Market Data plan) when ALPACA_API_KEY/SECRET are set — real timestamps
and per-headline symbol tags the RSS feeds lack.

Headlines are deduped on near-identical titles, stamped with age_hours,
filtered to the last 24h (undated items are kept, honestly labeled with
age_hours=None), sorted newest first, and capped.

CLI: python -m tools.market_news
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MAX_AGE_HOURS = 24.0

FEEDS = [
    ("yahoo_finance", "https://finance.yahoo.com/news/rssindex"),
    ("cnbc", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("marketwatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

# Some feeds (Yahoo especially) reject requests with no/robot User-Agent.
_UA = {"User-Agent": "Mozilla/5.0 (EastEquityAgent; easton.ryan@hws.edu)"}


def _parse_rss_date(value):
    """RSS pubDate (RFC 822 like 'Sun, 13 Jul 2026 10:00:00 GMT') or ISO ->
    aware datetime, else None. Pure."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_rss(xml_text: str, source: str) -> list[dict]:
    """RSS 2.0 <item> rows -> [{title, source, published(iso|None), url}].
    Bad/empty XML -> []. Items without a title are skipped. Pure function so
    tests can feed fixture strings."""
    if not xml_text or not isinstance(xml_text, str):
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        dt = _parse_rss_date((item.findtext("pubDate") or "").strip())
        link = (item.findtext("link") or "").strip() or None
        out.append({"title": title, "source": source,
                    "published": dt.isoformat() if dt else None, "url": link})
    return out


def _norm_title(title: str) -> str:
    """Casefold + strip punctuation + collapse whitespace -> dedupe key. Pure."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(title).casefold())).strip()


def dedupe_headlines(items: list[dict]) -> list[dict]:
    """Drop near-identical titles (casefold, punctuation-stripped), keeping the
    first occurrence. Pure."""
    seen: set[str] = set()
    out = []
    for h in items:
        key = _norm_title(h.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def select_headlines(items: list[dict], now: datetime, max_items: int = 15,
                     max_age_hours: float = MAX_AGE_HOURS) -> list[dict]:
    """Stamp age_hours (1dp; None when the date is missing/unparseable), drop
    items older than max_age_hours WHEN a date exists (fail-open on undated),
    sort newest first (undated last), cap at max_items. Pure."""
    kept = []
    for h in items:
        dt = _parse_rss_date(h.get("published"))
        if dt is None:
            kept.append({**h, "age_hours": None})
            continue
        age = (now - dt).total_seconds() / 3600.0
        if age <= max_age_hours:
            kept.append({**h, "age_hours": round(age, 1)})
    kept.sort(key=lambda h: h["age_hours"] if h["age_hours"] is not None else float("inf"))
    return kept[:max_items]


def _headlines_from_finnhub_general(payload, limit: int = 10) -> list[dict]:
    """Finnhub /news?category=general rows -> our headline shape. Pure."""
    if not isinstance(payload, list):
        return []
    out = []
    for row in payload[:limit]:
        if not isinstance(row, dict) or not row.get("headline"):
            continue
        ts = row.get("datetime")
        published = None
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                published = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
        out.append({"title": str(row["headline"]).strip(), "source": "finnhub",
                    "published": published, "url": row.get("url")})
    return out


def get_market_news(max_items: int = 15) -> dict:
    """Sweep the RSS feeds (+ optional Finnhub general news), merge, dedupe,
    age-filter, sort newest first. Per-feed fail-soft: a dead feed is skipped
    and reported in "sources" with ok:false; only all-dead -> status error."""
    from tools.net import get_with_throttle_retry

    now = datetime.now(timezone.utc)
    sources, items = [], []
    for name, url in FEEDS:
        try:
            r = get_with_throttle_retry(url, headers=_UA, timeout=(4, 8), retries=1)
            parsed = parse_rss(r.text, name)
            items.extend(parsed)
            sources.append({"name": name, "ok": True, "items": len(parsed)})
        except Exception as e:
            sources.append({"name": name, "ok": False, "error": str(e)[:200]})

    try:
        from tools import alpaca_data
        if alpaca_data.has_keys():
            # get_news_result reports whether the CALL completed. The bare
            # get_news returns [] on network error, non-200 and on a quiet feed
            # alike, so this block used to record a dead Alpaca feed as
            # {"ok": True, "items": 0} — and that False "ok" also propped up
            # any_ok below, letting a total news outage report status "ok".
            rows, err = alpaca_data.get_news_result(hours=MAX_AGE_HOURS, limit=30)
            if err is None:
                items.extend(rows)
                sources.append({"name": "alpaca_news", "ok": True, "items": len(rows)})
            else:
                sources.append({"name": "alpaca_news", "ok": False, "error": err})
    except Exception as e:
        sources.append({"name": "alpaca_news", "ok": False, "error": str(e)[:200]})

    api_key = os.environ.get("FINNHUB_API_KEY")
    if api_key:
        try:
            r = get_with_throttle_retry("https://finnhub.io/api/v1/news",
                                        params={"category": "general", "token": api_key},
                                        timeout=(4, 8), retries=1)
            parsed = _headlines_from_finnhub_general(r.json())
            items.extend(parsed)
            sources.append({"name": "finnhub", "ok": True, "items": len(parsed)})
        except Exception as e:
            sources.append({"name": "finnhub", "ok": False, "error": str(e)[:200]})

    headlines = select_headlines(dedupe_headlines(items), now, max_items=max_items)
    any_ok = any(s["ok"] for s in sources)
    out = {"status": "ok" if any_ok else "error", "as_of": now.isoformat(),
           "sources": sources, "headlines": headlines}
    if not any_ok:
        out["error"] = "all market-news sources failed"
    return out


if __name__ == "__main__":
    try:
        from pathlib import Path

        from tools.envload import load_env
        load_env()
    except Exception:
        pass
    print(json.dumps(get_market_news(), indent=2, default=str))
