"""F4 regression: queued-intent re-validation must re-run the IDENTITY gates.

    python3 tests/test_validator_audit_f4.py

Regression context (2026-08-05 audit): revalidate_intent_against_book re-ran
only the book-shape caps (funding, heat, theme, factor, beta, stress, sector),
and the Actions executor never re-checked universe membership, the forbidden
leveraged/inverse ticker list, the action whitelist, the instrument whitelist,
or the $1B market-cap floor. state/order_intents.json is git-committed and a
push to it TRIGGERS execution, so a hand-edited or bad-merged BUY of SOXL — a
name the validator would reject on sight at proposal time — executed if it was
merely funded and inside the caps.

Standalone and hermetic: no conftest, no network, no state/ writes. The live
order-intents queue is pointed at an empty temp path so pending-intent heat
(F9) cannot leak into these checks.
"""

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator

# Hermetic: F9 heat must read an (absent) fixture queue, never the live one.
validator._INTENTS_FILE = Path(tempfile.mkdtemp()) / "order_intents.json"

UNIVERSE = validator.load_universe()
IN_UNIVERSE = "NVDA" if "NVDA" in UNIVERSE else sorted(UNIVERSE)[0]
OFF_UNIVERSE = "ZZZQ"
assert OFF_UNIVERSE not in UNIVERSE
FORBIDDEN = "SOXL"
assert FORBIDDEN in validator.load_config()["hard_rules"]["forbidden_ticker_patterns"]

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def book(equity=10_000.0):
    return {"total_equity_usd": equity, "cash_usd": equity, "positions": [],
            "history": []}


def intent(**over):
    # Shape mirrors execution/alpaca_broker place_order -> _save_intents: the
    # order dict from runlib/brain_io plus order ids and queue status. Sized
    # inside the LIVE config's notional caps so only the check under test fires.
    i = {
        "ticker": IN_UNIVERSE, "action": "BUY", "position_size_usd": 150.0,
        "entry_price_max": 100.0, "reference_price": 99.5,
        "demand_driver": "ai_compute_gpu",
        "plan": {"stop_loss": 92.0, "entry_price_max": 100.0,
                 "demand_driver": "ai_compute_gpu"},
        "order_id": "EE-f4test000001", "client_order_id": "EE-f4test000001",
        "status": "queued_intent",
    }
    i.update(over)
    return i


def reval(i, market_context=None, cfg=None):
    return validator.revalidate_intent_against_book(
        i, book(), market_context or {}, cfg=cfg)


def test_sane_buy_still_passes():
    print("a sane in-universe BUY intent still executes (the gate is not a kill switch):")
    r = reval(intent())
    check("no reasons", r == [], str(r))


def test_forbidden_ticker_rejected():
    print("hand-edited BUY of a leveraged ETF is stopped at the relay:")
    r = reval(intent(ticker=FORBIDDEN))
    check("forbidden_ticker fires", any("forbidden_ticker" in x for x in r), str(r))


def test_off_universe_rejected():
    print("BUY of an off-universe name is stopped at the relay:")
    r = reval(intent(ticker=OFF_UNIVERSE))
    check("off_universe_ticker fires",
          any("off_universe_ticker" in x for x in r), str(r))


def test_bad_ticker_format_rejected():
    print("garbage ticker is stopped at the relay:")
    r = reval(intent(ticker="123$"))
    check("invalid_ticker_format fires",
          any("invalid_ticker_format" in x for x in r), str(r))


def test_unknown_action_rejected():
    print("an action outside BUY/SELL_TO_CLOSE is a corrupted row, not an exit:")
    r = reval(intent(action="SHORT_SELL"))
    check("forbidden_intent_action fires",
          any("forbidden_intent_action" in x for x in r), str(r))
    r = reval(intent(action=""))
    check("missing action also rejected",
          any("forbidden_intent_action" in x for x in r), str(r))


def test_forbidden_instrument_rejected():
    print("a non-EQUITY instrument on a BUY intent is rejected:")
    r = reval(intent(instrument="OPTION"))
    check("forbidden_instrument fires",
          any("forbidden_instrument" in x for x in r), str(r))


def test_sells_are_never_blocked():
    print("SELL_TO_CLOSE is never blocked here, even on a forbidden/off-universe name:")
    r = reval(intent(action="SELL_TO_CLOSE", ticker=FORBIDDEN,
                     position_size_usd=90_000))
    check("forbidden-name sell passes", r == [], str(r))
    r = reval(intent(action="SELL_TO_CLOSE", ticker=OFF_UNIVERSE))
    check("off-universe sell passes", r == [], str(r))


def test_market_cap_floor_when_knowable():
    print("the $1B floor binds when the executor's context DOES carry a cap:")
    r = reval(intent(), market_context={IN_UNIVERSE: {"market_cap_usd": 5e8}})
    check("below_min_market_cap fires",
          any("below_min_market_cap" in x for x in r), str(r))
    r = reval(intent(), market_context={IN_UNIVERSE: {"market_cap_usd": 5e11}})
    check("above-floor cap passes", r == [], str(r))


def test_market_cap_absence_never_fails_closed_here():
    print("no cap in the relay context must NOT reject, even with "
          "require_market_cap_data flipped on:")
    cfg = copy.deepcopy(validator.load_config())
    cfg.setdefault("book_risk", {})["require_market_cap_data"] = True
    r = reval(intent(), market_context={}, cfg=cfg)
    check("no market_cap_unverifiable at the relay",
          not any("market_cap_unverifiable" in x for x in r), str(r))


if __name__ == "__main__":
    print(f"In-universe ticker under test: {IN_UNIVERSE}\n")
    for fn in (test_sane_buy_still_passes, test_forbidden_ticker_rejected,
               test_off_universe_rejected, test_bad_ticker_format_rejected,
               test_unknown_action_rejected, test_forbidden_instrument_rejected,
               test_sells_are_never_blocked, test_market_cap_floor_when_knowable,
               test_market_cap_absence_never_fails_closed_here):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All F4 identity-gate checks passed.")
