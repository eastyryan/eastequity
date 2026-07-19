"""A dead feed must never be reportable as a quiet market.

THE FAILURE CLASS
-----------------
Several helpers returned []/{} on network error, non-200 AND on a genuinely empty
result. Their callers wrapped them in try/except to detect outages — but since the
helpers never raised, that detection was dead code. Audited 2026-07-19:

  * tools/market_radar.py, under a SIMULATED TOTAL OUTAGE, reported
        coverage: {"requested": 3, "succeeded": 3, "failed": 0}
        status  : "degraded_all_feeds_empty"
    Three dead feeds counted as three successes, and `if ok_sources == 0` could
    never fire. "degraded_all_feeds_empty" is a claim about the market; the truth
    was a claim about the fetch.
  * tools/market_news.py recorded a dead Alpaca feed as {"ok": True, "items": 0},
    which also propped up any_ok, letting a total outage report status "ok".
  * sec_filings.ticker_to_cik returned None for BOTH "not an SEC filer" and "the
    lookup failed", so tools/partnerships.py counted a failed EDGAR lookup as a
    VERIFIED success and published "signed no material agreements" when the truth
    was "we never asked" — the exact failure its own docstring claims to have
    fixed. tools/filings_sweep.py silently dropped the names.

THE RULE THESE TESTS PIN
    "The feed said nothing" and "we never got an answer" are different facts, and
    the bundle must be able to tell the brain which one it has.
"""

from __future__ import annotations

import sys
import types

import pytest


# ---------------------------------------------------------------------------
# alpaca_data: the *_result helpers carry an explicit error channel.
# ---------------------------------------------------------------------------

def _fake_requests(monkeypatch, *, status=200, body=None, raises=None):
    from tools import alpaca_data

    class _Resp:
        status_code = status

        def json(self):
            return body or {}

    def _get(*_a, **_k):
        if raises:
            raise raises
        return _Resp()

    monkeypatch.setattr(alpaca_data.requests, "get", _get)
    monkeypatch.setattr(alpaca_data, "_headers", lambda: {"k": "v"})
    return alpaca_data


def test_http_error_is_an_error_not_an_empty_market(monkeypatch):
    a = _fake_requests(monkeypatch, status=500)
    for rows, err in (a.get_news_result(), a.get_movers_result(),
                      a.get_most_actives_result()):
        assert not rows
        assert err == "http_500", "a 500 must be reported as a failed call"


def test_network_exception_is_an_error(monkeypatch):
    a = _fake_requests(monkeypatch, raises=ConnectionError("egress blocked"))
    for _, err in (a.get_news_result(), a.get_movers_result(),
                   a.get_most_actives_result()):
        assert err and "ConnectionError" in err


def test_genuinely_empty_response_is_not_an_error(monkeypatch):
    """The other half of the contract: a quiet market must NOT read as an outage."""
    a = _fake_requests(monkeypatch, status=200, body={"news": [], "most_actives": []})
    rows, err = a.get_news_result()
    assert rows == [] and err is None
    rows, err = a.get_most_actives_result()
    assert rows == [] and err is None
    data, err = a.get_movers_result()
    assert data == {} and err is None


def test_missing_keys_is_an_error(monkeypatch):
    from tools import alpaca_data
    monkeypatch.setattr(alpaca_data, "_headers", lambda: None)
    assert alpaca_data.get_news_result()[1] == "no_api_keys"
    assert alpaca_data.get_movers_result()[1] == "no_api_keys"
    assert alpaca_data.get_most_actives_result()[1] == "no_api_keys"


def test_bare_helpers_keep_their_old_shape(monkeypatch):
    """Back-compat: existing callers must be unaffected by the new channel."""
    a = _fake_requests(monkeypatch, status=500)
    assert a.get_news() == []
    assert a.get_movers() == {}
    assert a.get_most_actives() == []


# ---------------------------------------------------------------------------
# market_radar: a total outage must not report succeeded=3.
# ---------------------------------------------------------------------------

