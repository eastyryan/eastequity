"""Formal tests for tools.watchlist_triggers (symmetric +-2% band).

Covers the 2026-07-14 AMD false-positive regression (price ~3.9% below $555
must NOT fire) and the parse_price_level lowest-of-range rule.

Runnable as a script AND under pytest:

    python3 tests/test_watchlist_triggers.py
    python3 -m pytest tests/test_watchlist_triggers.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.watchlist_triggers import (  # noqa: E402
    TRIGGER_TOLERANCE_PCT,
    check_watchlist_triggers,
    parse_price_level,
)

FAILURES: list[str] = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _alert_tickers(alerts):
    return {a.get("ticker") for a in alerts}


def test_price_within_2pct_fires():
    print("price within 2% of level fires:")
    # $505 is +1.0% from $500 — inside the band
    wl = [{"ticker": "AMD", "would_buy_at": "near $500"}]
    alerts = check_watchlist_triggers(wl, {"AMD": 505.0})
    check("fires for +1% above level", len(alerts) == 1, str(alerts))
    check("ticker is AMD", _alert_tickers(alerts) == {"AMD"}, str(alerts))
    check("parsed_level is 500",
          alerts and alerts[0].get("parsed_level") == 500.0, str(alerts))
    # exactly at level
    alerts = check_watchlist_triggers(wl, {"AMD": 500.0})
    check("fires when price equals level", len(alerts) == 1, str(alerts))
    # just inside band on the downside (~1.8%): avoid float edge at exactly 2%
    alerts = check_watchlist_triggers(wl, {"AMD": 491.0})
    check("fires near -2% (inside band)", len(alerts) == 1, str(alerts))
    # just inside band on the upside (~1.8%)
    alerts = check_watchlist_triggers(wl, {"AMD": 509.0})
    check("fires near +2% (inside band)", len(alerts) == 1, str(alerts))
    # just outside band must not fire
    alerts = check_watchlist_triggers(wl, {"AMD": 489.0})  # -2.2%
    check("does not fire just outside -2%", alerts == [], str(alerts))
    alerts = check_watchlist_triggers(wl, {"AMD": 511.0})  # +2.2%
    check("does not fire just outside +2%", alerts == [], str(alerts))


def test_amd_style_3_9pct_below_does_not_fire():
    print("AMD-style 3.9% below $555 does NOT fire (2026-07-14 regression):")
    # 555 * (1 - 0.039) = 533.355 — the false-positive case from the run notes
    level = 555.0
    price = round(level * (1 - 0.039), 2)  # 533.355 -> 533.36-ish; use 533.5 like CLI
    price = 533.5
    dist_pct = (price / level - 1) * 100
    check(f"fixture is ~3.9% below (got {dist_pct:.2f}%)",
          abs(dist_pct) > TRIGGER_TOLERANCE_PCT * 100)
    wl = [{"ticker": "AMD", "would_buy_at": "near $555"}]
    alerts = check_watchlist_triggers(wl, {"AMD": price})
    check("does not fire for 3.9% below level", alerts == [], str(alerts))
    # also the actual ~534.39 close referenced in the journal
    alerts = check_watchlist_triggers(wl, {"AMD": 534.39})
    check("does not fire for 534.39 vs 555 (~-3.7%)", alerts == [], str(alerts))


def test_far_above_does_not_fire():
    print("far above level does NOT fire:")
    wl = [{"ticker": "ANET", "would_buy_at": "below $170"}]
    alerts = check_watchlist_triggers(wl, {"ANET": 200.0})  # ~17.6% above
    check("no alert when well above buy level", alerts == [], str(alerts))
    # just outside +2%: 170 * 1.03 = 175.1
    alerts = check_watchlist_triggers(wl, {"ANET": 175.1})
    check("no alert at +3%", alerts == [], str(alerts))


def test_parse_price_level_lowest_from_range():
    print("parse_price_level picks lowest from ranges:")
    check("$500-$520 -> 500", parse_price_level("$500-$520") == 500.0)
    check("$500-$520 zone -> 500", parse_price_level("$500-$520 zone") == 500.0)
    check("near $175 or above $185 -> 175",
          parse_price_level("near $175 or above $185") == 175.0)
    check("comma thousands $1,800 -> 1800",
          parse_price_level("pullback to $1,800") == 1800.0)
    check("decimals $52.50 -> 52.5", parse_price_level("near $52.50") == 52.5)
    # range case also fires when price is near the lowest
    wl = [{"ticker": "SNDK", "would_buy_at": "$500-$520 zone"}]
    alerts = check_watchlist_triggers(wl, {"SNDK": 508.0})  # 1.6% above $500
    check("range text fires near lowest level", len(alerts) == 1, str(alerts))
    check("parsed_level is lowest (500)",
          alerts and alerts[0].get("parsed_level") == 500.0, str(alerts))


def test_non_price_text_returns_no_alert():
    print("non-price text returns no alert:")
    wl = [{"ticker": "GEV",
           "would_buy_at": "on a pullback toward the 50-day average, or after 8/4 earnings"}]
    alerts = check_watchlist_triggers(wl, {"GEV": 100.0})
    check("non-price trigger produces no alert", alerts == [], str(alerts))
    check("parse returns None",
          parse_price_level(wl[0]["would_buy_at"]) is None)


def test_empty_inputs_return_empty():
    print("empty inputs return []:")
    check("empty watchlist", check_watchlist_triggers([], {"AMD": 500.0}) == [])
    check("None-like empty prices", check_watchlist_triggers(
        [{"ticker": "AMD", "would_buy_at": "near $500"}], {}) == [])
    check("None watchlist", check_watchlist_triggers(None, {"AMD": 500.0}) == [])  # type: ignore[arg-type]
    check("None prices", check_watchlist_triggers(
        [{"ticker": "AMD", "would_buy_at": "near $500"}], None) == [])  # type: ignore[arg-type]
    check("parse None -> None", parse_price_level(None) is None)
    check("parse empty string -> None", parse_price_level("") is None)
    check("parse int -> None", parse_price_level(123) is None)  # type: ignore[arg-type]


if __name__ == "__main__":
    for fn in (test_price_within_2pct_fires,
               test_amd_style_3_9pct_below_does_not_fire,
               test_far_above_does_not_fire,
               test_parse_price_level_lowest_from_range,
               test_non_price_text_returns_no_alert,
               test_empty_inputs_return_empty):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All watchlist-trigger checks passed.")
