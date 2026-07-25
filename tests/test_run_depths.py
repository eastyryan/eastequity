"""Offline tests for runlib.depths — no network."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib.depths import (
    DEFAULT_SLOT_DEPTHS,
    depth_allows_new_buys,
    focus_ticker_budget,
    resolve_depth,
    slot_depth_from_hhmm,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    # Assert so pytest actually fails on a failed check (print-only checks
    # pass silently under `pytest -q`, which is what CI runs).
    assert cond, f"{name}{' - ' + detail if detail else ''}"


def test_resolve_precedence():
    print("resolve_depth precedence:")
    check("explicit wins", resolve_depth(explicit="holdings_watchlist", light_flag=True) == "holdings_watchlist")
    check("weekly flag", resolve_depth(weekly_market=True) == "weekly_market")
    check("light flag", resolve_depth(light_flag=True) == "light")
    check("news_only", resolve_depth(news_only=True) == "evening_review")
    check("default full", resolve_depth() == "full")
    check("alias hw", resolve_depth(explicit="hw") == "holdings_watchlist")


def test_slot_map():
    print("slot_depths:")
    check("0845 focused", slot_depth_from_hhmm("0845") == "holdings_watchlist")
    check("1600 full", slot_depth_from_hhmm("1600") == "full")
    check("0600 light", slot_depth_from_hhmm("0600") == "light")
    cfg = {"schedule": {"slot_depths": {"1030": "full"}}}
    check("config override", slot_depth_from_hhmm("1030", cfg) == "full")
    check("default keys cover weekday slots", set(DEFAULT_SLOT_DEPTHS) >= {"0845", "1030", "1200", "1400", "1600"})


def test_nearest_slot_matching():
    """Scheduled runs land minutes past the slot: nearest-match within tolerance."""
    print("nearest-slot matching:")
    check("0607 routine lag -> light", slot_depth_from_hhmm("0607") == "light")
    check("0912 -> holdings_watchlist", slot_depth_from_hhmm("0912") == "holdings_watchlist")
    check("1110 between hw slots -> hw", slot_depth_from_hhmm("1110") == "holdings_watchlist")
    check("1540 pre-4pm gather -> full", slot_depth_from_hhmm("1540") == "full")
    check("1610 -> full", slot_depth_from_hhmm("1610") == "full")
    check("1740 -> evening_review", slot_depth_from_hhmm("1740") == "evening_review")
    check("0007 midnight outside tolerance -> full", slot_depth_from_hhmm("0007") == "full")
    check("2340 late night -> full", slot_depth_from_hhmm("2340") == "full")
    check("garbage hhmm -> full", slot_depth_from_hhmm("noon") == "full")
    noisy = {"schedule": {"slot_depths": {"0845": "holdings_watchlist",
                                          "_note": "annotation", "bad": "light",
                                          "1300": "not_a_depth"}}}
    check("_note/malformed keys ignored", slot_depth_from_hhmm("0905", noisy) == "holdings_watchlist")
    check("unknown depth value ignored", slot_depth_from_hhmm("1305", noisy) == "full")
    check("resolve hhmm routes slot map", resolve_depth(hhmm="0607") == "light")
    check("news_only beats slot map", resolve_depth(news_only=True, hhmm="1005") == "evening_review")
    check("explicit beats slot map", resolve_depth(explicit="full", hhmm="0607") == "full")


def test_buy_policy_and_budget():
    print("buy policy + budgets:")
    check("hw allows buys", depth_allows_new_buys("holdings_watchlist"))
    check("full allows buys", depth_allows_new_buys("full"))
    check("light blocks buys", not depth_allows_new_buys("light"))
    check("weekly blocks buys", not depth_allows_new_buys("weekly_market"))
    b = focus_ticker_budget("holdings_watchlist")
    check("hw no full scan", b["full_universe_scan"] is False)
    check("hw focus_only scan", b["scan_mode"] == "focus_only")
    check("hw still runs 8-K sweep", b["filings_sweep"] is True)
    check("hw tape promote cap", int(b.get("tape_promote_max") or 0) >= 1)
    bf = focus_ticker_budget("full")
    check("full promotes", bf["promote_extra"] == 2)
    check("full scans universe", bf["full_universe_scan"] is True)


if __name__ == "__main__":
    test_resolve_precedence()
    test_slot_map()
    test_nearest_slot_matching()
    test_buy_policy_and_budget()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        sys.exit(1)
    print("\nAll run_depth tests passed.")
