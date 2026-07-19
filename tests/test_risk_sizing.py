"""Offline tests for risk-based sizing, portfolio heat, theme risk, and the
regime gate (validator additions, 2026-07-16). Pure python, no network."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator


def buy(ticker="NVDA", size=1000.0, entry=100.0, stop=92.0, target=120.0,
        driver="ai_compute_gpu", **kw):
    p = {"ticker": ticker, "action": "BUY", "instrument": "EQUITY",
         "position_size_usd": size, "entry_price_max": entry, "stop_loss": stop,
         "target_price": target, "holding_horizon_days": 30, "confidence": 0.7,
         "risk_reward_ratio": round((target - entry) / (entry - stop), 2),
         "thesis": "Clean swing setup with a real catalyst and rising estimates.",
         "macro_context": "Regime supports adding measured long exposure.",
         "catalysts": ["Earnings in ~3 weeks"],
         "risk_map": "Guide cut or break of the 50-DMA kills the trade.",
         "scenarios": {"bull": {"price": 125, "prob": 0.3},
                       "base": {"price": 115, "prob": 0.5},
                       "bear": {"price": 90, "prob": 0.2}},
         "variant_perception": "Consensus models a flat multiple into FY2027 and the sell-side mean target sits 4% below spot; I think estimates lag the order book by a full quarter. The mechanism is that backlog conversion is booked on shipment, so the revenue already contracted does not appear until the next print. Resolves at the Q3 earnings report on 2026-08-27."
                               "growth because estimates lag; resolves at the next print.",
         "demand_driver": driver,
         "thesis_invalidators": {
             "invalidating_print": "EPS guide cut or estimate cuts >5%",
             "invalidating_structure": "Close below the 50-DMA on rising volume",
             "time_box": "If no progress toward thesis in 25 trading days, exit"}}
    p.update(kw)
    return p


def pf(equity=10000.0, positions=None):
    return {"total_equity_usd": equity, "cash_usd": equity,
            "positions": positions or [], "history": []}


def pos(ticker, cost, stop, qty, driver=None, trailing=None):
    p = {"ticker": ticker, "avg_cost": cost, "quantity": qty,
         "market_value_usd": cost * qty,
         "plan": {"stop_loss": stop, "demand_driver": driver}}
    if trailing is not None:
        p["trailing_stop"] = trailing
    return p


def test_risk_clamp_math():
    # $2000 at 8% stop = $160 risk > 1% budget ($100) -> clamped to $1250.
    p = buy(size=2000.0)
    r = validator.validate_proposals([p], pf(), {})[0]
    assert r.approved, r.reasons
    assert abs(p["position_size_usd"] - 1250.0) < 0.01
    assert "risk-clamped" in p.get("sizing_note", "")


def test_within_budget_untouched():
    p = buy(size=1000.0)  # $80 risk < $100 budget
    r = validator.validate_proposals([p], pf(), {})[0]
    assert r.approved, r.reasons
    assert p["position_size_usd"] == 1000.0 and "sizing_note" not in p


def test_tight_stop_sizes_bigger():
    # 5% stop: budget $100 / 0.05 = $2000 allowed (the notional ceiling).
    p = buy(size=2000.0, stop=95.0, target=111.0)  # rr = 11/5 = 2.2
    r = validator.validate_proposals([p], pf(), {})[0]
    assert r.approved, r.reasons
    assert p["position_size_usd"] == 2000.0


def test_clamp_floor_rejects():
    # 15% stop on tiny equity: budget $10 -> $67 position < $200 floor -> reject.
    p = buy(size=500.0, stop=85.0, target=131.0)
    r = validator.validate_proposals([p], pf(equity=1000.0), {})[0]
    assert not r.approved
    assert any("risk_budget_size_too_small" in x for x in r.reasons)


def test_portfolio_heat_cap():
    # Open book already carries $750 committed risk; 8% cap on $10k = $800.
    positions = [pos("AAPL", 100, 85, 10, "consumer_platforms"),      # $150
                 pos("MSFT", 200, 170, 10, "software_platforms"),     # $300
                 pos("AMZN", 150, 120, 10, "consumer_platforms")]     # $300
    p = buy(size=1000.0)  # $80 risk -> 750+80=830 > 800
    r = validator.validate_proposals([p], pf(positions=positions), {})[0]
    assert not r.approved
    assert any("portfolio_heat_cap_exceeded" in x for x in r.reasons)


def test_trailed_stop_frees_heat():
    # Same book, but stops ratcheted above cost: committed risk = 0 -> BUY fits.
    # Holdings deliberately OUTSIDE factor_map.AI_STACK_DRIVERS so this isolates the
    # heat control. AAPL/MSFT/AMZN are all mega_tech_platforms (an AI-stack driver),
    # which made the book 45% factor-stacked and tripped the new cap instead.
    positions = [pos("ABBV", 100, 85, 10, trailing=105.0),
                 pos("CAT", 200, 170, 10, trailing=210.0),
                 pos("JNJ", 150, 120, 10, trailing=155.0)]
    p = buy(size=1000.0)
    r = validator.validate_proposals([p], pf(positions=positions), {})[0]
    assert r.approved, r.reasons


def test_theme_risk_cap():
    # $150 of committed hyperscaler risk held; new $80 same-theme -> 230 > $200 cap.
    positions = [pos("DELL", 450, 435, 10, "hyperscaler_server_capex")]  # $150
    p = buy(ticker="HPE", size=1000.0, driver="hyperscaler_server_capex")
    r = validator.validate_proposals([p], pf(positions=positions), {})[0]
    assert not r.approved
    assert any("theme_risk_cap_exceeded" in x for x in r.reasons)
    # A different theme passes the PER-DRIVER cap — but it must be outside the AI
    # factor stack. CONTRACT CHANGE (2026-07-19): datacenter_power is a different
    # demand_driver AND a member of AI_STACK_DRIVERS, so it is now caught one level
    # up by the aggregate factor cap. That was the point: "different driver" no
    # longer implies "different bet".
    p2 = buy(ticker="ABBV", size=1000.0, driver="healthcare")
    r2 = validator.validate_proposals([p2], pf(positions=positions), {})[0]
    assert r2.approved, r2.reasons


def test_a_different_ai_stack_driver_still_hits_the_factor_cap():
    """The gap the per-driver cap left open: 8 positions / 4 AI drivers = 100% AI."""
    positions = [pos("DELL", 450, 435, 10, "hyperscaler_server_capex")]  # $4500 MV
    p = buy(ticker="CEG", size=1000.0, driver="datacenter_power")
    r = validator.validate_proposals([p], pf(positions=positions), {})[0]
    assert not r.approved
    assert any("factor_stack_concentration_exceeded" in x for x in r.reasons), r.reasons


def test_theme_cap_batch_accumulates():
    # Two same-theme BUYs at $100 risk each sit exactly AT the 2% cap; a third breaks it.
    a = buy(ticker="DELL", size=1250.0, driver="hyperscaler_server_capex")
    b = buy(ticker="HPE", size=1250.0, driver="hyperscaler_server_capex")
    c = buy(ticker="SMCI", size=1250.0, driver="hyperscaler_server_capex")
    ra, rb, rc = validator.validate_proposals([a, b, c], pf(), {})
    assert ra.approved and rb.approved
    assert not rc.approved
    assert any("theme_risk_cap_exceeded" in x for x in rc.reasons)


def test_regime_gate_blocks_buys_not_sells():
    ctx = {"_regime": {"spy_below_200dma": True}}
    r = validator.validate_proposals([buy()], pf(), ctx)[0]
    assert not r.approved
    assert any("regime_gate_spy_below_200dma" in x for x in r.reasons)
    sell = {"ticker": "DELL", "action": "SELL_TO_CLOSE", "thesis": "cutting"}
    held = pf(positions=[pos("DELL", 450, 408, 2.2)])
    rs = validator.validate_proposals([sell], held, ctx)[0]
    assert rs.approved, rs.reasons


def test_regime_gate_fails_open():
    r = validator.validate_proposals([buy()], pf(), {})[0]
    assert r.approved, r.reasons
    r2 = validator.validate_proposals([buy()], pf(), {"_regime": {}})[0]
    assert r2.approved, r2.reasons


def test_momentum_unwind_halves_budget():
    ctx = {"_regime": {"momentum_unwind": True}}
    p = buy(size=2000.0)  # budget $50 at 8% stop -> $625
    r = validator.validate_proposals([p], pf(), ctx)[0]
    assert r.approved, r.reasons
    assert abs(p["position_size_usd"] - 625.0) < 0.01
    assert "momentum-unwind" in p.get("sizing_note", "")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  [PASS] {name}")
            except AssertionError as e:
                failed += 1
                print(f"  [FAIL] {name}: {e}")
    sys.exit(1 if failed else 0)
