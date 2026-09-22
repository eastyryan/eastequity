"""Unit tests for the RTH price-freshness trade gate (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.live_prices import trade_gate_decision
from runlib.brain_io import apply_price_freshness_trade_gate


def test_gate_blocks_no_live_overlay_during_rth():
    d = trade_gate_decision(
        {"status": "no_live_overlay", "stale": True, "age_min": None, "applied": []},
        rth=True, max_age_min=120,
    )
    assert d["blocked"] is True
    assert d["code"] == "no_live_overlay"
    assert d["reason"].startswith("price_freshness_gate:no_live_overlay")


def test_gate_blocks_stale_overlay_over_2h_rth():
    d = trade_gate_decision(
        {"status": "ok", "stale": True, "age_min": 185, "applied": ["AMD"]},
        rth=True, max_age_min=120,
    )
    assert d["blocked"] is True
    assert d["code"] == "stale_live_overlay"
    assert "185>120" in d["reason"]


def test_gate_allows_fresh_overlay_rth():
    d = trade_gate_decision(
        {"status": "ok", "stale": False, "age_min": 12, "applied": ["AMD"]},
        rth=True, max_age_min=120,
    )
    assert d["blocked"] is False
    assert d["code"] == "ok"


def test_gate_stands_down_outside_rth():
    d = trade_gate_decision(
        {"status": "no_live_overlay", "stale": True, "age_min": 999},
        rth=False, max_age_min=120,
    )
    assert d["blocked"] is False
    assert d["code"] == "outside_rth"


def test_apply_gate_filters_buys_and_discretionary_sells(monkeypatch, tmp_path):
    """apply_price_freshness_trade_gate must drop BUY/SELL, keep other actions."""
    logged = []

    def _log(proposal, reasons, run_id):
        logged.append((proposal.get("ticker"), reasons[0], run_id))

    import journal
    monkeypatch.setattr(journal, "log_rejection", _log)

    context = {
        "price_freshness_live": {
            "status": "no_live_overlay", "stale": True, "age_min": None, "applied": [],
        }
    }
    cfg = {"schedule": {"live_prices": {
        "trade_gate_enabled": True, "trade_gate_max_age_min": 120,
    }}}
    proposals = [
        {"ticker": "AAA", "action": "BUY"},
        {"ticker": "BBB", "action": "SELL"},
        {"ticker": "CCC", "action": "HOLD"},
    ]
    # Force RTH=True inside trade_gate_decision via monkeypatch
    import tools.live_prices as lp
    real = lp.trade_gate_decision

    def _forced(report, *, rth=None, max_age_min=120.0):
        return real(report, rth=True, max_age_min=max_age_min)

    monkeypatch.setattr(lp, "trade_gate_decision", _forced)

    kept = apply_price_freshness_trade_gate(proposals, context, cfg, "RID-TEST")
    assert [p["ticker"] for p in kept] == ["CCC"]
    assert len(logged) == 2
    assert all(r.startswith("price_freshness_gate:") for _, r, _ in logged)
    assert context["price_freshness_trade_gate"]["blocked"] is True


def test_apply_gate_disabled_passes_through():
    context = {"price_freshness_live": {"status": "no_live_overlay", "stale": True}}
    cfg = {"schedule": {"live_prices": {"trade_gate_enabled": False}}}
    proposals = [{"ticker": "AAA", "action": "BUY"}]
    kept = apply_price_freshness_trade_gate(proposals, context, cfg, "RID")
    assert kept == proposals
