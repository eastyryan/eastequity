"""Offline unit tests for tools.market_radar (pure build_radar assembly) and
the alpaca-news merge shapes. No network.

    python3 -m pytest tests/test_market_radar.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.market_radar import (  # noqa: E402
    MIN_MOVER_PRICE_USD, _clean_symbol, _news_mention_counts, build_radar,
)


UNIVERSE = {"NVDA", "DELL", "MU"}


def _movers():
    return {
        "gainers": [
            {"symbol": "NVDA", "percent_change": 4.2, "price": 190.0},
            {"symbol": "EVLVW", "percent_change": 223.0, "price": 0.0042},  # penny warrant
            {"symbol": "CDNA", "percent_change": 35.6, "price": 18.4},
            {"symbol": "SOXL", "percent_change": 12.0, "price": 40.0},      # forbidden
            {"symbol": "BRK.B", "percent_change": 2.0, "price": 500.0},     # bad format
        ],
        "losers": [
            {"symbol": "MU", "percent_change": -6.0, "price": 120.0},
            {"symbol": "NVVE", "percent_change": -61.0, "price": 1.2},      # under floor
        ],
    }


def _actives():
    return [
        {"symbol": "NVDA", "volume": 1000, "trade_count": 900},
        {"symbol": "SPY", "volume": 999, "trade_count": 800},    # ETF noise
        {"symbol": "INTC", "volume": 500, "trade_count": 400},
    ]


def _news():
    return [
        {"title": "a", "symbols": ["INTC", "YUM"]},
        {"title": "b", "symbols": ["YUM"]},
        {"title": "c", "symbols": ["yum", "NVDA"]},
        {"title": "d", "symbols": ["BRK.B"]},  # bad format ignored
    ]


def test_clean_symbol():
    assert _clean_symbol(" nvda ") == "NVDA"
    assert _clean_symbol("BRK.B") is None
    assert _clean_symbol("TOOLONG") is None
    assert _clean_symbol(None) is None


def test_news_mention_counts_normalizes_and_counts():
    counts = _news_mention_counts(_news())
    assert counts["YUM"] == 3
    assert counts["INTC"] == 1
    assert "BRK.B" not in counts


def test_build_radar_filters_and_tags():
    r = build_radar(_movers(), _actives(), _news(), UNIVERSE,
                    forbidden={"SOXL"})
    g = {row["ticker"] for row in r["gainers"]}
    assert g == {"NVDA", "CDNA"}          # penny, forbidden, bad-format dropped
    assert all(row["price"] >= MIN_MOVER_PRICE_USD for row in r["gainers"])
    l = {row["ticker"] for row in r["losers"]}
    assert l == {"MU"}
    a = {row["ticker"] for row in r["most_active"]}
    assert a == {"NVDA", "INTC"}          # SPY noise dropped
    by_ticker = {row["ticker"]: row for row in r["gainers"] + r["most_active"]}
    assert by_ticker["NVDA"]["in_universe"] is True
    assert by_ticker["INTC"]["in_universe"] is False
    assert by_ticker["NVDA"]["news_mentions_24h"] == 1


def test_build_radar_news_trending_excludes_surfaced():
    r = build_radar(_movers(), _actives(), _news(), UNIVERSE, forbidden=set())
    trending = {row["ticker"] for row in r["news_trending"]}
    # YUM has 3 mentions and was not surfaced elsewhere; INTC/NVDA were surfaced
    assert trending == {"YUM"}
    assert "YUM" in r["off_universe_symbols"]
    assert "NVDA" not in r["off_universe_symbols"]


def test_build_radar_empty_inputs():
    r = build_radar({}, [], [], set(), forbidden=set())
    assert r["gainers"] == [] and r["losers"] == []
    assert r["most_active"] == [] and r["news_trending"] == []
    assert r["off_universe_symbols"] == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