def test_radar_total_outage_reports_failure_not_quiet_market(monkeypatch):
    """THE REGRESSION, reproduced: this exact scenario reported succeeded=3."""
    from tools import market_radar, alpaca_data

    monkeypatch.setattr(alpaca_data, "get_movers_result",
                        lambda **_k: ({}, "ConnectionError: egress blocked"))
    monkeypatch.setattr(alpaca_data, "get_most_actives_result",
                        lambda **_k: ([], "ConnectionError: egress blocked"))
    monkeypatch.setattr(alpaca_data, "get_news_result",
                        lambda **_k: ([], "ConnectionError: egress blocked"))

    out = market_radar.get_market_radar()
    cov = out["coverage"]
    assert cov["succeeded"] == 0, (
        f"three dead feeds reported {cov['succeeded']} successes — the outage "
        f"detector is dead code again")
    assert cov["failed"] == 3
    assert out["status"] == "error", (
        "a dead feed must not be published as 'degraded_all_feeds_empty', which "
        "asserts the market was quiet")
    assert all(v.startswith("error") for v in cov["sources"].values())


def test_radar_quiet_market_is_still_reported_as_success(monkeypatch):
    """The inverse must hold or the fix just moved the lie."""
    from tools import market_radar, alpaca_data

    monkeypatch.setattr(alpaca_data, "get_movers_result", lambda **_k: ({}, None))
    monkeypatch.setattr(alpaca_data, "get_most_actives_result", lambda **_k: ([], None))
    monkeypatch.setattr(alpaca_data, "get_news_result", lambda **_k: ([], None))

    out = market_radar.get_market_radar()
    assert out["coverage"]["succeeded"] == 3
    assert out["coverage"]["failed"] == 0
    assert out["status"] != "error"


def test_radar_partial_outage_is_disclosed(monkeypatch):
    from tools import market_radar, alpaca_data

    monkeypatch.setattr(alpaca_data, "get_movers_result",
                        lambda **_k: ({"gainers": [{"symbol": "AAA", "price": 50,
                                                    "percent_change": 9}]}, None))
    monkeypatch.setattr(alpaca_data, "get_most_actives_result",
                        lambda **_k: ([], "http_429"))
    monkeypatch.setattr(alpaca_data, "get_news_result", lambda **_k: ([], None))

    out = market_radar.get_market_radar()
    assert out["coverage"]["failed"] == 1
    assert "error" in out["coverage"]["sources"]["actives"]


# ---------------------------------------------------------------------------
# market_news: a dead Alpaca feed is not "ok with 0 items".
# ---------------------------------------------------------------------------

def test_market_news_records_dead_alpaca_feed_as_failed(monkeypatch):
    from tools import market_news, alpaca_data

    monkeypatch.setattr(alpaca_data, "has_keys", lambda: True)
    monkeypatch.setattr(alpaca_data, "get_news_result",
                        lambda **_k: ([], "ConnectionError: blocked"))
    import tools.net as net
    monkeypatch.setattr(net, "get_with_throttle_retry",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("rss down")))

    out = market_news.get_market_news()
    alpaca = [s for s in out["sources"] if s["name"] == "alpaca_news"]
    assert alpaca and alpaca[0]["ok"] is False, (
        "a dead Alpaca feed recorded as ok=True also props up any_ok, letting a "
        "total news outage report status 'ok'")
    assert out["status"] == "error"


def test_market_news_quiet_alpaca_feed_still_counts_as_ok(monkeypatch):
    from tools import market_news, alpaca_data

    monkeypatch.setattr(alpaca_data, "has_keys", lambda: True)
    monkeypatch.setattr(alpaca_data, "get_news_result", lambda **_k: ([], None))
    import tools.net as net
    monkeypatch.setattr(net, "get_with_throttle_retry",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("rss down")))

    out = market_news.get_market_news()
    alpaca = [s for s in out["sources"] if s["name"] == "alpaca_news"][0]
    assert alpaca["ok"] is True and alpaca["items"] == 0


# ---------------------------------------------------------------------------
# ticker_to_cik: "we could not look it up" != "it is not there".
# ---------------------------------------------------------------------------

