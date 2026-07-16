"""Calendar-day BUY cap: swing_rules.max_new_positions_per_day is enforced
against already-filled BUYs in portfolio history on today's US/Eastern date,
not only within the current proposal batch.

Runnable as a script AND under pytest:

    python3 tests/test_daily_buy_cap.py
    python3 -m pytest tests/test_daily_buy_cap.py -q
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator  # noqa: E402

CFG = validator.load_config()
MAX_PER_DAY = int(CFG["swing_rules"]["max_new_positions_per_day"])
UNIVERSE = sorted(validator.load_universe())

# Need distinct universe tickers so entry-cooldown / already-holding do not
# collide with the history we inject for the daily-cap cases.
TICKERS = [t for t in UNIVERSE if t not in ("DELL", "HPE")][:6]
if len(TICKERS) < 4:
    TICKERS = UNIVERSE[:6]

FAILURES: list[str] = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def buy(ticker, size=900.0, entry=450.0, stop=400.0, target=560.0):
    return {
        "ticker": ticker, "action": "BUY", "instrument": "EQUITY",
        "position_size_usd": size, "entry_price_max": entry, "stop_loss": stop,
        "target_price": target, "holding_horizon_days": 30, "confidence": 0.7,
        "risk_reward_ratio": 2.2,
        "thesis": "Clean swing setup with a real catalyst and rising estimates.",
        "macro_context": "Regime supports adding measured long exposure.",
        "variant_perception": "Consensus sees a fair multiple; I see mispriced growth because estimates lag; resolves at the next print.",
        "thesis_invalidators": {"invalidating_print": "EPS guide cut or estimate cuts >5%", "invalidating_structure": "Close below the 50-DMA on rising volume", "time_box": "If no progress toward thesis in 25 trading days, exit"},
        "demand_driver": "hyperscaler_server_capex",
        "catalysts": ["Earnings in ~3 weeks"],
        "risk_map": "Stop below recent structure; thesis invalid if sector leadership fails.",
        "scenarios": {"base": "Thesis plays out over the hold window.",
                      "bull": "Estimates re-accelerate and multiple expands.",
                      "bear": "Macro tightens and the name mean-reverts to support."},
    }


def reasons_of(res):
    return " | ".join(res.reasons)


def _et_today_iso_now() -> str:
    """An ISO timestamp that falls on today's ET calendar date (UTC-aware)."""
    return datetime.now(timezone.utc).isoformat()


def _et_yesterday_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def _filled_buy(ticker, when_iso, status="filled"):
    return {
        "ticker": ticker,
        "action": "BUY",
        "status": status,
        "filled_at": when_iso,
        "submitted_at": when_iso,
        "position_size_usd": 500,
    }


def _empty_portfolio(history=None):
    return {
        "total_equity_usd": 10000,
        "cash_usd": 10000,
        "positions": [],
        "history": history or [],
    }


def test_count_filled_buys_today():
    print("_count_filled_buys_today (unit):")
    today = validator._et_today()
    now = _et_today_iso_now()
    yday = _et_yesterday_iso()
    hist = [
        _filled_buy("AAA", now),
        _filled_buy("BBB", now),
        _filled_buy("CCC", yday),                      # prior day — ignore
        _filled_buy("DDD", now, status="rejected"),    # not filled — ignore
        {"ticker": "EEE", "action": "SELL_TO_CLOSE", "status": "filled",
         "filled_at": now},                            # sell — ignore
        {"ticker": "FFF", "action": "BUY", "status": "filled",
         "filled_at": "not-a-timestamp"},              # unparseable — fail open
        {"ticker": "GGG", "action": "BUY", "status": "filled"},  # no ts — fail open
    ]
    n = validator._count_filled_buys_today({"history": hist}, today_et=today)
    check("counts only two filled BUYs today", n == 2, f"got {n}")
    n0 = validator._count_filled_buys_today({"history": []}, today_et=today)
    check("empty history -> 0", n0 == 0)
    n_none = validator._count_filled_buys_today({}, today_et=today)
    check("missing history -> 0", n_none == 0)


def test_batch_only_cap_still_enforced():
    print("batch counter alone (no prior fills today):")
    # max_new_positions_per_day is 2: first two approve, third rejects.
    assert MAX_PER_DAY == 2, f"test assumes max_per_day=2, got {MAX_PER_DAY}"
    props = [buy(TICKERS[i]) for i in range(3)]
    results = validator.validate_proposals(props, _empty_portfolio(), {})
    check("first BUY approved", results[0].approved, reasons_of(results[0]))
    check("second BUY approved", results[1].approved, reasons_of(results[1]))
    check("third BUY rejected by batch cap", not results[2].approved, reasons_of(results[2]))
    check("third reason mentions max_new_positions_per_day",
          any("max_new_positions_per_day_exceeded" in r for r in results[2].reasons),
          reasons_of(results[2]))
    check("third reason shows already_0_today",
          any("already_0_today" in r for r in results[2].reasons),
          reasons_of(results[2]))


