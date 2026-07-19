"""The earnings calendar must never report a dead feed as an empty market.

THE INCIDENT (2026-07-18 data-integrity audit)
----------------------------------------------
data/earnings_calendar.json read exactly this for its ENTIRE committed history:

    {"built_at": "2026-07-17T17:48...", "by_ticker": {}, "status": "empty", "n": 0}

The sweep had never once succeeded on the cloud gather node, and nothing anywhere
said so. Four independent defects conspired:

  1. every ticker was swallowed by a bare `except Exception: continue` — no
     counter, no reason, no evidence a fetch had even been attempted;
  2. `ok` counted only successes and the status was `"ok" if ok else "empty"`, so
     a 100%-FAILED sweep reported "empty" and NEVER "error";
  3. `built_at` was refreshed even when ok == 0, so a totally-failed sweep wrote
     a FRESH timestamp over a dead calendar — and the 90-minute `_cache_fresh`
     guard then suppressed the retry that could have healed it;
  4. by_ticker was seeded from the CACHED map, so stale entries persisted
     forever wearing that fresh timestamp with no way to age them out.

Consequence: `days_to_earnings` was ABSENT on every scanned row. CLAUDE.md tells
the brain to "respect the binary-print problem" and to choose the earnings path
"through binary vs around it" — an ABSENT days_to_earnings reads as "no print
imminent", the exact INVERSE of the truth. The system walked into earnings blind
while every status field it published said it was fine.

ROOT CAUSE of the fetch failure was NOT an API change: yf.Ticker(t).earnings_dates
still returns the documented DataFrame (a 182-name sweep returns 182/182 from a
residential IP). The sweep fired ~182 sequential unauthenticated Yahoo requests
with no pause from a GitHub-hosted Azure runner, which Yahoo rate-limits.

These tests are hermetic: no network, no clock.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import tools.earnings_calendar as EC  # noqa: E402

ET = "-04:00"


def _now(day: int = 17, hour: int = 12) -> datetime:
    return datetime.fromisoformat(f"2026-07-{day:02d}T{hour:02d}:00:00{ET}")


class _FakeIndexFrame:
    """Minimal stand-in for the earnings_dates DataFrame: only `.index` is read."""
    def __init__(self, stamps):
        self.index = list(stamps)

    def __len__(self):
        return len(self.index)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the module's cache at a temp file so tests never touch data/, and
    neutralize the keyless EDGAR fallback by default.

    The fallback fires whenever the primary feed failed, and it makes REAL
    network calls — which would both slow the suite and make these assertions
    depend on EDGAR's mood. Tests that want to exercise it re-patch it."""
    cache = tmp_path / "earnings_calendar.json"
    monkeypatch.setattr(EC, "_CACHE", cache)
    monkeypatch.setattr(EC, "_SWEEP_SLEEP_SEC", 0)     # keep the suite fast
    monkeypatch.setattr(EC, "_last_earnings_8k_from_edgar", lambda *a, **k: None)
    return cache


def _install_yf(monkeypatch, behavior):
    """Install a fake yfinance whose Ticker(t).earnings_dates runs `behavior(t)`."""
    import types

    import pandas as pd

    class _T:
        def __init__(self, sym):
            self._sym = sym

        @property
        def earnings_dates(self):
            return behavior(self._sym)

    mod = types.ModuleType("yfinance")
    mod.Ticker = _T
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return pd


# --------------------------------------------------------------------------- #
# status: the empty-vs-error collapse
# --------------------------------------------------------------------------- #
def test_total_failure_reports_error_not_empty(sandbox, monkeypatch):
    """THE HEADLINE REGRESSION. Every fetch raising used to yield "empty"."""
    def _boom(_sym):
        raise RuntimeError("429 Too Many Requests")

    _install_yf(monkeypatch, _boom)
    out = EC.build_earnings_calendar(["NVDA", "DELL", "MU"], now_et=_now(), force=True)

    assert out["status"] == "error", "a dead feed must never wear the 'empty' label"
    assert out["coverage"]["failed"] == 3
    assert out["coverage"]["succeeded"] == 0
    assert set(out["coverage"]["failures"]) == {"NVDA", "DELL", "MU"}
    assert "429" in out["coverage"]["failures"]["NVDA"]


