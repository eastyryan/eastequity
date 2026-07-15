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
    check("0900 focused", slot_depth_from_hhmm("0900") == "holdings_watchlist")
    check("1600 full", slot_depth_from_hhmm("1600") == "full")
    check("0600 light", slot_depth_from_hhmm("0600") == "light")
    cfg = {"schedule": {"slot_depths": {"1000": "full"}}}
    check("config override", slot_depth_from_hhmm("1000", cfg) == "full")
    check("default keys cover weekday slots", set(DEFAULT_SLOT_DEPTHS) >= {"0900", "1000", "1200", "1400", "1600"})


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
    test_buy_policy_and_budget()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        sys.exit(1)
    print("\nAll run_depth tests passed.")
