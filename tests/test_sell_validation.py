"""Tests for SELL_TO_CLOSE / HOLD validation semantics. Pure Python, no network:

    python3 tests/test_sell_validation.py

Regression context: every action used to be forced through the BUY-oriented
required_proposal_fields list, so a legitimate discretionary exit without
stop_loss/target_price/position_size_usd was rejected missing_required_field,
and a holding dropped from the universe could never be sold. Sells also were
never checked against actual holdings (only the broker rejected, after the
proposal was journaled approved).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator

UNIVERSE = validator.load_universe()
IN_UNIVERSE = "DELL" if "DELL" in UNIVERSE else sorted(UNIVERSE)[0]
OFF_UNIVERSE = "ZZZQ"
assert OFF_UNIVERSE not in UNIVERSE

PORTFOLIO = {
    "total_equity_usd": 10000,
    "positions": [
        {"ticker": IN_UNIVERSE, "quantity": 2.0, "avg_cost": 450.0, "market_value_usd": 900.0},
        {"ticker": OFF_UNIVERSE, "quantity": 10.0, "avg_cost": 20.0, "market_value_usd": 210.0},
    ],
}

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def validate_one(p):
    return validator.validate_proposals([p], PORTFOLIO, {})[0]


def reasons_of(res):
    return " | ".join(res.reasons)


def test_minimal_sell_of_holding_approved():
    print("minimal SELL_TO_CLOSE of a held name:")
    res = validate_one({"ticker": IN_UNIVERSE, "action": "SELL_TO_CLOSE",
                        "thesis": "Thesis broke on guidance cut; booking the loss."})
    check("approved", res.approved, reasons_of(res))


def test_sell_not_held_rejected():
    print("SELL_TO_CLOSE of a name we do not hold:")
    res = validate_one({"ticker": "NVDA", "action": "SELL_TO_CLOSE",
                        "thesis": "Phantom exit."})
    check("rejected", not res.approved, reasons_of(res))
    check("reason is sell_ticker_not_held",
          any("sell_ticker_not_held" in r for r in res.reasons), reasons_of(res))


def test_sell_off_universe_holding_approved():
    print("SELL_TO_CLOSE of a holding that left the universe:")
    res = validate_one({"ticker": OFF_UNIVERSE, "action": "SELL_TO_CLOSE",
                        "thesis": "Name was removed from the universe; exiting."})
    check("approved (no off_universe rejection)", res.approved, reasons_of(res))


def test_bad_sell_fraction_still_rejected():
    print("invalid sell_fraction still rejected:")
    res = validate_one({"ticker": IN_UNIVERSE, "action": "SELL_TO_CLOSE",
                        "thesis": "Trim half.", "sell_fraction": 1.5})
    check("rejected", not res.approved, reasons_of(res))
    check("reason is invalid_sell_fraction",
          any("invalid_sell_fraction" in r for r in res.reasons), reasons_of(res))


def test_sell_bad_confidence_range_rejected():
    print("SELL with out-of-range confidence (when present) rejected:")
    res = validate_one({"ticker": IN_UNIVERSE, "action": "SELL_TO_CLOSE",
                        "thesis": "Exit.", "confidence": 7})
    check("rejected", not res.approved, reasons_of(res))
    check("reason is confidence_not_in_0_1",
          any("confidence_not_in_0_1" in r for r in res.reasons), reasons_of(res))
    res = validate_one({"ticker": IN_UNIVERSE, "action": "SELL_TO_CLOSE",
                        "thesis": "Exit.", "confidence": 0.4})
    check("low-but-valid confidence fine on a sell (min_confidence is BUY-only)",
          res.approved, reasons_of(res))


def test_minimal_hold_approved():
    print("minimal HOLD:")
    res = validate_one({"ticker": IN_UNIVERSE, "action": "HOLD"})
    check("approved", res.approved, reasons_of(res))


def test_buy_still_requires_full_schema():
    print("BUY still requires the full field set:")
    res = validate_one({"ticker": IN_UNIVERSE, "action": "BUY",
                        "thesis": "Underspecified entry."})
    check("rejected", not res.approved, reasons_of(res))
    check("missing stop_loss flagged",
          any("missing_required_field:stop_loss" in r for r in res.reasons), reasons_of(res))
    check("missing position_size_usd flagged",
          any("missing_required_field:position_size_usd" in r for r in res.reasons),
          reasons_of(res))


if __name__ == "__main__":
    print(f"Held in-universe ticker under test: {IN_UNIVERSE}\n")
    for fn in (test_minimal_sell_of_holding_approved, test_sell_not_held_rejected,
               test_sell_off_universe_holding_approved, test_bad_sell_fraction_still_rejected,
               test_sell_bad_confidence_range_rejected, test_minimal_hold_approved,
               test_buy_still_requires_full_schema):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All sell-validation checks passed.")
