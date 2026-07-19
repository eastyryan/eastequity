"""universe_scanner: point-in-time integrity, rate-limit discipline, coverage.

THE INCIDENTS (2026-07-18 data-integrity audit)
----------------------------------------------
1. DIVIDE-BY-ONE (universe_scanner.py:696). vol_surge was

       float(vol.iloc[-5:].mean()) / max(float(vol.iloc[-65:-5].mean()), 1)

   The max(..., 1) floor existed to dodge ZeroDivisionError, but it turned
   ABSENT volume history into a denominator of ONE SHARE, producing an
   astronomical fake surge — and vol_surge > 1.3 SCORES +0.5 on
   swing_setup_score. A data hole promoted a name up the ranking on the strength
   of its own absence.

2. STILL-FORMING BARS. discovery_screen (the WEEKLY job) drops the partial bar
   and advertises it at :21. This scanner (the 4x/DAY job) had no equivalent, so
   an intraday run divided HALF a session's volume by full-day history —
   systematically understating vol_surge and mislabeling real breakouts as
   `unconfirmed_breakout_suspect`, which CLAUDE.md tells the brain "fails far
   more often" and to reject.

3. RATE LIMITING ON THE WRONG JOB. discovery_screen chunks and sleeps
   (CHUNK_SIZE=100, CHUNK_SLEEP_SEC=1.25). This scanner had NO sleep, retry or
   chunk anywhere: two unbounded full-universe yf.download calls plus ~120
   back-to-back scraped requests in the enrich loop.

4. UNDISCLOSED ENRICH DEGRADATION. The bulk pass disclosed requested /
   scan_failures; the ENRICH pass had no counters at all. A fully rate-limited
   enrich produced rows with no market_cap_usd (the $1B floor's input) and no
   days_to_earnings (the binary-print discipline) while the scan still reported
   "status": "ok".

5. RETROACTIVE RESTATEMENT. auto_adjust=True at 15 call sites, disclosed
   nowhere: the 200-week MA the deep_value_200w lane computes is NOT the level
   the crowd watches on an unadjusted chart.

Hermetic: pure helpers and a fake yfinance. No network.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import tools.universe_scanner as US  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. the still-forming-bar guard
# --------------------------------------------------------------------------- #
def test_todays_bar_before_the_close_is_partial():
    # 12:00 ET on the bar's own date -> still forming.
    assert US._last_bar_is_partial("2026-07-17", "2026-07-17", 12 * 60) is True


def test_todays_bar_after_the_close_is_complete():
    assert US._last_bar_is_partial("2026-07-17", "2026-07-17", 16 * 60) is False
    assert US._last_bar_is_partial("2026-07-17", "2026-07-17", 20 * 60) is False


def test_a_prior_session_bar_is_never_partial():
    assert US._last_bar_is_partial("2026-07-16", "2026-07-17", 10 * 60) is False


def test_missing_bar_date_is_not_partial():
    """Fail-soft: an unknown date must not silently delete the newest bar."""
    assert US._last_bar_is_partial("", "2026-07-17", 10 * 60) is False


def test_discovery_screen_and_scanner_share_one_definition():
    """The two screens must never drift apart on what 'a completed session' is —
    discovery_screen now imports these instead of keeping a private copy."""
    import tools.discovery_screen as DS

    assert DS._last_bar_is_partial is US._last_bar_is_partial
    assert DS._completed_series is US._completed_series


def test_completed_series_drops_only_the_forming_bar():
    import pandas as pd

    idx = pd.to_datetime(["2026-07-15", "2026-07-16", "2026-07-17"])
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=idx)
    midday = datetime(2026, 7, 17, 12, 0)

    trimmed = US._completed_series(df, midday)
    assert len(trimmed) == 2 and float(trimmed["Close"].iloc[-1]) == 2.0

    after_close = datetime(2026, 7, 17, 16, 30)
    assert len(US._completed_series(df, after_close)) == 3


def test_completed_series_is_fail_soft():
    """On any surprise, return the frame unchanged — a missing row is worse than
    a partial one."""
    assert US._completed_series("not a frame", datetime(2026, 7, 17, 12)) == "not a frame"


# --------------------------------------------------------------------------- #
# 2. chunking helper
# --------------------------------------------------------------------------- #
def test_chunks_partitions_without_loss():
    got = US._chunks(list(range(7)), 3)
    assert got == [[0, 1, 2], [3, 4, 5], [6]]
    assert [x for c in got for x in c] == list(range(7))
    assert US._chunks([], 3) == []


# --------------------------------------------------------------------------- #
# a fake yfinance so scan_universe runs offline
# --------------------------------------------------------------------------- #
def _frame(n, *, vol=1_000_000.0, last_vol=None, tail_zero_vols=0):
    """n ascending daily bars ending 2026-07-17."""
    import pandas as pd

    idx = pd.bdate_range(end="2026-07-17", periods=n)
    closes = [100.0 + i * 0.1 for i in range(n)]
    vols = [vol] * n
    if tail_zero_vols:
        # Wipe the BASELINE window (-65:-5) — the exact hole the old
        # max(denominator, 1) floor converted into a fake surge.
        for i in range(max(0, n - 65), n - 5):
            vols[i] = 0.0
    if last_vol is not None:
        for i in range(n - 5, n):
            vols[i] = last_vol
    return pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes],
         "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": vols},
        index=idx)


@pytest.fixture
def fake_market(monkeypatch):
    """Install a fake yfinance + neutralize sleeps. Returns a recorder dict."""
    rec = {"download_calls": [], "sleeps": []}

    def _install(frames_by_ticker, *, fail_chunks=()):
        import pandas as pd

        def _download(tickers, **kwargs):
            names = tickers if isinstance(tickers, list) else [tickers]
            rec["download_calls"].append(list(names))
            if len(rec["download_calls"]) in fail_chunks:
                raise RuntimeError("429 Too Many Requests")
            cols, data = [], {}
            for t in names:
                df = frames_by_ticker.get(t)
                if df is None:
                    continue
                for c in df.columns:
                    cols.append((t, c))
                    data[(t, c)] = df[c]
            if not cols:
                return pd.DataFrame()
            out = pd.DataFrame(data)
            out.columns = pd.MultiIndex.from_tuples(cols)
            return out

        mod = types.ModuleType("yfinance")
        mod.download = _download
        mod.Ticker = lambda t: (_ for _ in ()).throw(RuntimeError("enrich blocked"))
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        monkeypatch.setattr(US.time, "sleep", lambda s: rec["sleeps"].append(s))
        monkeypatch.setattr(US, "load_universe",
                            lambda: {"test": [t for t in frames_by_ticker
                                              if t != "SPY"]})
        return rec

    return _install


# --------------------------------------------------------------------------- #
# 3. the divide-by-one bug
# --------------------------------------------------------------------------- #
def test_absent_volume_history_yields_none_not_an_astronomical_surge(fake_market):
    """THE REGRESSION. A zero baseline used to divide by ONE SHARE, minting a
    millions-fold 'surge' that scored +0.5 and floated a data hole to the top."""
    frames = {"AAA": _frame(300, tail_zero_vols=1, last_vol=5_000_000.0),
              "SPY": _frame(300)}
    fake_market(frames)

    out = US.scan_universe(top_n=5, tickers=["AAA"], enrich=False)
    row = next(r for r in out["top_setups"] if r["ticker"] == "AAA")

    assert row["volume_surge_5d_vs_3m"] is None, \
        "no usable baseline must render as None, never as a fabricated surge"
    # ...and the score must not have been paid for the absence.
    assert row["swing_setup_score"] < 90


def test_a_real_surge_still_computes(fake_market):
    """The guard must not have blinded the signal it protects."""
    frames = {"AAA": _frame(300, vol=1_000_000.0, last_vol=3_000_000.0),
              "SPY": _frame(300)}
    fake_market(frames)
    out = US.scan_universe(top_n=5, tickers=["AAA"], enrich=False)
    row = out["top_setups"][0]
    assert row["volume_surge_5d_vs_3m"] == pytest.approx(3.0, abs=0.05)


# --------------------------------------------------------------------------- #
# 4. rate-limit discipline
# --------------------------------------------------------------------------- #
def test_downloads_are_chunked_and_slept_between(fake_market, monkeypatch):
    """Ported from discovery_screen.py:53-56. Before this, the 4x/day scanner
    issued two unbounded full-universe requests with no pause at all."""
    monkeypatch.setattr(US, "CHUNK_SIZE", 2)
    names = ["A", "B", "C", "D"]
    frames = {t: _frame(300) for t in names + ["SPY"]}
    rec = fake_market(frames)

    US.scan_universe(top_n=5, tickers=names, enrich=False)

    # Daily pass: 5 symbols (4 + SPY) at 2/chunk = 3 requests; weekly pass: 4
    # symbols = 2 requests. Nothing is a single unbounded call any more.
    assert all(len(c) <= 2 for c in rec["download_calls"])
    assert len(rec["download_calls"]) >= 4
    assert rec["sleeps"], "must pause between chunks"


def test_one_dead_chunk_no_longer_kills_the_whole_scan(fake_market, monkeypatch):
    """A single raising yf.download used to take the ENTIRE scan down. Now the
    surviving chunks are kept and the failure is COUNTED."""
    monkeypatch.setattr(US, "CHUNK_SIZE", 2)
    names = ["A", "B", "C", "D"]
    frames = {t: _frame(300) for t in names + ["SPY"]}
    fake_market(frames, fail_chunks=(1,))       # kill the first daily chunk

    out = US.scan_universe(top_n=10, tickers=names, enrich=False)
    assert out["scan_coverage"]["daily_chunk_failures"] == 1
    assert out["scanned"] < out["requested"]
    assert out["status"] == "degraded", "partial coverage must not report ok"


def test_total_download_failure_is_error_not_ok(fake_market, monkeypatch):
    """THE REGRESSION: `"status": "ok"` was hardcoded, so a scan that produced
    ZERO rows still advertised health."""
    monkeypatch.setattr(US, "CHUNK_SIZE", 10)
    names = ["A", "B"]
    frames = {t: _frame(300) for t in names + ["SPY"]}
    fake_market(frames, fail_chunks=(1, 2, 3, 4))

    out = US.scan_universe(top_n=5, tickers=names, enrich=False)
    assert out["status"] == "error"
    assert out["scanned"] == 0
    assert "UNSCANNED, not absent" in out["error"]


# --------------------------------------------------------------------------- #
# 5. coverage keys the orchestrator depends on
# --------------------------------------------------------------------------- #
def test_scanned_and_requested_are_always_emitted(fake_market):
    """orchestrator reads scan_ctx.get("scanned", 0) and compares it against
    `requested`. A missing `scanned` would silently compare a default of 0 and
    mark EVERY run partial."""
    frames = {"A": _frame(300), "SPY": _frame(300)}
    fake_market(frames)
    out = US.scan_universe(top_n=5, tickers=["A"], enrich=False)

    assert isinstance(out["scanned"], int)
    assert isinstance(out["requested"], int)
    assert out["scanned"] == 1 and out["requested"] == 1
    assert out["scan_failures"] == 0
    # ...and the low-coverage predicate the orchestrator actually runs.
    assert not (out["scanned"] < 0.8 * out["requested"])


def test_short_history_and_no_data_are_counted_separately(fake_market):
    """'Too new to score' and 'the fetch returned nothing' are different facts."""
    frames = {"A": _frame(300), "TINY": _frame(30), "SPY": _frame(300)}
    fake_market(frames)
    out = US.scan_universe(top_n=5, tickers=["A", "TINY", "GONE"], enrich=False)

    cov = out["scan_coverage"]
    assert cov["short_history"] == 1      # TINY: 30 bars < MIN_BARS
    assert cov["no_data"] == 1            # GONE: no frame at all
    assert cov["requested"] == 3 and cov["scanned"] == 1


def test_enrich_disabled_says_so_rather_than_reporting_zeros(fake_market):
    """An enrich pass that never ran must not be indistinguishable from one that
    ran and failed."""
    frames = {"A": _frame(300), "SPY": _frame(300)}
    fake_market(frames)
    out = US.scan_universe(top_n=5, tickers=["A"], enrich=False)
    assert out["enrich_coverage"] == {"skipped": True, "reason": "enrich=False"}


def test_totally_failed_enrich_is_counted_and_downgrades_the_scan(fake_market):
    """THE REGRESSION: the enrich pass had NO counters at all — the largest
    undisclosed degradation surface in the system. Every .info call failing
    means market_cap_usd is absent on every surfaced row, and missing market cap
    is MISSING DATA, not a sub-$1B name."""
    frames = {"A": _frame(300), "B": _frame(300), "SPY": _frame(300)}
    fake_market(frames)          # fake Ticker() always raises

    out = US.scan_universe(top_n=5, tickers=["A", "B"], enrich=True)
    cov = out["enrich_coverage"]

    assert cov["attempted"] == 2
    assert cov["info_failed"] == 2 and cov["info_ok"] == 0
    assert out["status"] == "degraded"
    assert "NOT a sub-$1B name" in out["enrich_error"]
    # The rows themselves must carry the failure, not merely lack a field.
    assert all(r.get("enrich_error") == "info_unavailable" for r in out["top_setups"])


def test_enrich_aborts_after_sustained_consecutive_failures(fake_market, monkeypatch):
    """A blocked scraper must not burn the gather node's budget name by name."""
    monkeypatch.setattr(US, "ENRICH_ABORT_AFTER_CONSECUTIVE_FAILURES", 2)
    names = [f"T{i}" for i in range(8)]
    frames = {t: _frame(300) for t in names + ["SPY"]}
    fake_market(frames)

    out = US.scan_universe(top_n=8, tickers=names, enrich=True)
    cov = out["enrich_coverage"]
    assert cov["aborted_after_consecutive_failures"] is True
    assert cov["skipped_after_abort"] > 0
    assert cov["attempted"] < len(names)


# --------------------------------------------------------------------------- #
# 6. point-in-time disclosure
# --------------------------------------------------------------------------- #
def test_adjusted_price_basis_is_disclosed_on_rows_and_scan(fake_market):
    """auto_adjust=True at 15 call sites was disclosed NOWHERE. Adjusted history
    is restated on every corporate action, so wma_200w is not the level a retail
    chart shows — and the deep_value_200w lane is entirely about that level."""
    frames = {"A": _frame(300), "SPY": _frame(300)}
    fake_market(frames)
    out = US.scan_universe(top_n=5, tickers=["A"], enrich=False)

    assert out["price_basis"] == "split_dividend_adjusted"
    assert "wma_200w" in out["price_basis_note"]
    assert "unadjusted" in out["price_basis_note"]
    assert out["top_setups"][0]["price_basis"] == "split_dividend_adjusted"
    # And the still-forming-bar policy is stated, since it changes last_close.
    assert "DROPPED" in out["bar_completeness_note"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