def test_reachable_but_no_scheduled_prints_is_empty(sandbox, monkeypatch):
    """The genuinely empty market state must still be reachable — otherwise we
    would have replaced one lie with another."""
    _install_yf(monkeypatch, lambda _s: _FakeIndexFrame([]))
    out = EC.build_earnings_calendar(["NVDA", "DELL"], now_et=_now(), force=True)

    assert out["status"] == "empty"
    assert out["coverage"]["failed"] == 0
    assert out["coverage"]["reachable_no_dates"] == 2


def test_partial_failure_is_degraded_with_counts(sandbox, monkeypatch):
    pd = _install_yf(monkeypatch, lambda s: (
        _FakeIndexFrame([pd_ts("2026-08-26 16:00", _pd())]) if s == "NVDA"
        else (_ for _ in ()).throw(RuntimeError("429"))))
    out = EC.build_earnings_calendar(["NVDA", "DELL"], now_et=_now(), force=True)

    assert out["status"] == "degraded"
    assert out["coverage"]["succeeded"] == 1
    assert out["coverage"]["failed"] == 1
    assert "DELL" in out["coverage"]["failures"]
    assert pd is not None


def _pd():
    import pandas as pd
    return pd


def pd_ts(s, pd):
    return pd.Timestamp(s, tz="America/New_York")


# --------------------------------------------------------------------------- #
# persistence: a failed sweep must not forge freshness or destroy good data
# --------------------------------------------------------------------------- #
def test_failed_sweep_does_not_refresh_built_at(sandbox, monkeypatch):
    """Defect 3. Refreshing built_at on a failed sweep made a permanently dead
    feed look minutes-old AND suppressed the 90-minute retry."""
    old = "2026-07-10T09:00:00-04:00"
    sandbox.write_text(json.dumps({
        "built_at": old,
        "by_ticker": {"NVDA": {"last": "2026-05-20T16:00:00-04:00"}},
    }))

    def _boom(_sym):
        raise RuntimeError("blocked")

    _install_yf(monkeypatch, _boom)
    out = EC.build_earnings_calendar(["NVDA"], now_et=_now(), force=True)

    assert out["built_at"] == old, "built_at must describe the last REAL build"
    assert out["last_attempt_at"].startswith("2026-07-17")
    assert "stale_notice" in out
    assert "UNKNOWN" in out["stale_notice"]


def test_failed_sweep_does_not_overwrite_a_good_calendar_on_disk(sandbox, monkeypatch):
    """Mirrors freshness_audit.py's refusal to persist an all-error audit: an
    outage must not destroy the last-good artifact."""
    good = {"built_at": "2026-07-10T09:00:00-04:00",
            "by_ticker": {"NVDA": {"last": "2026-05-20T16:00:00-04:00"}},
            "status": "ok"}
    sandbox.write_text(json.dumps(good))

    _install_yf(monkeypatch, lambda _s: (_ for _ in ()).throw(RuntimeError("blocked")))
    out = EC.build_earnings_calendar(["NVDA"], now_et=_now(), force=True)

    assert out["status"] == "error"
    assert "artifact_error" in out
    assert json.loads(sandbox.read_text()) == good, "the good calendar survived"


def test_successful_sweep_does_refresh_and_persist(sandbox, monkeypatch):
    pd = _pd()
    _install_yf(monkeypatch, lambda _s: _FakeIndexFrame([
        pd_ts("2026-05-20 16:00", pd), pd_ts("2026-08-26 16:00", pd)]))
    out = EC.build_earnings_calendar(["NVDA"], now_et=_now(), force=True)

    assert out["status"] == "ok"
    assert out["built_at"].startswith("2026-07-17")
    assert "last_attempt_at" not in out
    on_disk = json.loads(sandbox.read_text())
    assert on_disk["by_ticker"]["NVDA"]["next"].startswith("2026-08-26")
    # Every fetched entry is stamped so it can be aged out later (defect 4).
    assert on_disk["by_ticker"]["NVDA"]["fetched_at"].startswith("2026-07-17")
    assert on_disk["by_ticker"]["NVDA"]["source"] == "yfinance"


