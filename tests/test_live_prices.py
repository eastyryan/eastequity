"""Offline tests for the holdings/watchlist live-price overlay — no network."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.live_prices import (
    freshness_report,
    live_age_minutes,
    overlay_live_prices,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    assert cond, f"{name}{' - ' + detail if detail else ''}"


NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def _blob(age_min: float, prices: dict) -> dict:
    return {"as_of": (NOW - timedelta(minutes=age_min)).isoformat(),
            "source": "yfinance_intraday", "prices": prices}


def test_age():
    print("live_age_minutes:")
    check("10 min old", abs(live_age_minutes(_blob(10, {}), NOW) - 10) < 0.01)
    check("no as_of -> None", live_age_minutes({"prices": {}}, NOW) is None)


def test_overlay_fresh():
    print("overlay (fresh feed):")
    scan = {"DELL": 419.21, "HPE": 48.2, "NVDA": 190.0}
    blob = _blob(5, {"DELL": 401.5, "HPE": 47.9})
    merged, applied = overlay_live_prices(scan, blob, tickers=["DELL", "HPE"],
                                          max_age_min=30, now=NOW)
    check("DELL overlaid to live 401.5 (below stop)", merged["DELL"] == 401.5)
    check("HPE overlaid", merged["HPE"] == 47.9)
    check("NVDA untouched (not requested)", merged["NVDA"] == 190.0)
    check("applied lists DELL+HPE", set(applied) == {"DELL", "HPE"})


def test_overlay_stale_not_applied():
    print("overlay (stale feed is NOT applied):")
    scan = {"DELL": 419.21}
    blob = _blob(90, {"DELL": 401.5})  # 90 min old, window 30
    merged, applied = overlay_live_prices(scan, blob, tickers=["DELL"],
                                          max_age_min=30, now=NOW)
    check("stale live NOT overlaid — keeps daily bar", merged["DELL"] == 419.21)
    check("nothing applied", applied == [])


def test_overlay_guards():
    print("overlay guards:")
    scan = {"DELL": 419.21}
    # non-positive / non-numeric live prices are ignored
    blob = _blob(1, {"DELL": 0, "HPE": "n/a"})
    merged, applied = overlay_live_prices(scan, blob, max_age_min=30, now=NOW)
    check("bad live prices ignored", merged["DELL"] == 419.21 and applied == [])
    # empty feed
    m2, a2 = overlay_live_prices(scan, {"prices": {}}, now=NOW)
    check("empty feed -> no change", m2 == scan and a2 == [])
    # None feed
    m3, a3 = overlay_live_prices(scan, None, now=NOW)
    check("None feed -> no change", m3 == scan and a3 == [])


def test_freshness_report():
    print("freshness_report:")
    fresh = freshness_report(_blob(5, {"DELL": 401.5}), ["DELL"],
                             max_age_min=30, now=NOW)
    check("fresh not stale", fresh["stale"] is False)
    check("fresh status ok", fresh["status"] == "ok")
    stale = freshness_report(_blob(120, {"DELL": 401.5}), [],
                             max_age_min=30, now=NOW)
    check("old snapshot flagged stale", stale["stale"] is True)
    check("no overlay -> status no_live_overlay", stale["status"] == "no_live_overlay")
    missing = freshness_report({"prices": {}}, [], max_age_min=30, now=NOW)
    check("missing as_of -> stale", missing["stale"] is True)


if __name__ == "__main__":
    test_age()
    test_overlay_fresh()
    test_overlay_stale_not_applied()
    test_overlay_guards()
    test_freshness_report()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        sys.exit(1)
    print("\nAll live_prices tests passed.")
