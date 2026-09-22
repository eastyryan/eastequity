"""holdings_watchlist must still run tape/8-K promotions + holdings deep-dive.

The depth skips the full-universe scan on purpose (speed). That must NOT skip:
  * tape + 8-K promotions into deep focus
  * deep research on holdings (+ watchlist / promotions)

Pinned after a near-miss where "no full universe" was read as "no research".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("EE_BROKER", "simulation")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlib.context_gather as cg  # noqa: E402
from runlib.depths import focus_ticker_budget  # noqa: E402


def test_holdings_watchlist_budget_keeps_tape_and_filings():
    b = focus_ticker_budget("holdings_watchlist")
    assert b["full_universe_scan"] is False
    assert b["filings_sweep"] is True
    assert int(b.get("tape_promote_max") or 0) >= 1
    assert b["deep_research"] == "holdings_watchlist_triggers"
    assert b["scan_mode"] == "focus_only"


def test_holdings_watchlist_runs_tape_promote_and_deep_dive():
    """Even with full_universe_scan=False, tape/8-K promote + deep research run."""
    budget = focus_ticker_budget("holdings_watchlist")
    assert budget["full_universe_scan"] is False

    held = ["NVDA"]
    watch = ["AVGO"]
    calls = {"tape": 0, "deep": 0, "scan_tickers": None, "8k": 0, "news": 0}

    fake_scan = {
        "status": "ok",
        "prices": {"NVDA": 100.0, "AVGO": 200.0},
        "prices_meta": {
            "NVDA": {"price_as_of": "2026-09-22", "source": "daily_bar"},
            "AVGO": {"price_as_of": "2026-09-22", "source": "daily_bar"},
        },
        "top_setups": [],
        "atr_by_ticker": {},
    }

    def fake_scan_universe(top_n=5, tickers=None, enrich=False, **kwargs):
        calls["scan_tickers"] = list(tickers) if tickers is not None else None
        assert tickers is not None, "must pass explicit ticker list (no full-universe fallthrough)"
        assert set(tickers) <= {"NVDA", "AVGO"}
        assert enrich is False
        return dict(fake_scan)

    def fake_tape(focus, market_news, todays_8ks, max_extra, market_events=None):
        calls["tape"] += 1
        assert max_extra >= 1
        assert market_news is not None
        assert todays_8ks is not None
        return ["ANET"], [{"ticker": "ANET", "reason": "8k"}]

    def fake_deep(focus):
        calls["deep"] += 1
        assert "NVDA" in focus and "AVGO" in focus and "ANET" in focus
        ok = {t: {"status": "ok"} for t in focus}
        return ok, ok, ok

    def fake_news_fetch():
        calls["news"] += 1
        return {"status": "ok", "headlines": [{"title": "Arista $ANET files 8-K"}]}

    def fake_8k_fetch():
        calls["8k"] += 1
        return {"status": "ok", "filers": [{"ticker": "ANET", "form": "8-K"}]}

    with patch.object(cg, "scan_universe", fake_scan_universe), \
         patch.object(cg, "tape_and_8k_promotions", fake_tape), \
         patch.object(cg, "deep_research_bundle", fake_deep), \
         patch.object(cg, "fetch_market_news", fake_news_fetch), \
         patch.object(cg, "fetch_universe_8ks", fake_8k_fetch), \
         patch.object(cg, "filter_trigger_alerts", return_value=([], [])), \
         patch.object(cg, "check_watchlist_triggers", return_value=[]), \
         patch.object(cg, "light_prices", return_value={"ANET": 300.0}), \
         patch.object(cg, "get_options_signals", return_value={"status": "skipped"}), \
         patch.object(cg, "get_ledger_summary", return_value={"status": "skipped"}), \
         patch.object(cg, "get_smart_money", return_value={"status": "skipped"}), \
         patch.object(cg, "get_news_and_catalysts",
                      return_value={"status": "ok", "tickers": {}}), \
         patch.object(cg, "get_insider_activity", return_value={"status": "skipped"}), \
         patch.object(cg, "auto_grade", return_value=None), \
         patch("tools.fundamental_screen.get_screen",
               return_value={"status": "skipped"}), \
         patch("tools.partnerships.get_partnerships",
               return_value={"status": "skipped"}), \
         patch("tools.price_chart.render_charts", return_value=None):
        out = cg._gather_holdings_watchlist_research(
            held=held, watch=watch, prior_watchlist=[],
            budget=budget, market_events={},
            earnings_reporters=[], depth="holdings_watchlist")

    assert calls["tape"] == 1, "tape/8-K promotions must run on holdings_watchlist"
    assert calls["deep"] == 1, "holdings deep-dive must run on holdings_watchlist"
    assert calls["news"] == 1 and calls["8k"] == 1
    assert calls["scan_tickers"] is not None
    assert "ANET" in out["focus"]
    assert out["tape_promotions"] and out["tape_promotions"][0]["ticker"] == "ANET"
    assert out["market_news"] is not None and out["todays_8ks"] is not None
    assert (out.get("discovery_block") or {}).get("status", "").startswith("skipped")


def test_research_freshness_fails_loud_on_empty_critical_lanes():
    rf = cg.build_research_freshness(
        depth="full",
        scan={"prices_meta": {
            "NVDA": {"price_as_of": "2026-09-01", "source": "daily_bar"},
        }},
        news={"status": "error"},
        filings={"NVDA": {"status": "error"}},
        earnings_week={"calendar_status": "empty", "names_in_calendar": 0, "stale": True},
    )
    assert rf["critical_lanes_empty"] is True
    assert rf["fail_loud"]
    assert "news_and_catalysts" in rf["empty_critical_lanes"]
    assert "sec_filings" in rf["empty_critical_lanes"]
    assert "earnings_calendar" in rf["empty_critical_lanes"]
    assert rf["price_as_of"]["bars_older_than_et_today"] == 1


def test_slim_pack_promotes_research_freshness_fail_loud():
    from runlib.context_tiers import slim_context_for_brain, BLOCKING_KEYS
    assert "research_freshness" in BLOCKING_KEYS
    full = {
        "run_date": "2026-09-22T12:00:00+00:00",
        "as_of_et": "2026-09-22",
        "trading_mode": "paper",
        "run_depth": "full",
        "run_depth_note": "full",
        "allows_new_buys": True,
        "hard_limits": {},
        "digest": {"by_ticker": {}},
        "research_freshness": {
            "status": "critical_lanes_empty",
            "critical_lanes_empty": True,
            "empty_critical_lanes": ["news_and_catalysts"],
            "fail_loud": "CRITICAL RESEARCH LANES EMPTY/DEAD: news_and_catalysts",
            "price_as_of": {"n_priced": 0, "bars_older_than_et_today": 0, "sample": {}},
            "live_overlay": {"status": "not_yet_applied"},
            "critical_lanes": {},
        },
        "reasoning_process": {"run_depth": "full"},
        "portfolio": {"positions": []},
    }
    slim = slim_context_for_brain(full)
    assert "research_freshness" in slim
    assert "CRITICAL RESEARCH LANES" in (slim.get("stale_data_notice") or "")
    dq = slim.get("data_quality") or {}
    assert dq.get("empty") is True or dq.get("research_lanes_empty") is True
