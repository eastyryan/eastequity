"""Validator gates for thesis_invalidators, demand_driver, theme concentration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def base_buy(ticker="ANET", driver="networking", size=800.0):
    return {
        "ticker": ticker, "action": "BUY", "instrument": "EQUITY",
        "position_size_usd": size, "entry_price_max": 100.0, "stop_loss": 90.0,
        "target_price": 120.0, "holding_horizon_days": 30, "confidence": 0.7,
        "risk_reward_ratio": 2.0,
        "thesis": "Clean swing setup with catalyst and rising estimates for a fat pitch.",
        "macro_context": "Regime supports measured long exposure.",
        "catalysts": ["Next product cycle print in 3 weeks"],
        "variant_perception": (
            "Consensus sees a fair multiple; I see mispriced growth because estimates lag; "
            "the mechanism is backlog conversion; resolves at the next earnings print."),
        "risk_map": "Guide cut or failed reclaim of the 50-DMA kills it.",
        "scenarios": {"bull": {"price": 130, "prob": 0.3}, "base": {"price": 120, "prob": 0.45},
                      "bear": {"price": 85, "prob": 0.25}},
        "thesis_invalidators": {
            "invalidating_print": "EPS guide cut or estimates cut more than 5 percent",
            "invalidating_structure": "Close below the 50-day moving average on rising volume",
            "time_box": "If no progress toward thesis within 25 trading days, exit",
        },
        "demand_driver": driver,
    }


def test_invalidators_and_driver_required():
    print("falsifiers + demand_driver:")
    pf = {"total_equity_usd": 10000, "cash_usd": 9000, "positions": [], "history": []}
    r = validator.validate_proposals([base_buy()], pf)[0]
    check("full BUY approved", r.approved, " | ".join(r.reasons))

    bad = base_buy()
    del bad["thesis_invalidators"]
    r = validator.validate_proposals([bad], pf)[0]
    check("missing invalidators rejected", not r.approved)
    check("reason mentions invalidators",
          any("thesis_invalidator" in x or "missing_required_field:thesis_invalidators" in x
              for x in r.reasons), " | ".join(r.reasons))

    weak = base_buy()
    weak["thesis_invalidators"] = {"invalidating_print": "x", "invalidating_structure": "y",
                                   "time_box": "z"}
    r = validator.validate_proposals([weak], pf)[0]
    check("weak invalidators rejected", not r.approved)

    nod = base_buy()
    del nod["demand_driver"]
    r = validator.validate_proposals([nod], pf)[0]
    check("missing demand_driver rejected", not r.approved)


def test_theme_concentration():
    print("theme concentration (server stack):")
    # Two server names already ~30% equity; adding another server should breach 35%.
    pf = {
        "total_equity_usd": 10000, "cash_usd": 6000,
        "positions": [
            # quantity/avg_cost/plan present because a real position always carries
            # them; a stopless position now fails the heat cap CLOSED.
            {"ticker": "DELL", "market_value_usd": 1500, "quantity": 3.0,
             "avg_cost": 500.0, "plan": {"stop_loss": 490.0},
             "demand_driver": "hyperscaler_server_capex"},
            {"ticker": "HPE", "market_value_usd": 1500, "quantity": 75.0,
             "avg_cost": 20.0, "plan": {"stop_loss": 19.8},
             "demand_driver": "hyperscaler_server_capex"},
        ],
        "history": [],
    }
    r = validator.validate_proposals(
        [base_buy("SMCI", "hyperscaler_server_capex", size=900)], pf)[0]
    check("third server rejected on theme", not r.approved, " | ".join(r.reasons))
    check("theme reason", any("theme_concentration" in x for x in r.reasons),
          " | ".join(r.reasons))

    r2 = validator.validate_proposals(
        [base_buy("NVDA", "ai_compute_gpu", size=900)], pf)[0]
    check("different theme still ok", r2.approved, " | ".join(r2.reasons))


if __name__ == "__main__":
    test_invalidators_and_driver_required()
    test_theme_concentration()
    if FAILURES:
        print(f"\nFAILED: {FAILURES}")
        sys.exit(1)
    print("\nAll reasoning gate tests passed.")
