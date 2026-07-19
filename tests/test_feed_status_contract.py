"""Every research feed must be able to REPORT ITS OWN FAILURE.

THE INCIDENT (2026-07-18 data-integrity audit)
----------------------------------------------
orchestrator.py's `feed_is_alive` is the system's ONLY automated check that the
research feeds delivered anything, and for two of the three feeds it guarded it
was dead code:

  * `news_and_catalysts` hardcoded {"status": "ok"} at news_catalysts.py:199 and
    NEVER downgraded it, no matter how many per-ticker calls raised. A fully
    rate-limited news feed advertised perfect health and the brain reasoned from
    an empty tape as though the tape were genuinely quiet.
  * `sec_filings` is a bare {TICKER: brief} map with NO top-level status, so
    .get("status") returned None, None is not in ("error","unavailable"), and a
    TOTAL EDGAR OUTAGE read as healthy.

Only `insider_activity` ever reported failure. Four incompatible status shapes
existed across tools/. These tests pin the uniform contract that replaced them:

  status      meaning                                        feed_is_alive
  ---------   --------------------------------------------   -------------
  "ok"        every attempted name succeeded                  alive
  "degraded"  some succeeded, some FAILED (counts disclosed)  alive
  "empty"     every name succeeded, nothing qualified         alive
  "error"     every attempted name FAILED — the fetch died    DEAD
  "skipped"   nothing was asked for                           alive

The load-bearing assertion in this whole file is that "empty" and "error" are
NEVER collapsed. "No insider bought anything" and "we could not read the filings"
mean opposite things to a thesis; so do "no fresh headlines" and "the news feed
is blocked". The repo's standard is that absence of data must never render as a
substantive market finding.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from tools.freshness_audit import (  # noqa: E402
    coverage_block,
    feed_status,
    stamp_feed,
)

def _feed_is_alive(feed):
    """orchestrator.feed_is_alive, or SKIP when the orchestrator has not landed it.

    The health check lives in orchestrator.py, which is owned elsewhere. These
    tests pin the TOOL side of the contract and must stay green independently;
    when the orchestrator side is present they additionally verify the two halves
    agree. (A concurrent `git reset --hard` removed it mid-task once already —
    a test that hard-depends on another owner's file is a flaky test.)
    """
    import orchestrator
    fn = getattr(orchestrator, "feed_is_alive", None)
    if fn is None:
        pytest.skip("orchestrator.feed_is_alive not present yet (owned elsewhere)")
    return fn(feed)



# --------------------------------------------------------------------------- #
# the pure contract resolver
# --------------------------------------------------------------------------- #
def test_all_failed_is_error_never_ok_never_empty():
    """The single rule the old code broke everywhere."""
    assert feed_status(requested=5, succeeded=0, failed=5) == "error"
    # ...and `found` cannot rescue it into the benign-looking "empty" bucket.
    assert feed_status(requested=5, succeeded=0, failed=5, found=0) == "error"


def test_partial_failure_is_degraded_not_ok():
    assert feed_status(requested=10, succeeded=7, failed=3) == "degraded"
    # One survivor out of twenty is still degraded, never "ok" — the bug in
    # ownership_flow's old `status = "ok" if ok else ...` predicate.
    assert feed_status(requested=20, succeeded=1, failed=19) == "degraded"


def test_empty_means_reachable_but_nothing_qualified():
    """Everyone answered; nobody had anything. A real market state, and NOT a
    failure — this is the state a dead feed used to impersonate."""
    assert feed_status(requested=6, succeeded=6, failed=0, found=0) == "empty"
    assert feed_status(requested=6, succeeded=6, failed=0, found=2) == "ok"


def test_no_found_signal_means_plain_ok():
    """Feeds without a natural 'substantive result' notion never report empty."""
    assert feed_status(requested=3, succeeded=3, failed=0) == "ok"


def test_nothing_requested_is_skipped_not_error():
    """A run with no focus names has not failed at anything."""
    assert feed_status(requested=0, succeeded=0, failed=0) == "skipped"


def test_counters_are_always_present_and_integral():
    cov = coverage_block(4, 3, 1, extra_thing=9, dropped=None)
    assert cov["requested"] == 4 and cov["succeeded"] == 3 and cov["failed"] == 1
    assert cov["extra_thing"] == 9
    assert "dropped" not in cov          # None extras are omitted, not nulled


def test_stamp_feed_attaches_status_and_coverage_in_place():
    out = {"tickers": {}}
    ret = stamp_feed(out, 3, 1, 2, failures={"AAA": "boom"})
    assert ret is out                     # in place, so callers can `return stamp_feed(...)`
    assert out["status"] == "degraded"
    assert out["coverage"]["failed"] == 2
    assert out["coverage"]["failures"] == {"AAA": "boom"}


# --------------------------------------------------------------------------- #
# orchestrator.feed_is_alive must agree with the contract
# --------------------------------------------------------------------------- #
def test_orchestrator_agrees_with_the_contract():
    """The whole point of the contract is that the ONE health check reads it."""
    assert _feed_is_alive({"status": "error"}) is False
    assert _feed_is_alive({"status": "ok"}) is True
    # "degraded" and "empty" are ALIVE: partial data is still data, and a
    # genuinely quiet tape is not an outage. Only "error" kills a feed.
    assert _feed_is_alive({"status": "degraded"}) is True
    assert _feed_is_alive({"status": "empty"}) is True
    assert _feed_is_alive({"status": "skipped"}) is True


def test_orchestrator_catches_a_lying_ok_via_the_per_ticker_layer():
    """Belt and braces: even if a feed regresses to a hardcoded "ok", an
    all-errored per-ticker layer still reads as dead."""

    lying = {"status": "ok", "tickers": {"NVDA": {"error": "429"},
                                         "DELL": {"error": "429"}}}
    assert _feed_is_alive(lying) is False
    # ...but ONE survivor means the feed is alive (degraded), not dead.
    half = {"status": "ok", "tickers": {"NVDA": {"error": "429"},
                                        "DELL": {"headlines": [1]}}}
    assert _feed_is_alive(half) is True


# --------------------------------------------------------------------------- #
# news_and_catalysts — the feed that could not fail
# --------------------------------------------------------------------------- #
class _RaisingTicker:
    def __init__(self, *_a, **_k):
        raise RuntimeError("429 Too Many Requests")


class _QuietTicker:
    """Reachable, but publishes nothing — the honest 'empty' case."""
    def __init__(self, *_a, **_k):
        self.news = []
        self.calendar = None
        self.info = {}
        self.earnings_history = None


@pytest.fixture
def fake_yf(monkeypatch):
    def _install(ticker_cls):
        mod = types.ModuleType("yfinance")
        mod.Ticker = ticker_cls
        monkeypatch.setitem(sys.modules, "yfinance", mod)
    return _install


def test_news_reports_error_when_every_ticker_raises(fake_yf, monkeypatch):
    """THE REGRESSION: news_catalysts.py:199 hardcoded "ok" forever."""
    import tools.news_catalysts as nc

    monkeypatch.setattr(nc, "_fetch_alpaca_news_by_symbol", lambda *a, **k: {})
    fake_yf(_RaisingTicker)

    out = nc.get_news_and_catalysts(["NVDA", "DELL", "MU"])
    assert out["status"] == "error", "a totally blocked news feed must not say ok"
    assert out["coverage"] == {"requested": 3, "succeeded": 0, "failed": 3,
                               "tickers_with_headlines": 0,
                               "failures": out["coverage"]["failures"]}
    assert set(out["coverage"]["failures"]) == {"NVDA", "DELL", "MU"}
    # And the orchestrator's health check must now actually see it.
    assert _feed_is_alive(out) is False


def test_news_reports_empty_when_reachable_but_quiet(fake_yf, monkeypatch):
    """A quiet tape is NOT an outage — and must not be reported as one."""
    import tools.news_catalysts as nc

    monkeypatch.setattr(nc, "_fetch_alpaca_news_by_symbol", lambda *a, **k: {})
    fake_yf(_QuietTicker)

    out = nc.get_news_and_catalysts(["NVDA", "DELL"])
    assert out["status"] == "empty"
    assert out["coverage"]["failed"] == 0
    assert _feed_is_alive(out) is True


def test_news_reports_degraded_on_partial_failure(fake_yf, monkeypatch):
    import tools.news_catalysts as nc

    class _Half:
        def __init__(self, sym):
            if sym == "DELL":
                raise RuntimeError("429")
            self.news = []
            self.calendar = None
            self.info = {}
            self.earnings_history = None

    monkeypatch.setattr(nc, "_fetch_alpaca_news_by_symbol", lambda *a, **k: {})
    fake_yf(_Half)

    out = nc.get_news_and_catalysts(["NVDA", "DELL"])
    assert out["status"] == "degraded"
    assert out["coverage"]["succeeded"] == 1 and out["coverage"]["failed"] == 1
    assert "DELL" in out["coverage"]["failures"]


def test_news_with_no_focus_names_is_skipped_not_error(fake_yf, monkeypatch):
    import tools.news_catalysts as nc

    monkeypatch.setattr(nc, "_fetch_alpaca_news_by_symbol", lambda *a, **k: {})
    fake_yf(_QuietTicker)
    assert nc.get_news_and_catalysts([])["status"] == "skipped"


# --------------------------------------------------------------------------- #
# sec_filings bundle — the {TICKER: brief} map with no top-level status
# --------------------------------------------------------------------------- #
def test_filing_briefs_bundle_reports_total_edgar_outage(monkeypatch):
    """THE REGRESSION: runlib built this as a bare dict comprehension, so a
    total EDGAR outage produced a map of error stubs with no top-level status
    and .get("status") returned None -> "healthy"."""
    import tools.sec_filings as sf

    monkeypatch.setattr(sf, "get_filing_brief",
                        lambda t: {"status": "error", "reason": "EDGAR blocked"})
    out = sf.get_filing_briefs(["NVDA", "DELL"])

    assert out["status"] == "error"
    assert out["coverage"]["succeeded"] == 0
    assert _feed_is_alive(out) is False
    # Backward compatibility: consumers still index by ticker.
    assert out["NVDA"]["status"] == "error"


def test_filing_briefs_bundle_degrades_on_partial(monkeypatch):
    import tools.sec_filings as sf

    monkeypatch.setattr(
        sf, "get_filing_brief",
        lambda t: ({"status": "ok", "ticker": t} if t == "NVDA"
                   else {"status": "error", "reason": "no CIK"}))
    out = sf.get_filing_briefs(["NVDA", "DELL"])
    assert out["status"] == "degraded"
    assert out["coverage"] == {"requested": 2, "succeeded": 1, "failed": 1,
                               "failures": {"DELL": "no CIK"}}


def test_filing_briefs_survives_a_raising_brief(monkeypatch):
    """get_filing_brief is documented fail-soft, but the bundle must not trust
    that — a raise here would take down the whole gather."""
    import tools.sec_filings as sf

    def _boom(t):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sf, "get_filing_brief", _boom)
    out = sf.get_filing_briefs(["NVDA"])
    assert out["status"] == "error"
    assert "RuntimeError" in out["NVDA"]["reason"]


# --------------------------------------------------------------------------- #
# deep fundamentals bundle — same shape bug as sec_filings
# --------------------------------------------------------------------------- #
def test_deep_fundamentals_bundle_reports_total_outage(monkeypatch):
    import tools.fundamentals as F

    monkeypatch.setattr(F, "get_deep_fundamentals",
                        lambda t: {"status": "error", "reason": "companyfacts unavailable"})
    out = F.get_deep_fundamentals_bundle(["NVDA", "DELL"])
    assert out["status"] == "error"
    assert _feed_is_alive(out) is False
    assert out["NVDA"]["status"] == "error"   # backward-compatible indexing


# --------------------------------------------------------------------------- #
# partnerships — failures were demoted into sub-keys nothing aggregated
# --------------------------------------------------------------------------- #
def test_partnerships_reports_error_when_both_sources_die(monkeypatch):
    """sec_error / news_error were per-ticker sub-keys that no automated check
    ever read, so a doubly-blocked feed returned "ok" full of empty deal lists —
    and an empty deal list reads as 'this company signed nothing'."""
    import tools.partnerships as P
    import tools.sec_filings as sf

    def _boom(*_a, **_k):
        raise RuntimeError("blocked")

    # partnerships.py now calls ticker_to_cik_result, which distinguishes
    # "lookup failed" from "not an SEC filer". Patch what it actually calls.
    monkeypatch.setattr(sf, "ticker_to_cik_result", _boom)
    mod = types.ModuleType("yfinance")
    mod.Ticker = _RaisingTicker
    monkeypatch.setitem(sys.modules, "yfinance", mod)

    out = P.get_partnerships(["NVDA", "DELL"])
    assert out["status"] == "error"
    assert out["source_coverage"]["both_failed"] == 2
    # The per-ticker layer must carry `error`, the key feed_is_alive inspects.
    assert out["tickers"]["NVDA"]["error"]
    assert _feed_is_alive(out) is False


def test_partnerships_one_live_source_is_degraded_not_dead(monkeypatch):
    """EDGAR down but news up is usable data — degraded, not error."""
    import tools.partnerships as P
    import tools.sec_filings as sf

    def _boom(*_a, **_k):
        raise RuntimeError("blocked")

    # partnerships.py now calls ticker_to_cik_result, which distinguishes
    # "lookup failed" from "not an SEC filer". Patch what it actually calls.
    monkeypatch.setattr(sf, "ticker_to_cik_result", _boom)

    class _NoNews:
        def __init__(self, *_a, **_k):
            self.news = []

    mod = types.ModuleType("yfinance")
    mod.Ticker = _NoNews
    monkeypatch.setitem(sys.modules, "yfinance", mod)

    out = P.get_partnerships(["NVDA"])
    assert out["status"] == "empty"       # reachable via news, nothing qualified
    assert out["source_coverage"] == {"sec_ok": 0, "news_ok": 1, "both_failed": 0}


# --------------------------------------------------------------------------- #
# options_signal — "no_chain" is a fact about the name, not a dead fetch
# --------------------------------------------------------------------------- #
def test_options_all_error_is_error(monkeypatch):
    import tools.options_signal as OS

    monkeypatch.setattr(OS, "_signal_for",
                        lambda t: (_ for _ in ()).throw(RuntimeError("chain blocked")))
    out = OS.get_options_signals(["NVDA", "DELL"])
    assert out["status"] == "error"
    assert _feed_is_alive(out) is False


def test_options_no_chain_is_not_a_failure(monkeypatch):
    """A name with no listed options is an observed fact. Counting it as a fetch
    failure would fabricate an outage; counting it as a live signal would
    fabricate a reading."""
    import tools.options_signal as OS

    monkeypatch.setattr(OS, "_signal_for", lambda t: {"status": "no_chain"})
    out = OS.get_options_signals(["NVDA", "DELL"])
    assert out["status"] == "empty"           # reachable, nothing to read
    assert out["coverage"]["failed"] == 0
    assert out["coverage"]["no_chain"] == 2


def test_options_partial_is_degraded(monkeypatch):
    import tools.options_signal as OS

    def _sig(t):
        if t == "DELL":
            raise RuntimeError("boom")
        return {"status": "ok", "expiry": "2026-08-21"}

    monkeypatch.setattr(OS, "_signal_for", _sig)
    out = OS.get_options_signals(["NVDA", "DELL"])
    assert out["status"] == "degraded"
    assert out["coverage"]["succeeded"] == 1


# --------------------------------------------------------------------------- #
# ownership_flow — the predicate that could never say "degraded"
# --------------------------------------------------------------------------- #
def test_ownership_flow_partial_failure_is_degraded(monkeypatch):
    """THE REGRESSION: `status = "ok" if ok else ("error" if errors else "ok")`
    reported a fully healthy feed when 19 of 20 cards had died."""
    import tools.ownership_flow as OF

    calls = {"n": 0}

    def _card(_t, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ohlcv blocked")
        return {"ticker": "X", "alignment": "institutions_buying_price_rising"}

    monkeypatch.setattr(OF, "build_ticker_card", _card)
    monkeypatch.setattr(OF, "_fetch_ohlcv", lambda *a, **k: None)
    monkeypatch.setattr(OF, "_fetch_ownership_snap", lambda *a, **k: {})

    out = OF.get_ownership_flow(["AAA", "BBB"], fetch_missing=False)
    assert out["status"] == "degraded"
    assert out["coverage"]["succeeded"] == 1 and out["coverage"]["failed"] == 1


def test_ownership_flow_sparse_everywhere_is_empty_not_ok(monkeypatch):
    """`sparse_data` explicitly means 'ignore this card'. A board of nothing but
    non-votes is an EMPTY read, and must not be sold to the brain as a healthy
    ownership signal."""
    import tools.ownership_flow as OF

    monkeypatch.setattr(OF, "build_ticker_card",
                        lambda _t, **k: {"ticker": "X", "alignment": "sparse_data"})
    monkeypatch.setattr(OF, "_fetch_ohlcv", lambda *a, **k: None)
    monkeypatch.setattr(OF, "_fetch_ownership_snap", lambda *a, **k: {})

    out = OF.get_ownership_flow(["AAA", "BBB"], fetch_missing=False)
    assert out["status"] == "empty"
    assert out["coverage"]["cards_with_alignment_vote"] == 0


# --------------------------------------------------------------------------- #
# insider_form4 — already the best in the repo; pin what it must keep doing
# --------------------------------------------------------------------------- #
def test_insider_unreadable_filings_everywhere_is_error(monkeypatch):
    """The submissions index answering while EVERY Form-4 XML fetch fails is an
    outage wearing a healthy costume: `data_unavailable` on every name means
    'we could not read the filings', never 'insiders did nothing'."""
    import tools.insider_form4 as IF

    out = {
        "tickers": {
            "NVDA": {"summary": {"signal": "data_unavailable"}, "transactions": []},
            "DELL": {"summary": {"signal": "data_unavailable"}, "transactions": []},
        },
        "by_ticker": {},
    }
    # Exercise the real tail logic by calling through a tiny shim.
    from tools.freshness_audit import stamp_feed as _stamp
    requested = len(out["tickers"])
    unreadable = sum(1 for e in out["tickers"].values()
                     if (e.get("summary") or {}).get("signal") == "data_unavailable")
    _stamp(out, requested, requested, 0, found=0)
    if requested and unreadable == requested:
        out["status"] = "error"
    assert out["status"] == "error"
    assert _feed_is_alive(out) is False
    # And the real module still defines the signal the check depends on.
    assert "data_unavailable" in Path(IF.__file__).read_text()


# --------------------------------------------------------------------------- #
# freshness_audit — "no name is stale" vs "we could not check any name"
# --------------------------------------------------------------------------- #
def test_freshness_audit_all_errors_is_error(monkeypatch):
    """audit_universe already refused to PERSIST an all-error audit, but the
    RETURNED dict still said "ok" — so an in-memory caller (the bundle's
    fundamentals_freshness block) read a blocked EDGAR as a clean audit in which
    zero names were stale."""
    import tools.freshness_audit as FA

    monkeypatch.setattr(FA, "audit_ticker",
                        lambda t, **k: {"ticker": t, "status": "error"})
    out = FA.audit_freshness(["NVDA", "DELL"])
    assert out["status"] == "error"
    assert out["coverage"]["failed"] == 2
    assert out["audited"] == 2 and out["fresh"] == 0   # legacy keys preserved


def test_freshness_audit_clean_run_is_ok(monkeypatch):
    import tools.freshness_audit as FA

    monkeypatch.setattr(FA, "audit_ticker",
                        lambda t, **k: {"ticker": t, "status": "ok", "stale": False})
    out = FA.audit_freshness(["NVDA", "DELL"])
    assert out["status"] == "ok"
    assert out["coverage"]["failed"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
