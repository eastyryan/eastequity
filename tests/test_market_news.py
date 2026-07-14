"""Tests for the market-wide headline sweep in tools/market_news.py.
Pure Python, no network — parse_rss and the selection pipeline are fed
fixture strings:

    python3 tests/test_market_news.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.market_news import (_headlines_from_finnhub_general, dedupe_headlines,
                               parse_rss, select_headlines)

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item>
    <title>Fed holds rates steady</title>
    <link>https://example.com/fed</link>
    <pubDate>Tue, 14 Jul 2026 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Chips rally on AI capex</title>
    <link>https://example.com/chips</link>
    <pubDate>2026-07-14T08:30:00Z</pubDate>
  </item>
  <item>
    <title>Undated market story</title>
    <link>https://example.com/undated</link>
  </item>
  <item>
    <link>https://example.com/no-title</link>
    <pubDate>Tue, 14 Jul 2026 09:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_parse_rss():
    print("parse_rss:")
    items = parse_rss(RSS_FIXTURE, "testfeed")
    check("titled items extracted (titleless skipped)", len(items) == 3,
          str([i["title"] for i in items]))
    fed = next((i for i in items if i["title"] == "Fed holds rates steady"), None)
    check("source stamped", fed and fed["source"] == "testfeed", str(fed))
    check("url extracted", fed and fed["url"] == "https://example.com/fed", str(fed))
    check("RFC822 pubDate -> iso",
          fed and fed["published"] == "2026-07-14T10:00:00+00:00", str(fed))
    iso = next((i for i in items if i["title"].startswith("Chips")), None)
    check("ISO pubDate parsed",
          iso and iso["published"] == "2026-07-14T08:30:00+00:00", str(iso))
    undated = next((i for i in items if i["title"].startswith("Undated")), None)
    check("missing pubDate -> published None",
          undated is not None and undated["published"] is None, str(undated))
    check("bad XML -> []", parse_rss("<rss><channel><item>", "x") == [])
    check("non-XML -> []", parse_rss("totally not xml", "x") == [])
    check("empty -> []", parse_rss("", "x") == [])
    check("None -> []", parse_rss(None, "x") == [])


def test_dedupe():
    print("dedupe_headlines:")
    items = [
        {"title": "Fed Holds Rates Steady!", "source": "a"},
        {"title": "fed holds rates steady", "source": "b"},
        {"title": "Fed holds; rates steady", "source": "c"},
        {"title": "A different story", "source": "d"},
        {"title": "", "source": "e"},
    ]
    out = dedupe_headlines(items)
    check("near-identical titles collapsed to first", len(out) == 2,
          str([(h["title"], h["source"]) for h in out]))
    check("first occurrence kept", out[0]["source"] == "a", str(out[0]))
    check("distinct title survives", out[1]["title"] == "A different story")
    check("empty title dropped", all(h["title"] for h in out))


def h(hours_ago=None, published=None, title="headline"):
    if published is None and hours_ago is not None:
        published = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"title": title, "source": "s", "published": published, "url": "https://x"}


def test_select_headlines():
    print("select_headlines:")
    items = [h(30, title="stale"), h(2, title="fresh"), h(23.5, title="edge"),
             h(published="garbage-date", title="undated")]
    out = select_headlines(items, NOW, max_items=15)
    titles = [x["title"] for x in out]
    check(">24h dated item dropped", "stale" not in titles, str(titles))
    check("23.5h kept (inside window)", "edge" in titles, str(titles))
    check("undated kept with age_hours None",
          any(x["title"] == "undated" and x["age_hours"] is None for x in out))
    check("age_hours stamped 1dp",
          next(x for x in out if x["title"] == "fresh")["age_hours"] == 2.0)
    check("newest first, undated last",
          titles == ["fresh", "edge", "undated"], str(titles))
    capped = select_headlines([h(i, title=f"t{i}") for i in range(20)], NOW, max_items=5)
    check("capped at max_items", len(capped) == 5)
    check("cap keeps the newest", [x["title"] for x in capped] == [f"t{i}" for i in range(5)],
          str([x["title"] for x in capped]))


def test_finnhub_general():
    print("_headlines_from_finnhub_general:")
    payload = [
        {"headline": "Market rallies", "datetime": 1784023200, "url": "https://f/1"},
        {"headline": "", "datetime": 1784023200},
        {"not_a_headline": True},
        {"headline": "No timestamp", "datetime": None},
    ]
    out = _headlines_from_finnhub_general(payload)
    check("valid rows mapped, empty/garbage skipped", len(out) == 2,
          str([o["title"] for o in out]))
    check("unix ts -> iso", out[0]["published"] == "2026-07-14T10:00:00+00:00",
          str(out[0]))
    check("source is finnhub", out[0]["source"] == "finnhub")
    check("missing ts -> published None", out[1]["published"] is None, str(out[1]))
    check("non-list payload -> []", _headlines_from_finnhub_general({"a": 1}) == [])
    check("limit respected",
          len(_headlines_from_finnhub_general(
              [{"headline": f"h{i}", "datetime": 1784023200} for i in range(30)])) == 10)


if __name__ == "__main__":
    for fn in (test_parse_rss, test_dedupe, test_select_headlines, test_finnhub_general):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All market-news checks passed.")