def test_prior_filled_buys_block_new_entries():
    print("already-filled BUYs today count against the cap:")
    now = _et_today_iso_now()
    # Already at the daily limit from earlier runs today.
    history = [
        _filled_buy(TICKERS[0], now),
        _filled_buy(TICKERS[1], now),
    ]
    pf = _empty_portfolio(history)
    # New ticker, clean of cooldown / already-holding.
    res = validator.validate_proposals([buy(TICKERS[2])], pf, {})[0]
    check("rejected when already at daily fill cap", not res.approved, reasons_of(res))
    check("reason is max_new_positions_per_day_exceeded",
          any("max_new_positions_per_day_exceeded" in r for r in res.reasons),
          reasons_of(res))
    check("reason reports already_2_today",
          any("already_2_today" in r for r in res.reasons), reasons_of(res))


def test_one_prior_allows_one_more():
    print("one fill today leaves room for one more BUY:")
    now = _et_today_iso_now()
    history = [_filled_buy(TICKERS[0], now)]
    pf = _empty_portfolio(history)
    props = [buy(TICKERS[2]), buy(TICKERS[3])]
    results = validator.validate_proposals(props, pf, {})
    check("first new BUY approved (already 1 + batch 1 = 2)",
          results[0].approved, reasons_of(results[0]))
    check("second new BUY rejected (already 1 + batch 2 = 3 > 2)",
          not results[1].approved, reasons_of(results[1]))
    check("rejection cites already_1_today",
          any("already_1_today" in r for r in results[1].reasons),
          reasons_of(results[1]))


def test_yesterday_fills_do_not_count():
    print("prior-day fills do not consume today's cap:")
    yday = _et_yesterday_iso()
    history = [
        _filled_buy(TICKERS[0], yday),
        _filled_buy(TICKERS[1], yday),
    ]
    pf = _empty_portfolio(history)
    # Two BUYs should both clear the daily cap (batch of 2, already 0 today).
    # Use fresh tickers so cooldown does not fire (yesterday fill is 1d < 5d).
    # Cooldown applies to SAME ticker — different tickers are fine.
    props = [buy(TICKERS[2]), buy(TICKERS[3])]
    results = validator.validate_proposals(props, pf, {})
    check("first BUY ok after yesterday fills", results[0].approved, reasons_of(results[0]))
    check("second BUY ok after yesterday fills", results[1].approved, reasons_of(results[1]))


def test_unparseable_timestamp_fail_open():
    print("unparseable timestamps fail open (not counted):")
    history = [
        {"ticker": "XXX", "action": "BUY", "status": "filled",
         "filled_at": "garbage"},
        {"ticker": "YYY", "action": "BUY", "status": "filled",
         "filled_at": None},
    ]
    n = validator._count_filled_buys_today(
        {"history": history}, today_et=validator._et_today())
    check("unparseable history contributes 0", n == 0, f"got {n}")
    # And a fresh BUY still validates under the cap.
    res = validator.validate_proposals(
        [buy(TICKERS[2])], _empty_portfolio(history), {})[0]
    check("BUY still approved with garbage history stamps",
          res.approved, reasons_of(res))


def test_submitted_at_fallback():
    print("submitted_at used when filled_at missing:")
    now = _et_today_iso_now()
    history = [{
        "ticker": TICKERS[0], "action": "BUY", "status": "filled",
        "submitted_at": now,  # no filled_at
    }]
    n = validator._count_filled_buys_today(
        {"history": history}, today_et=validator._et_today())
    check("counts via submitted_at", n == 1, f"got {n}")


def test_sells_unaffected_by_daily_buy_cap():
    print("SELL_TO_CLOSE ignores the daily BUY cap:")
    now = _et_today_iso_now()
    history = [
        _filled_buy(TICKERS[0], now),
        _filled_buy(TICKERS[1], now),
    ]
    pf = {
        "total_equity_usd": 10000, "cash_usd": 8000,
        "positions": [{"ticker": "DELL", "quantity": 2, "avg_cost": 400,
                       "market_value_usd": 900}],
        "history": history,
    }
    sell = {"ticker": "DELL", "action": "SELL_TO_CLOSE",
            "thesis": "Thesis break; rotating out of the name."}
    res = validator.validate_proposals([sell], pf, {})[0]
    check("sell approved even at daily BUY cap", res.approved, reasons_of(res))
    check("no daily-cap reason on sell",
          not any("max_new_positions_per_day" in r for r in res.reasons),
          reasons_of(res))


if __name__ == "__main__":
    for fn in (test_count_filled_buys_today,
               test_batch_only_cap_still_enforced,
               test_prior_filled_buys_block_new_entries,
               test_one_prior_allows_one_more,
               test_yesterday_fills_do_not_count,
               test_unparseable_timestamp_fail_open,
               test_submitted_at_fallback,
               test_sells_unaffected_by_daily_buy_cap):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All daily-buy-cap checks passed.")
