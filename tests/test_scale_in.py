"""Offline tests: scale-in adds (blended cost) + partial exits + validator rules.

    python3 tests/test_scale_in.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import execution.simulated_broker as sb  # noqa: E402
import validator  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def fresh_state(tmp):
    sb.STATE_FILE = Path(tmp) / "portfolio.json"
    sb.STATE_FILE.write_text(json.dumps(
        {"cash_usd": 10000.0, "total_equity_usd": 10000.0, "positions": [],
         "pending_orders": {}, "history": []}))


def zero_costs(monkey_target=sb):
    monkey_target._execution_costs = lambda: dict(sb._COST_DEFAULTS, slippage_bps=0)


def buy(t, usd, ref):
    o = sb.place_order({"ticker": t, "action": "BUY", "position_size_usd": usd,
                        "reference_price": ref, "proposal_id": "x"})
    return sb.readback(o["order_id"])


def sell(t, ref, frac=None):
    o = sb.place_order({"ticker": t, "action": "SELL_TO_CLOSE", "reference_price": ref,
                        "proposal_id": "x", "sell_fraction": frac})
    return sb.readback(o["order_id"])


def test_broker_add_and_partial():
    print("broker: scale-in merge + partial exit math (zero costs for clean math):")
    with tempfile.TemporaryDirectory() as tmp:
        fresh_state(tmp)
        zero_costs()
        f1 = buy("TEST", 1000, 100.0)   # 10 sh @ 100
        f2 = buy("TEST", 600, 120.0)    # 5 sh @ 120 -> blended (1000+600)/15 = 106.6667
        check("first buy not an add", f1["is_add"] is False)
        check("second buy IS an add", f2["is_add"] is True)
        st = json.loads(sb.STATE_FILE.read_text())
        pos = st["positions"][0]
        check("one merged position", len(st["positions"]) == 1)
        check("qty 15", abs(pos["quantity"] - 15.0) < 1e-6, str(pos["quantity"]))
        check("blended avg cost 106.67", abs(pos["avg_cost"] - 106.6667) < 0.001,
              str(pos["avg_cost"]))
        check("adds_count = 1", pos.get("adds_count") == 1)
        # partial exit: 1/3 at 150 -> 5 sh, pnl = 5 * (150 - 106.6667) = 216.67
        pos["dividends_received_usd"] = 9.0
        st["positions"][0] = pos
        sb.STATE_FILE.write_text(json.dumps(st))
        f3 = sell("TEST", 150.0, frac=1 / 3)
        check("partial sold 5 sh", abs(f3["quantity"] - 5.0) < 1e-6, str(f3["quantity"]))
        check("partial pnl ~216.67", abs(f3["realized_pnl_usd"] - 216.67) < 0.05,
              str(f3["realized_pnl_usd"]))
        check("dividends attributed pro-rata (3.0)", abs(f3["dividends_received_usd"] - 3.0) < 0.01)
        check("fill stamps entry avg_cost", abs(f3["avg_cost"] - 106.6667) < 0.001)
        st2 = json.loads(sb.STATE_FILE.read_text())
        check("10 sh remain", abs(st2["positions"][0]["quantity"] - 10.0) < 1e-6)
        check("remaining dividends 6.0",
              abs(st2["positions"][0]["dividends_received_usd"] - 6.0) < 0.01)
        # full close of the rest
        f4 = sell("TEST", 150.0)
        check("full close removes position",
              not json.loads(sb.STATE_FILE.read_text())["positions"])
        check("full-close pnl ~433.33", abs(f4["realized_pnl_usd"] - 433.33) < 0.05,
              str(f4["realized_pnl_usd"]))


def _prop(**kw):
    p = {"ticker": "DELL", "action": "BUY", "instrument": "EQUITY",
         "position_size_usd": 400, "entry_price_max": 460.0, "stop_loss": 415.0,
         "target_price": 520.0, "holding_horizon_days": 30, "confidence": 0.7,
         "risk_reward_ratio": 1.3, "thesis": "t", "macro_context": "m", "catalysts": ["c"]}
    p.update(kw)
    return p


def test_validator_scale_in_rules():
    print("validator: scale-in rules:")
    pf = {"total_equity_usd": 10000,
          "positions": [{"ticker": "DELL", "quantity": 1.2, "avg_cost": 450.0,
                         "market_value_usd": 550.0, "adds_count": 0}]}
    r = validator.validate_proposals([_prop()], pf)[0]
    check("add above cost within cap -> approved", r.approved, " | ".join(r.reasons))
    r2 = validator.validate_proposals([_prop(entry_price_max=440.0, stop_loss=400.0,
                                              target_price=500.0)], pf)[0]
    check("add BELOW cost rejected (never average down)",
          any("add_below_cost" in x for x in r2.reasons), " | ".join(r2.reasons))
    pf2 = json.loads(json.dumps(pf))
    pf2["positions"][0]["adds_count"] = 2
    r3 = validator.validate_proposals([_prop()], pf2)[0]
    check("max adds enforced", any("max_adds_reached" in x for x in r3.reasons))
    r4 = validator.validate_proposals([_prop(position_size_usd=900)], pf)[0]
    check("combined cap enforced (550+900 > $1000 cap)",
          any("combined_position_exceeds_cap" in x for x in r4.reasons),
          " | ".join(r4.reasons))
    # sell_fraction sanity
    s_ok = validator.validate_proposals([_prop(action="SELL_TO_CLOSE", sell_fraction=0.5)],
                                        pf)[0]
    check("sell_fraction 0.5 accepted",
          not any("invalid_sell_fraction" in x for x in s_ok.reasons))
    s_bad = validator.validate_proposals([_prop(action="SELL_TO_CLOSE", sell_fraction=1.5)],
                                         pf)[0]
    check("sell_fraction 1.5 rejected",
          any("invalid_sell_fraction" in x for x in s_bad.reasons))


if __name__ == "__main__":
    test_broker_add_and_partial()
    test_validator_scale_in_rules()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All scale-in / partial-exit checks passed.")
