"""The data-health check must actually detect a dead feed.

Before 2026-07-19 the predicate lived as a closure inside orchestrator.main(), was
never unit-tested, and was dead code for two of the three feeds it guarded:
`sec_filings` has no top-level status (a total EDGAR outage read healthy) and
`news_and_catalysts` hardcodes {"status": "ok"} and never downgrades. These pin both
the real shapes in the bundle today and the uniform contract being rolled out.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import feed_is_alive  # noqa: E402


# --------------------------------------------------------------------------- #
# The shapes that actually appear in the bundle
# --------------------------------------------------------------------------- #
def test_explicit_error_status_is_dead():
    assert feed_is_alive({"status": "error", "reason": "EDGAR 503"}) is False
    assert feed_is_alive({"status": "unavailable"}) is False


def test_healthy_feed_is_alive():
    assert feed_is_alive({"status": "ok", "tickers": {"NVDA": {"headlines": []}}}) is True


def test_sec_filings_shape_with_every_ticker_failed_is_dead():
    """The bug: a {TICKER: brief} map has no top-level status, so the old predicate
    returned True for a total EDGAR outage."""
    feed = {"NVDA": {"status": "error"}, "DELL": {"status": "error"}}
    assert feed_is_alive(feed) is False


def test_sec_filings_shape_with_some_tickers_ok_is_alive():
    """The mirror. Without it, 'any error means dead' would pass the test above and
    mark a normally-partial feed dead on one bad ticker."""
    feed = {"NVDA": {"status": "ok"}, "DELL": {"status": "error"}}
    assert feed_is_alive(feed) is True


def test_hardcoded_ok_over_all_failed_tickers_is_dead():
    """news_catalysts.py:199 sets status 'ok' unconditionally and demotes failures to
    per-ticker `error` keys, so the top level cannot be trusted on its own."""
    feed = {"status": "ok", "tickers": {"NVDA": {"error": "timeout"},
                                        "DELL": {"error": "timeout"}}}
    assert feed_is_alive(feed) is False


def test_hardcoded_ok_with_one_good_ticker_is_alive():
    feed = {"status": "ok", "tickers": {"NVDA": {"headlines": [1]},
                                        "DELL": {"error": "timeout"}}}
    assert feed_is_alive(feed) is True


# --------------------------------------------------------------------------- #
# Degenerate inputs must not read as healthy
# --------------------------------------------------------------------------- #
def test_missing_or_malformed_feed_is_dead():
    assert feed_is_alive(None) is False
    assert feed_is_alive([]) is False
    assert feed_is_alive("ok") is False


def test_empty_dict_is_alive():
    """An empty feed is 'nothing to report', not 'the fetch died' — a tool that
    genuinely failed is required to say so via status. Pinned so the distinction
    stays deliberate rather than accidental."""
    assert feed_is_alive({}) is True


def test_skipped_feeds_are_not_treated_as_failures():
    """Depth-gated feeds legitimately report skipped; that is not degradation."""
    for status in ("skipped", "skipped_light_run", "light", "holdings_watchlist"):
        assert feed_is_alive({"status": status}) is True, status