def test_missing_yfinance_is_error_not_a_quiet_market(sandbox, monkeypatch):
    """The old code returned status "yfinance_unavailable", which is not in any
    dead-status tuple the orchestrator knows — so it read as alive."""
    import builtins

    real_import = builtins.__import__

    def _no_yf(name, *a, **k):
        if name == "yfinance":
            raise ImportError("no yfinance")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_yf)
    out = EC.build_earnings_calendar(["NVDA"], now_et=_now(), force=True)
    assert out["status"] == "error"
    assert out["coverage"]["failed"] == 1


# --------------------------------------------------------------------------- #
# per-entry aging (defect 4)
# --------------------------------------------------------------------------- #
def test_stale_entries_are_aged_out():
    now = _now()
    old = (now - timedelta(days=EC._ENTRY_MAX_AGE_DAYS + 1)).isoformat()
    fresh = (now - timedelta(days=3)).isoformat()
    kept, dropped = EC.age_out_entries({
        "OLD": {"last": "2025-01-01T00:00:00-05:00", "fetched_at": old},
        "NEW": {"last": "2026-05-20T16:00:00-04:00", "fetched_at": fresh},
    }, now)
    assert set(kept) == {"NEW"}
    assert dropped == 1


def test_legacy_entries_without_a_stamp_are_kept_once():
    """Rows written before stamping existed cannot be PROVEN stale, so we keep
    them; the next successful fetch stamps them and they age normally after."""
    kept, dropped = EC.age_out_entries({"LEGACY": {"last": "2026-05-20T16:00:00-04:00"}},
                                       _now())
    assert set(kept) == {"LEGACY"} and dropped == 0


def test_age_out_is_pure_and_survives_garbage():
    kept, dropped = EC.age_out_entries({"BAD": "not-a-dict"}, _now())
    assert kept == {} and dropped == 1
    assert EC.age_out_entries(None, _now()) == ({}, 0)


# --------------------------------------------------------------------------- #
# throttling — the actual root cause
# --------------------------------------------------------------------------- #
def test_sweep_sleeps_between_chunks(sandbox, monkeypatch):
    """The sweep fired ~182 back-to-back Yahoo requests with NO pause. The
    weekly discovery screen has always chunked and slept; this one did not."""
    slept: list = []
    monkeypatch.setattr(EC, "_SWEEP_CHUNK", 2)
    monkeypatch.setattr(EC, "_SWEEP_SLEEP_SEC", 0.001)
    monkeypatch.setattr(EC.time, "sleep", lambda s: slept.append(s))
    _install_yf(monkeypatch, lambda _s: _FakeIndexFrame([]))

    EC.build_earnings_calendar(["A", "B", "C", "D", "E"], now_et=_now(), force=True)
    assert len(slept) == 2, "expected a pause after each chunk of 2 (5 names)"


def test_sweep_aborts_after_sustained_consecutive_failures(sandbox, monkeypatch):
    """A blocked feed must not burn the gather node's 15-minute budget on 182
    calls that will all fail — and must SAY that it stopped early."""
    monkeypatch.setattr(EC, "_SWEEP_ABORT_AFTER_CONSECUTIVE_FAILURES", 3)
    monkeypatch.setattr(EC, "_SWEEP_SLEEP_SEC", 0)
    attempted: list = []

    def _boom(sym):
        attempted.append(sym)
        raise RuntimeError("429")

    _install_yf(monkeypatch, _boom)
    out = EC.build_earnings_calendar(list("ABCDEFGHIJ"), now_et=_now(), force=True)

    assert out["coverage"]["aborted_after_consecutive_failures"] is True
    assert len(attempted) == 3, "must stop, not grind through all ten"
    assert out["coverage"]["unattempted"] == 7
    assert out["status"] == "error"


# --------------------------------------------------------------------------- #
# the consumer contract: an empty reporter list off a dead calendar is a WARNING
# --------------------------------------------------------------------------- #
def test_reporters_off_a_dead_calendar_are_labeled_incomplete(monkeypatch):
    """An empty `reporters` list means 'nobody printed' when the calendar is
    healthy and 'we do not know who printed' when it is not. The orchestrator's
    earnings-escalation path must be able to tell them apart."""
    monkeypatch.setattr(EC, "build_earnings_calendar",
                        lambda *a, **k: {"status": "error", "by_ticker": {},
                                         "coverage": {"failed": 182}})
    out = EC.earnings_reporters_for_slot("morning", now_et=_now(),
                                         universe=["NVDA", "DELL"])
    assert out["reporters"] == []
    assert out["reporters_incomplete"] is True
    assert "INCOMPLETE" in out["warning"]
    assert out["calendar_status"] == "error"


