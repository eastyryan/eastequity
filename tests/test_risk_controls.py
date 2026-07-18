"""Tests for the risk controls wired in from autonomy_config.json (previously
declared but enforced nowhere): cash reserve floor, sector concentration cap,
same-ticker entry cooldown, daily-loss/drawdown halts, market-hours gate.
Pure Python, no network:

    python3 tests/test_risk_controls.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator

CFG = validator.load_config()
SECTOR_MAP = validator.load_sector_map()

# Two tickers sharing a sector, discovered from the live universe file.
_by_sector = {}
for _t, _s in SECTOR_MAP.items():
    _by_sector.setdefault(_s, []).append(_t)
SECTOR, _pair = next((s, sorted(ts)[:2]) for s, ts in sorted(_by_sector.items())
                     if len(ts) >= 2)
HELD_A, BUY_B = _pair

FAILURES = []


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


def test_cash_reserve_floor():
    print("min_cash_reserve_pct (bites when marks drift and exposure cap won't):")
    # equity 10000 but stale marks: mv sums 6000 while cash is only 2500.
    portfolio = {"total_equity_usd": 10000, "cash_usd": 2500,
                 "positions": [{"ticker": "ZZZQ", "market_value_usd": 6000,
                                "quantity": 60.0, "avg_cost": 100.0,
                                "plan": {"stop_loss": 98.0}}]}
    res = validator.validate_proposals([buy("DELL")], portfolio, {})[0]
    check("rejected", not res.approved, reasons_of(res))
    check("reason is cash_reserve_floor_breached",
          any("cash_reserve_floor_breached" in r for r in res.reasons), reasons_of(res))
    portfolio["cash_usd"] = 3000  # 3000 - 900 = 2100 >= 2000 reserve
    res = validator.validate_proposals([buy("DELL")], portfolio, {})[0]
    check("passes with enough cash", res.approved, reasons_of(res))


def test_sector_concentration():
    print(f"max_sector_concentration_pct (pair {HELD_A}/{BUY_B} in '{SECTOR}'):")
    portfolio = {"total_equity_usd": 10000, "cash_usd": 6500,
                 "positions": [{"ticker": HELD_A, "market_value_usd": 3500,
                                "quantity": 35.0, "avg_cost": 100.0,
                                "plan": {"stop_loss": 98.0}}]}
    res = validator.validate_proposals([buy(BUY_B)], portfolio, {})[0]
    check("rejected (3500 held + 900 > 40% of 10000)", not res.approved, reasons_of(res))
    check("reason names the sector",
          any(f"sector_concentration_exceeded:{SECTOR}" in r for r in res.reasons),
          reasons_of(res))
    portfolio["positions"][0]["market_value_usd"] = 2500  # 2500 + 900 <= 4000
    res = validator.validate_proposals([buy(BUY_B)], portfolio, {})[0]
    check("passes under the cap", res.approved, reasons_of(res))


def test_entry_cooldown():
    print("min_days_between_entries_same_ticker:")
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    portfolio = {"total_equity_usd": 10000, "cash_usd": 10000, "positions": [],
                 "history": [{"ticker": "DELL", "action": "BUY",
                              "status": "filled", "filled_at": recent}]}
    res = validator.validate_proposals([buy("DELL")], portfolio, {})[0]
    check("re-entry 2d after last buy rejected", not res.approved, reasons_of(res))
    check("reason is entry_cooldown",
          any("entry_cooldown" in r for r in res.reasons), reasons_of(res))
    portfolio["history"][0]["filled_at"] = old
    res = validator.validate_proposals([buy("DELL")], portfolio, {})[0]
    check("re-entry 10d after last buy passes", res.approved, reasons_of(res))


def test_cooldown_applies_to_adds():
    print("cooldown also throttles scale-in adds:")
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    portfolio = {"total_equity_usd": 10000, "cash_usd": 9000,
                 "positions": [{"ticker": "DELL", "quantity": 1.0, "avg_cost": 400.0,
                                "market_value_usd": 400.0, "adds_count": 0,
                                "plan": {"stop_loss": 390.0}}],
                 "history": [{"ticker": "DELL", "action": "BUY",
                              "status": "filled", "filled_at": recent}]}
    res = validator.validate_proposals([buy("DELL", size=300.0)], portfolio, {})[0]
    check("add 2d after entry rejected", not res.approved, reasons_of(res))
    check("entry_cooldown among reasons",
          any("entry_cooldown" in r for r in res.reasons), reasons_of(res))


def test_risk_halt_reasons():
    print("daily-loss / drawdown halts (pure, auto-recovering):")
    hist = [{"date": "2026-07-09", "equity": 11800},
            {"date": "2026-07-10", "equity": 12000},
            {"date": "2026-07-12", "equity": 10000}]
    halts = validator.risk_halt_reasons(9600, hist, CFG, today_et="2026-07-13")
    check("-4% day trips daily_loss_halt",
          any(h.startswith("daily_loss_halt") for h in halts), str(halts))
    check("20% off the 12000 peak trips drawdown_halt",
          any(h.startswith("drawdown_halt") for h in halts), str(halts))
    halts = validator.risk_halt_reasons(9750, hist, CFG, today_et="2026-07-13")
    check("-2.5% day does not trip daily loss",
          not any(h.startswith("daily_loss_halt") for h in halts), str(halts))
    check("18.8% dd still trips drawdown",
          any(h.startswith("drawdown_halt") for h in halts), str(halts))
    check("recovered equity clears every halt",
          validator.risk_halt_reasons(11000, hist, CFG, today_et="2026-07-13") == [])
    check("no history -> no halts (fail open)",
          validator.risk_halt_reasons(9000, [], CFG, today_et="2026-07-13") == [])
    check("garbage equity -> no halts (fail open)",
          validator.risk_halt_reasons(None, hist, CFG, today_et="2026-07-13") == [])


def test_is_market_hours():
    print("is_market_hours boundaries (2026-07-13 is a Monday):")
    check("9:29 ET pre-open false",
          not validator.is_market_hours(datetime(2026, 7, 13, 9, 29)))
    check("9:30 ET open true", validator.is_market_hours(datetime(2026, 7, 13, 9, 30)))
    check("12:00 ET true", validator.is_market_hours(datetime(2026, 7, 13, 12, 0)))
    check("16:00 ET close true", validator.is_market_hours(datetime(2026, 7, 13, 16, 0)))
    check("16:01 ET false", not validator.is_market_hours(datetime(2026, 7, 13, 16, 1)))
    check("Saturday false", not validator.is_market_hours(datetime(2026, 7, 18, 12, 0)))


if __name__ == "__main__":
    for fn in (test_cash_reserve_floor, test_sector_concentration, test_entry_cooldown,
               test_cooldown_applies_to_adds, test_risk_halt_reasons, test_is_market_hours):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All risk-control checks passed.")
