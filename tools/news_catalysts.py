"""News & Catalyst Fetcher — swing-relevant headlines + upcoming earnings dates.

Sources (all free/keyless via yfinance):
  * per-ticker news headlines (filtered by recency)
  * next earnings date (the single most important swing catalyst clock)

Deliberately shallow: it surfaces raw headlines with links; Claude judges
swing relevance. A paid news API can slot in behind the same interface later.

CLI: python -m tools.news_catalysts NVDA VRT
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def get_news_and_catalysts(tickers: list[str], max_headlines: int = 6) -> dict:
    import yfinance as yf

    out = {"status": "ok", "as_of": datetime.now(timezone.utc).isoformat(), "tickers": {}}
    for t in tickers:
        entry: dict = {"headlines": [], "next_earnings": None}
        try:
            tk = yf.Ticker(t)
            for item in (tk.news or [])[:max_headlines]:
                content = item.get("content", item)
                entry["headlines"].append({
                    "title": content.get("title"),
                    "published": content.get("pubDate") or content.get("providerPublishTime"),
                    "url": (content.get("canonicalUrl") or {}).get("url") or content.get("link"),
                })
            try:
                cal = tk.calendar
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if dates:
                    entry["next_earnings"] = str(dates[0])
            except Exception:
                pass
        except Exception as e:
            entry["error"] = str(e)
        out["tickers"][t.upper()] = entry
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_news_and_catalysts(sys.argv[1:] or ["NVDA"]), indent=2, default=str))
