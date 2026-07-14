"""Tests for the news recency filter and analyst-ratings snapshot in
tools/news_catalysts.py. Pure Python, no network:

    python3 tests/test_news_recency.py

Regression context: the module docstring always claimed headlines were
"filtered by recency" but no filter existed — the first N items in whatever
order yfinance returned were passed through, dates unexamined.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.news_catalysts import (_filter_recent, _headlines_from_finnhub,
                                  _merge_headlines, _parse_pubdate, _ratings_from_info)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def h(days_ago=None, raw_date=None, title="headline"):
    published = raw_date if raw_date is not None else (NOW - timedelta(days=days_ago)).isoformat()
    return {"title": title, "published": published, "url": "https://x"}


def test_filter_recent():
    print("_filter_recent:")
    items = [h(0.5, title="fresh"), h(6.9, title="edge"), h(8, title="stale"),
             h(raw_date="not-a-date", title="unknown-age")]
    out = _filter_recent(items, NOW, max_age_days=7)
    titles = [x["title"] for x in out]
    check("fresh kept", "fresh" in titles, str(titles))
    check("6.9d kept (inside window)", "edge" in titles, str(titles))
    check("8d dropped", "stale" not in titles, str(titles))
    check("unparseable date kept with age_days None",
          any(x["title"] == "unknown-age" and x["age_days"] is None for x in out))
    fresh = next(x for x in out if x["title"] == "fresh")
    check("age_days stamped", fresh["age_days"] == 0.5, str(fresh))


def test_parse_pubdate():
    print("_parse_pubdate:")
    check("ISO with Z", _parse_pubdate("2026-07-13T10:00:00Z") is not None)
    check("epoch seconds (legacy providerPublishTime)",
          _parse_pubdate(1780000000) is not None)
    check("garbage -> None", _parse_pubdate("last tuesday") is None)
    check("empty -> None", _parse_pubdate("") is None)


def test_ratings_from_info():
    print("_ratings_from_info:")
    r = _ratings_from_info({"recommendationKey": "buy", "recommendationMean": 1.8,
                            "numberOfAnalystOpinions": 42, "targetMeanPrice": 550.0,
                            "currentPrice": 500.0})
    check("fields mapped", r["recommendation_key"] == "buy" and r["n_analysts"] == 42)
    check("target vs price computed", r["target_vs_price_pct"] == 10.0, str(r))
    r = _ratings_from_info({"recommendationMean": 2.0})
    check("partial info still returned", r and r["recommendation_mean"] == 2.0)
    check("target pct None without price", r["target_vs_price_pct"] is None)
    check("empty info -> None", _ratings_from_info({}) is None)
    check("non-dict -> None", _ratings_from_info(None) is None)


def test_finnhub_merge():
    print("_headlines_from_finnhub / _merge_headlines (optional Finnhub layer):")
    payload = [
        {"headline": "NVDA wins big order", "datetime": 1784023200, "url": "https://f/1"},
        {"headline": "", "datetime": 1784023200},
        {"headline": "No timestamp", "datetime": None, "url": "https://f/2"},
    ]
    out = _headlines_from_finnhub(payload)
    check("finnhub rows mapped to headline shape (unix ts -> iso)",
          len(out) == 2 and out[0]["published"] == "2026-07-14T10:00:00+00:00"
          and out[0]["title"] == "NVDA wins big order", str(out))
    check("finnhub non-list payload -> []", _headlines_from_finnhub({"error": "x"}) == [])
    base = [{"title": "NVDA Wins Big Order!", "published": None, "url": "https://y/1"},
            {"title": None, "published": None, "url": "https://y/2"}]
    merged = _merge_headlines(base, out)
    check("merge dedupes by title, base rows untouched",
          merged[:2] == base and [m["title"] for m in merged[2:]] == ["No timestamp"],
          str(merged))


if __name__ == "__main__":
    for fn in (test_filter_recent, test_parse_pubdate, test_ratings_from_info,
               test_finnhub_merge):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All news-recency checks passed.")