def test_cik_lookup_distinguishes_failure_from_absence(monkeypatch, tmp_path):
    import tools.sec_filings as sf

    monkeypatch.setattr(sf, "CACHE", tmp_path)
    monkeypatch.setattr(sf, "_get", lambda _url: {
        "0": {"ticker": "NVDA", "cik_str": 1045810}})

    cik, why = sf.ticker_to_cik_result("NVDA")
    assert cik == "0001045810" and why == "ok"

    cik, why = sf.ticker_to_cik_result("NOTREAL")
    assert cik is None and why == "not_listed"

    def _boom(_url):
        raise ConnectionError("sec.gov blocked")

    monkeypatch.setattr(sf, "_get", _boom)
    monkeypatch.setattr(sf, "CACHE", tmp_path / "empty")
    cik, why = sf.ticker_to_cik_result("NVDA")
    assert cik is None and why == "lookup_failed", (
        "a blocked SEC host must not be indistinguishable from 'not an SEC filer'")


def test_bare_ticker_to_cik_keeps_its_old_shape(monkeypatch, tmp_path):
    """9 call sites still use the bare form; it must behave exactly as before."""
    import tools.sec_filings as sf
    monkeypatch.setattr(sf, "CACHE", tmp_path)
    monkeypatch.setattr(sf, "_get", lambda _url: {
        "0": {"ticker": "NVDA", "cik_str": 1045810}})
    assert sf.ticker_to_cik("NVDA") == "0001045810"
    assert sf.ticker_to_cik("NOTREAL") is None


# ---------------------------------------------------------------------------
# partnerships: a failed lookup is not a verified "signed nothing".
# ---------------------------------------------------------------------------

class _QuietTicker:
    def __init__(self, *_a, **_k):
        self.news = []


def test_partnerships_marks_a_failed_cik_lookup_as_an_error(monkeypatch):
    import tools.partnerships as P
    import tools.sec_filings as sf

    monkeypatch.setattr(sf, "ticker_to_cik_result",
                        lambda _t: (None, "lookup_failed"))
    mod = types.ModuleType("yfinance")
    mod.Ticker = _QuietTicker
    monkeypatch.setitem(sys.modules, "yfinance", mod)

    out = P.get_partnerships(["NVDA"])
    entry = out["tickers"]["NVDA"]
    assert entry.get("sec_error"), (
        "a failed CIK lookup was counted as a verified success, so an empty deal "
        "list read as 'this company signed nothing'")
    assert not entry.get("sec_note")


def test_partnerships_non_filer_is_a_real_answer_not_an_error(monkeypatch):
    import tools.partnerships as P
    import tools.sec_filings as sf

    monkeypatch.setattr(sf, "ticker_to_cik_result", lambda _t: (None, "not_listed"))
    mod = types.ModuleType("yfinance")
    mod.Ticker = _QuietTicker
    monkeypatch.setitem(sys.modules, "yfinance", mod)

    entry = P.get_partnerships(["NVDA"])["tickers"]["NVDA"]
    assert not entry.get("sec_error"), "'not an SEC filer' is an answer, not a failure"
    assert entry.get("sec_note")


# ---------------------------------------------------------------------------
# filings_sweep: unmapped names are UNSCANNED, not clean.
# ---------------------------------------------------------------------------

def test_filings_sweep_discloses_unmapped_tickers(monkeypatch):
    import tools.filings_sweep as FS
    import tools.sec_filings as sf

    def _lookup(t):
        return ("0001045810", "ok") if t == "NVDA" else (None, "lookup_failed")

    monkeypatch.setattr(sf, "ticker_to_cik_result", _lookup)
    import tools.net as net
    monkeypatch.setattr(net, "get_sec",
                        lambda _u: types.SimpleNamespace(text="no filings today"))

    out = FS.today_8ks(["NVDA", "DELL", "HPE"])
    cov = out["coverage"]
    assert cov["requested"] == 3 and cov["mapped"] == 1
    assert cov["lookup_failed"] == 2
    assert "UNSCANNED" in out["note_unscanned"], (
        "dropped names must be disclosed as unscanned, not silently omitted — "
        "their 8-Ks are missing, not absent")


def test_filings_sweep_total_lookup_failure_is_an_error(monkeypatch):
    import tools.filings_sweep as FS
    import tools.sec_filings as sf

    monkeypatch.setattr(sf, "ticker_to_cik_result", lambda _t: (None, "lookup_failed"))
    out = FS.today_8ks(["NVDA", "DELL"])
    assert out["status"] == "error"
    assert "lookup failures" in out["error"]
