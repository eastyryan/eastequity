"""Tests for the volatility-aware stop floor (validator.stop_floor_pct +
_check_volatility_stop). Pure Python, no network - run with:

    python3 tests/test_volatility_stop.py

The floor rejects stops placed inside a name's noise band (the DELL problem:
a 6% stop against a ±12% expected move / ~7% daily ATR). It fails open when no
volatility data is present, and flags names too volatile for any valid stop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator

CFG = validator.load_config()
UNIVERSE = validator.load_universe()
TICKER = "DELL" if "DELL" in UNIVERSE else sorted(UNIVERSE)[0]

# A portfolio that clears the sizing/exposure rules so only the stop check varies.
PORTFOLIO = {"total_equity_usd": 10000, "positions": []}


def base_proposal(entry, stop, target, rr):
    """A proposal that passes every rule EXCEPT whatever the test varies."""
    return {
        "ticker": TICKER,
        "action": "BUY",
        "instrument": "EQUITY",
        "position_size_usd": 1000,
        "entry_price_max": entry,
        "stop_loss": stop,
        "target_price": target,
        "holding_horizon_days": 30,
        "confidence": 0.7,
        "risk_reward_ratio": rr,
        "thesis": "Clean swing setup with a real catalyst and rising estimates.",
        "macro_context": "Regime supports adding measured long exposure.",
        "variant_perception": "Consensus sees a fair multiple; I see mispriced growth because estimates lag; resolves at the next print.",
        "thesis_invalidators": {"invalidating_print": "EPS guide cut or estimate cuts >5%", "invalidating_structure": "Close below the 50-DMA on rising volume", "time_box": "If no progress toward thesis in 25 trading days, exit"},
        "demand_driver": "hyperscaler_server_capex",
        "catalysts": ["Earnings in ~3 weeks"],
    }


def validate_one(p, market_context):
    return validator.validate_proposals([p], PORTFOLIO, market_context)[0]


def reasons_of(res):
    return " | ".join(res.reasons)


FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_stop_floor_pct_math():
    print("stop_floor_pct math:")
    # ATR only: 1.0 x 7.36% = 0.0736
    f = validator.stop_floor_pct(7.36, None, CFG)
    check("atr-only floor ~= 7.36%", f is not None and abs(f - 0.0736) < 1e-6, f"got {f}")
    # Both present: max(1.0x4%, 0.5x20%) = max(4%, 10%) = 10%
    f = validator.stop_floor_pct(4.0, 20.0, CFG)
    check("expected-move term dominates -> 10%", f is not None and abs(f - 0.10) < 1e-6, f"got {f}")
    # Neither -> None (fail open)
    check("no vol data -> None", validator.stop_floor_pct(None, None, CFG) is None)
    check("zero/garbage atr -> None", validator.stop_floor_pct(0, None, CFG) is None)


def test_too_tight_stop_rejected():
    print("too-tight stop (the DELL problem):")
    # entry 450, stop 430 = 4.4% < 7.36% ATR floor. RR fine (60/20=3.0).
    p = base_proposal(450.0, 430.0, 510.0, 3.0)
    res = validate_one(p, {TICKER: {"atr_pct": 7.36}})
    check("rejected", not res.approved, reasons_of(res))
    check("reason is stop_inside_noise_band",
          any("stop_inside_noise_band" in r for r in res.reasons), reasons_of(res))


def test_wide_enough_stop_approved():
    print("stop outside the noise band:")
    # entry 450, stop 412 = 8.4% > 7.36% floor. RR = 60/38 = 1.58.
    p = base_proposal(450.0, 412.0, 510.0, 1.6)
    res = validate_one(p, {TICKER: {"atr_pct": 7.36}})
    check("approved", res.approved, reasons_of(res))


def test_fail_open_without_vol_data():
    print("fail-open when no volatility data:")
    # Same too-tight 4.4% stop, but no market_context for the ticker.
    p = base_proposal(450.0, 430.0, 510.0, 3.0)
    res = validate_one(p, {})  # empty context
    check("approved (vol check skipped)", res.approved, reasons_of(res))
    check("no stop_inside_noise_band reason",
          not any("stop_inside_noise_band" in r for r in res.reasons))


def test_expected_move_term_can_reject():
    print("expected-move term drives the floor:")
    # Low ATR (4%) but ±20% expected move -> floor 10%. Stop 6% must be rejected.
    p = base_proposal(450.0, 423.0, 510.0, 2.2)  # 423 = 6% below 450
    res = validate_one(p, {TICKER: {"atr_pct": 4.0, "expected_move_pct": 20.0}})
    check("rejected by expected-move floor", not res.approved, reasons_of(res))
    check("reason cites expected move",
          any("stop_inside_noise_band" in r and "expected move" in r for r in res.reasons),
          reasons_of(res))


def test_untradeable_when_floor_exceeds_cap():
    print("too volatile for any valid stop:")
    # ATR 20% -> floor 20% > 15% max stop cap. No stop can satisfy both.
    p = base_proposal(450.0, 400.0, 560.0, 2.2)  # 400 = 11.1% below (under the 15% cap)
    res = validate_one(p, {TICKER: {"atr_pct": 20.0}})
    check("rejected", not res.approved, reasons_of(res))
    check("reason is volatility_untradeable",
          any("volatility_untradeable" in r for r in res.reasons), reasons_of(res))


def test_backward_compatible_default_arg():
    print("backward compatibility:")
    # Old two-arg call site must still work (market_context defaults to None).
    p = base_proposal(450.0, 412.0, 510.0, 1.6)
    res = validator.validate_proposals([p], PORTFOLIO)[0]
    check("two-arg validate_proposals still approves a clean proposal",
          res.approved, reasons_of(res))


if __name__ == "__main__":
    print(f"Universe ticker under test: {TICKER}\n")
    for fn in (test_stop_floor_pct_math, test_too_tight_stop_rejected,
               test_wide_enough_stop_approved, test_fail_open_without_vol_data,
               test_expected_move_term_can_reject, test_untradeable_when_floor_exceeds_cap,
               test_backward_compatible_default_arg):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All volatility-stop checks passed.")
