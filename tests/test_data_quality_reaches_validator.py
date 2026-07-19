"""The data-quality gate must actually run on the paths that trade.

THE INCIDENT (audited 2026-07-19)
---------------------------------
validator._check_data_quality opens with:

    dq = (market_context or {}).get("_data_quality") or {}
    if not dq:
        return   # no label published this run — nothing asserted, nothing to enforce

Everything that produced that label — degraded / low_coverage / dead_feeds / relay
fallback — lived inside `if args.gather_only:` in orchestrator.py. The live trading
branch set no label at all, and scripts/run_cycle.sh (the production cron) takes the
live branch. Measured across the last 12 real runs: dq=None, 12 of 12.

So `data_quality_stale`, `data_quality_empty` and `fundamentals_stale:<TICKER>` —
which CLAUDE.md advertises as "real rejections, not prose" — were unreachable in
production. The gate existed, was unit-tested, and had never once executed.

Separately, --act-on performed NO age check. Age was computed only in the
gather-only relay-fallback branch, so a HEALTHY gather wrote no label and a cloud
routine acting on a 10-hour-old bundle passed every freshness gate. Live prices are
overlaid for holdings/watchlist only; the fundamentals, news, filings and setups in
that bundle are arbitrarily old and were being labelled fresh.

These tests pin BOTH halves: that a degraded/aged bundle gets labelled, and that a
healthy fresh one does not get falsely flagged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import orchestrator
import validator

_CFG = validator.load_config()


def _ctx(*, age_hours=0.5, scanned=100, requested=100, macro_ok=True,
         top_setups=("AAA",), feeds_ok=True) -> dict:
    gathered = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    feed = ({"status": "ok", "tickers": {"AAA": {"status": "ok"}}} if feeds_ok
            else {"status": "ok", "tickers": {"AAA": {"error": "blocked"}}})
    return {
        "run_date": gathered.isoformat(),
        "universe_scan": {"top_setups": list(top_setups), "status": "ok",
                          "scanned": scanned, "requested": requested},
        "macro_regime": {"status": "ok" if macro_ok else "error"},
        "news_and_catalysts": feed, "sec_filings": feed, "insider_activity": feed,
    }


# ---------------------------------------------------------------------------
# bundle age
# ---------------------------------------------------------------------------

def test_bundle_age_is_read_from_the_bundles_own_run_date():
    assert orchestrator.bundle_age_hours(_ctx(age_hours=3)) == pytest.approx(3, abs=0.05)


def test_missing_or_broken_run_date_is_none_not_zero():
    """None means 'unknown age'. Zero would mean 'brand new' — the dangerous guess."""
    assert orchestrator.bundle_age_hours({}) is None
    assert orchestrator.bundle_age_hours({"run_date": "not a date"}) is None


def test_naive_timestamp_does_not_crash():
    assert orchestrator.bundle_age_hours(
        {"run_date": "2026-07-19T06:00:00"}) is not None


# ---------------------------------------------------------------------------
# labelling
# ---------------------------------------------------------------------------

def test_old_bundle_is_labelled_stale():
    """THE --act-on HOLE: a 10-hour-old healthy bundle used to pass every gate."""
    ctx = _ctx(age_hours=10)
    label = orchestrator.label_data_quality(ctx, "full")
    assert label["stale"] is True
    assert label["age_hours"] == pytest.approx(10, abs=0.05)
    assert "gathered" in ctx["stale_data_notice"]


def test_fresh_healthy_bundle_is_not_flagged_stale():
    """The inverse must hold or the gate just blocks everything."""
    label = orchestrator.label_data_quality(_ctx(age_hours=0.5), "full")
    assert not label.get("stale")


def test_degraded_bundle_is_labelled_empty():
    label = orchestrator.label_data_quality(
        _ctx(macro_ok=False, top_setups=()), "full")
    assert label["source"] == "degraded_empty" and label["stale"] is True


def test_low_coverage_is_labelled_partial_on_the_primary_trading_depth():
    """holdings_watchlist is the 4x/day PRIMARY trading slot."""
    label = orchestrator.label_data_quality(
        _ctx(scanned=40, requested=100), "holdings_watchlist")
    assert label["source"] == "live_partial"
    assert "40/100" in label["note"]


def test_dead_research_feed_is_labelled_partial():
    label = orchestrator.label_data_quality(_ctx(feeds_ok=False), "full")
    assert label["source"] == "live_partial"
    assert "feed" in label["note"]


def test_existing_relay_label_is_not_downgraded():
    """A relay/degraded stamp from the gathering node is more specific than
    anything we can infer here — never overwrite it with a weaker label."""
    ctx = _ctx(age_hours=0.5)
    ctx["data_quality"] = {"source": "relay_bundle", "age_hours": 2.0, "stale": False}
    label = orchestrator.label_data_quality(ctx, "full")
    assert label["source"] == "relay_bundle"


def test_existing_label_is_still_aged_up_when_the_bundle_is_old():
    """Not downgrading must not mean ignoring a genuinely stale bundle."""
    ctx = _ctx(age_hours=9)
    ctx["data_quality"] = {"source": "relay_bundle", "stale": False}
    label = orchestrator.label_data_quality(ctx, "full")
    assert label["source"] == "relay_bundle" and label["stale"] is True


# ---------------------------------------------------------------------------
# the label must actually REACH the validator and reject.
# ---------------------------------------------------------------------------

def _buy() -> dict:
    return {"ticker": "NVDA", "action": "BUY", "instrument": "EQUITY"}


def test_stale_label_makes_the_validator_reject_a_buy():
    """End of the chain: labelling is pointless if the rejection never fires."""
    reasons: list = []
    validator._check_data_quality(
        _buy(), _CFG, {"_data_quality": {"source": "aged_bundle", "stale": True,
                                   "age_hours": 10}}, reasons)
    assert any("data_quality_stale" in r for r in reasons), reasons


def test_empty_label_makes_the_validator_reject_a_buy():
    reasons: list = []
    validator._check_data_quality(
        _buy(), _CFG, {"_data_quality": {"source": "degraded_empty", "stale": True,
                                   "empty": True}}, reasons)
    assert any("data_quality" in r for r in reasons), reasons


def test_fundamentals_stale_names_the_ticker():
    reasons: list = []
    validator._check_data_quality(
        _buy(), _CFG, {"_data_quality": {"source": "live", "stale": False,
                                   "stale_tickers": ["NVDA"]}}, reasons)
    assert any("NVDA" in r for r in reasons), reasons


def test_healthy_label_rejects_nothing():
    reasons: list = []
    validator._check_data_quality(
        _buy(), _CFG, {"_data_quality": {"source": "live", "stale": False,
                                   "stale_tickers": []}}, reasons)
    assert reasons == []


def test_exits_are_never_blocked_by_data_quality():
    """Acting on stale data to REDUCE risk is always allowed — CLAUDE.md."""
    reasons: list = []
    validator._check_data_quality(
        {"ticker": "NVDA", "action": "SELL_TO_CLOSE"}, _CFG,
        {"_data_quality": {"source": "degraded_empty", "stale": True,
                           "empty": True}}, reasons)
    assert reasons == []


# ---------------------------------------------------------------------------
# the two paths share one implementation.
# ---------------------------------------------------------------------------

def test_health_assessment_is_shared_not_duplicated():
    """The gather-only branch and the live path must agree by construction —
    the bug was one path having the logic and the other having none."""
    ctx = _ctx(scanned=10, requested=100)
    health = orchestrator.assess_bundle_health(ctx, "holdings_watchlist")
    assert health["low_coverage"] is True
    assert health["partial"] is True
    assert health["degraded"] is False
