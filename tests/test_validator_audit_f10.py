"""F10 regression: the kill switch halts NEW risk, never exits.

    python3 tests/test_validator_audit_f10.py

Regression context (2026-08-05 audit): validate_proposals rejected the ENTIRE
batch under kill switch, SELL_TO_CLOSE included — contradicting the doctrine
every other layer follows: forced exits bypass the validator, and the Actions
executor deliberately executes SELL intents under kill switch ("protective
exits must never be blocked by a halt"). A halted book you cannot reduce is
more dangerous than whatever the switch was pulled for. Now: BUY/HOLD reject
as KILL_SWITCH_ACTIVE, sells continue through NORMAL sell validation (they are
validated, not waved through).

Standalone and hermetic: the kill-switch file is redirected to a temp path by
wrapping load_config (conftest's own trick), so the OPERATOR's live
state/KILL_SWITCH is neither read nor written.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator

TMP = Path(tempfile.mkdtemp())
SWITCH = TMP / "KILL_SWITCH"
# Hermetic on both side files: intents queue (F9 heat) and the switch itself.
validator._INTENTS_FILE = TMP / "order_intents.json"


class _redirected_switch:
    """Scoped load_config wrapper (conftest's own trick, but save/restored per
    test so nothing leaks if this file is ever collected alongside the suite)."""

    def __enter__(self):
        self._orig = validator.load_config

        def _test_config(_orig=self._orig):
            cfg = _orig()
            cfg.setdefault("risk_controls", {})["kill_switch_file"] = str(SWITCH)
            return cfg

        validator.load_config = _test_config
        return self

    def __exit__(self, *exc):
        validator.load_config = self._orig
        return False


UNIVERSE = validator.load_universe()
HELD = "DELL" if "DELL" in UNIVERSE else sorted(UNIVERSE)[0]

PORTFOLIO = {
    "total_equity_usd": 10_000.0, "cash_usd": 9_000.0,
    "positions": [{"ticker": HELD, "quantity": 2.0, "avg_cost": 450.0,
                   "market_value_usd": 900.0, "plan": {"stop_loss": 430.0}}],
    "history": [],
}

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def validate(batch):
    with _redirected_switch():
        return validator.validate_proposals(batch, PORTFOLIO, {})


def sell(ticker=HELD):
    return {"ticker": ticker, "action": "SELL_TO_CLOSE",
            "thesis": "Thesis broke; reducing risk while halted."}


def buy():
    return {"ticker": HELD, "action": "BUY", "thesis": "x",
            "holding_horizon_days": 30, "entry_price_max": 100.0,
            "stop_loss": 92.0, "target_price": 130.0,
            "position_size_usd": 150.0, "confidence": 0.7}


def test_engaged_switch_blocks_buys_not_sells():
    print("kill switch ENGAGED: BUY rejected, SELL of a holding approved:")
    SWITCH.write_text("engaged by test")
    try:
        results = validate([buy(), sell(), {"ticker": HELD, "action": "HOLD"}])
        b, s, h = results
        check("BUY rejected", not b.approved, str(b.reasons))
        check("BUY reason is KILL_SWITCH_ACTIVE",
              b.reasons == ["KILL_SWITCH_ACTIVE"], str(b.reasons))
        check("SELL approved", s.approved, str(s.reasons))
        check("HOLD rejected (no new commitments while halted)",
              not h.approved and h.reasons == ["KILL_SWITCH_ACTIVE"],
              str(h.reasons))
        check("result order matches proposal order",
              [r.proposal["action"] for r in results]
              == ["BUY", "SELL_TO_CLOSE", "HOLD"])
    finally:
        SWITCH.unlink()


def test_sells_are_validated_not_waved_through():
    print("kill switch ENGAGED: sells still pass through NORMAL sell validation:")
    SWITCH.write_text("engaged by test")
    try:
        res = validate([sell(ticker="NVDA")])[0]  # not held
        check("phantom exit still rejected", not res.approved, str(res.reasons))
        check("rejected on sell_ticker_not_held, not on the switch",
              any("sell_ticker_not_held" in r for r in res.reasons)
              and "KILL_SWITCH_ACTIVE" not in res.reasons, str(res.reasons))
        bad = dict(sell(), sell_fraction=1.5)
        res = validate([bad])[0]
        check("invalid sell_fraction still rejected",
              any("invalid_sell_fraction" in r for r in res.reasons),
              str(res.reasons))
    finally:
        SWITCH.unlink()


def test_disengaged_switch_changes_nothing():
    print("kill switch OFF: behavior unchanged:")
    assert not SWITCH.exists()
    b, s = validate([buy(), sell()])
    check("BUY not blocked by a disengaged switch",
          "KILL_SWITCH_ACTIVE" not in b.reasons, str(b.reasons))
    check("SELL approved", s.approved, str(s.reasons))


if __name__ == "__main__":
    print(f"Held in-universe ticker under test: {HELD}\n")
    for fn in (test_engaged_switch_blocks_buys_not_sells,
               test_sells_are_validated_not_waved_through,
               test_disengaged_switch_changes_nothing):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All F10 kill-switch checks passed.")
