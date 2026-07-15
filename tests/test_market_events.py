"""Offline unit tests for pure helpers in tools.market_events
(headline classification, flagging, commodity pct math). No network.

    python3 -m pytest tests/test_market_events.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.market_events import (  # noqa: E402
    classify_headline, flag_headlines, summarize_flags, _pct_change,
    get_market_events, KEYWORD_BUCKETS,
)


def test_classify_geopolitical():
    tags = classify_headline("Iran strikes escalate as Ukraine war grinds on")
    assert "geopolitical" in tags
    assert "inflation_rates" not in tags


def test_classify_inflation_rates():
    tags = classify_headline("Fed holds rates steady ahead of hot CPI print")
    assert "inflation_rates" in tags


def test_classify_financial_stress():
    tags = classify_headline("Regional bank liquidity concerns spark credit stress fears")
    assert "financial_stress" in tags


def test_classify_sector_macro():
    tags = classify_headline("OPEC oil cut meets semiconductor ban headlines")
    assert "sector_macro" in tags
    # oil cut is sector_macro; may also hit geopolitical if "strike" etc. present


def test_classify_multi_bucket():
    tags = classify_headline("China tariff threat lifts Treasury yields after Fed pivot talk")
    assert "geopolitical" in tags  # china, tariff
    assert "inflation_rates" in tags  # yields, fed


def test_classify_empty_and_noise():
    assert classify_headline("") == []
    assert classify_headline(None) == []  # type: ignore[arg-type]
    assert classify_headline("Apple beats earnings on iPhone strength") == []


def test_classify_does_not_false_positive_fed_in_federal_alone_without_phrase():
    # bare "federal" alone is not in the keyword list; "federal reserve" is.
    assert "inflation_rates" not in classify_headline("Federal case filed in Delaware")
    assert "inflation_rates" in classify_headline("Federal Reserve signals patience")


def test_flag_headlines_cap_and_shape():
    items = [
        {"title": "War escalates in Ukraine", "age_hours": 1.0},
        {"title": "Boring product launch", "age_hours": 2.0},
        {"title": "CPI cools more than expected", "age_hours": 3.0},
        {"title": "Bank run fears return", "age_hours": 4.0},
        {"title": "OPEC signals oil production cut", "age_hours": 5.0},
        {"title": "More war news from Iran", "age_hours": 6.0},
        {"title": "Yield spike on rate cut bets", "age_hours": 7.0},
        {"title": "Liquidity crunch at regional bank", "age_hours": 8.0},
        {"title": "China tariff headline nine", "age_hours": 9.0},
        {"title": "Semiconductor ban expanded", "age_hours": 10.0},
        {"title": "Sanction package approved", "age_hours": 11.0},
    ]
    out = flag_headlines(items, max_flags=8)
    assert len(out) == 8
    assert all(set(x.keys()) == {"title", "tags", "age_hours"} for x in out)
    assert out[0]["title"].startswith("War")
    assert "geopolitical" in out[0]["tags"]
    # non-matching "Boring product launch" skipped
    assert all("Boring" not in x["title"] for x in out)


def test_flag_headlines_empty_input():
    assert flag_headlines(None) == []  # type: ignore[arg-type]
    assert flag_headlines([]) == []
    assert flag_headlines([{"no_title": True}]) == []


def test_summarize_flags():
    flagged = flag_headlines([
        {"title": "Iran war risk", "age_hours": 1},
        {"title": "Fed rate cut odds rise", "age_hours": 2},
        {"title": "Iran and Fed both in one headline on yields", "age_hours": 3},
    ])
    s = summarize_flags(flagged)
    assert s["n_flagged"] == len(flagged)
    assert "geopolitical" in s["active"]
    assert "inflation_rates" in s["active"]
    assert s["counts"]["geopolitical"] >= 1
    assert set(s["counts"]) == set(KEYWORD_BUCKETS)


def test_pct_change_math():
    # 6 bars: base for 5d is first; 1d is penultimate
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 110.0]
    last, p1, p5 = _pct_change(closes)
    assert last == 110.0
    assert p1 == round((110 / 104 - 1) * 100, 2)
    assert p5 == round((110 / 100 - 1) * 100, 2)
    assert _pct_change([]) == (None, None, None)
    assert _pct_change([50.0])[0] == 50.0
    assert _pct_change([50.0])[1] is None


def test_get_market_events_with_injected_headlines_no_network():
    """Caller-supplied headlines + skip_commodities -> no network, never raises."""
    headlines = [
        {"title": "Ukraine war update", "age_hours": 0.5},
        {"title": "Market opens mixed", "age_hours": 1.0},
        {"title": "Hot CPI jolts yields", "age_hours": 2.0},
    ]
    out = get_market_events(headlines=headlines, skip_commodities=True)
    assert out["status"] in ("ok", "degraded")
    assert "as_of" in out
    assert out["commodities"] == {}
    assert out["headline_source"] == "caller"
    titles = [h["title"] for h in out["flagged_headlines"]]
    assert any("Ukraine" in t for t in titles)
    assert any("CPI" in t for t in titles)
    assert "Market opens mixed" not in titles
    assert set(out["flags"]["counts"]) == set(KEYWORD_BUCKETS)
    assert "note" in out