def test_reporters_off_a_healthy_empty_calendar_carry_no_warning(monkeypatch):
    monkeypatch.setattr(EC, "build_earnings_calendar",
                        lambda *a, **k: {"status": "empty", "by_ticker": {},
                                         "coverage": {"failed": 0}})
    out = EC.earnings_reporters_for_slot("morning", now_et=_now(),
                                         universe=["NVDA"])
    assert out["reporters"] == []
    assert "reporters_incomplete" not in out
    assert "warning" not in out


# --------------------------------------------------------------------------- #
# the honesty note about what the EDGAR fallback can and cannot recover
# --------------------------------------------------------------------------- #
def test_next_source_limitation_is_stated_on_every_build(sandbox, monkeypatch):
    """`next` (and therefore days_to_earnings) has NO keyless source. A degraded
    sweep with a missing `next` means UNKNOWN, never 'no print scheduled' — and
    the bundle must say so rather than leave the brain to infer it."""
    _install_yf(monkeypatch, lambda _s: _FakeIndexFrame([]))
    out = EC.build_earnings_calendar(["NVDA"], now_et=_now(), force=True)
    assert "no keyless source for a FUTURE earnings date" in out["next_source_note"]


# --------------------------------------------------------------------------- #
# keyless EDGAR fallback: recovers `last` where Yahoo cannot be reached
# --------------------------------------------------------------------------- #
def test_edgar_fallback_recovers_last_when_yahoo_is_blocked(sandbox, monkeypatch):
    """Yahoo is the single point of failure for the whole calendar and is exactly
    what an Azure-hosted runner cannot reach. 8-K item 2.02 ("Results of
    Operations and Financial Condition") is the filing a company is REQUIRED to
    make when it releases earnings, and EDGAR stays reachable through
    tools/net.py's proxy fallback."""
    _install_yf(monkeypatch, lambda _s: (_ for _ in ()).throw(RuntimeError("429")))
    monkeypatch.setattr(EC, "_last_earnings_8k_from_edgar", lambda t, **k: "2026-07-16")

    out = EC.build_earnings_calendar(["NVDA"], now_et=_now(), force=True)

    assert out["coverage"]["recovered_from_edgar_8k"] == 1
    rec = out["by_ticker"]["NVDA"]
    assert rec["last"].startswith("2026-07-16")
    assert rec["source"] == "edgar_8k_item_202"
    # A filing date has no session time, so bmo/amc is NOT invented.
    assert rec["last_when"] == "unknown"
    # Recovered `last` but no `next`: degraded, and the limitation is stated.
    assert out["status"] == "degraded"
    assert "no keyless source for a FUTURE earnings date" in out["next_source_note"]


def test_edgar_fallback_is_bounded(sandbox, monkeypatch):
    """A total Yahoo outage must not turn into an unbounded EDGAR sweep on a
    15-minute gather budget."""
    monkeypatch.setattr(EC, "_EDGAR_FALLBACK_MAX_TICKERS", 3)
    monkeypatch.setattr(EC, "_SWEEP_ABORT_AFTER_CONSECUTIVE_FAILURES", 99)
    seen: list = []

    def _edgar(t, **_k):
        seen.append(t)
        return "2026-07-16"

    _install_yf(monkeypatch, lambda _s: (_ for _ in ()).throw(RuntimeError("429")))
    monkeypatch.setattr(EC, "_last_earnings_8k_from_edgar", _edgar)

    EC.build_earnings_calendar(list("ABCDEFGH"), now_et=_now(), force=True)
    assert len(seen) == 3


def test_edgar_fallback_does_not_run_on_a_healthy_sweep(sandbox, monkeypatch):
    """Zero extra EDGAR load when the primary feed is working."""
    pd = _pd()
    called: list = []
    monkeypatch.setattr(EC, "_last_earnings_8k_from_edgar",
                        lambda t, **k: called.append(t))
    _install_yf(monkeypatch, lambda _s: _FakeIndexFrame([
        pd_ts("2026-08-26 16:00", pd)]))

    out = EC.build_earnings_calendar(["NVDA"], now_et=_now(), force=True)
    assert out["status"] == "ok"
    assert called == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
