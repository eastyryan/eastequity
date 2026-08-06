"""F5 regression: a plan with no parseable stop must shout UNSTOPPABLE, never
enforce a silent zero-stop. Pure Python, no network, no state writes:

    EE_BROKER=simulation python3 tests/test_exec_audit_unstoppable_shout.py

Regression context (audited 2026-08-05): exit_guard shouted UNSTOPPABLE only
when the WHOLE plan was missing. A plan present with stop_loss: None sailed
through in silence — no stop check can ever fire and ensure_protective_stop
finds no level to arm, so the position was exactly as unstoppable as one with
no plan, minus the warning. Worse: an unparseable STRING stop crashed
check_forced_exits at float(stop), taking stop enforcement down for every
OTHER position in the book. Horizon enforcement (which needs only the plan)
must keep working either way — reducing enforcement is not how a missing stop
gets reported.
"""

import contextlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EE_BROKER"] = "simulation"

from execution import exit_guard  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def run_guard(positions, prices):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exits = exit_guard.check_forced_exits({"positions": positions}, prices, {})
    return exits, buf.getvalue()


def pos(ticker, plan, **kw):
    p = {"ticker": ticker, "quantity": 2.0, "avg_cost": 100.0,
         "market_value_usd": 200.0, "original_plan": plan}
    p.update(kw)
    return p


def test_none_stop_shouts():
    print("plan present, stop_loss None:")
    exits, out = run_guard([pos("XYZ", {"stop_loss": None,
                                        "target_price": 130.0})], {"XYZ": 95.0})
    check("UNSTOPPABLE shout printed", "UNSTOPPABLE POSITION XYZ" in out, out)
    check("no exit invented", exits == [], str(exits))


def test_unparseable_stop_shouts_and_does_not_crash_the_book():
    print("unparseable string stop + a healthy breached position behind it:")
    positions = [pos("BAD", {"stop_loss": "n/a", "target_price": 130.0}),
                 pos("DELL", {"stop_loss": 100.0, "target_price": 130.0})]
    exits, out = run_guard(positions, {"BAD": 95.0, "DELL": 95.0})
    check("UNSTOPPABLE shout for the bad plan",
          "UNSTOPPABLE POSITION BAD" in out, out)
    check("guard did not crash; healthy stop still fired",
          any(e["ticker"] == "DELL" and e["reason"] == "stop_loss_breached"
              for e in exits), str(exits))


def test_zero_stop_is_missing_not_a_level():
    print("stop_loss 0 is not a level:")
    exits, out = run_guard([pos("ZRO", {"stop_loss": 0})], {"ZRO": 95.0})
    check("UNSTOPPABLE shout printed", "UNSTOPPABLE POSITION ZRO" in out, out)
    check("no zero-stop enforcement", exits == [], str(exits))


def test_horizon_enforcement_survives_a_missing_stop():
    print("missing stop must not disable horizon enforcement:")
    exits, out = run_guard(
        [pos("HOR", {"stop_loss": None, "holding_horizon_days": 5},
             days_held=10)], {"HOR": 95.0})
    check("UNSTOPPABLE shout printed", "UNSTOPPABLE POSITION HOR" in out, out)
    check("horizon exit still fires",
          any(e["ticker"] == "HOR" and e["reason"] == "horizon_expired"
              for e in exits), str(exits))
    check("exit carries no invented stop",
          all(e.get("stop_loss") is None and e.get("plan_stop_loss") is None
              for e in exits), str(exits))


def test_shout_does_not_depend_on_a_fresh_price():
    print("shout fires even when the price feed has no mark for the name:")
    exits, out = run_guard([pos("NOPX", {"stop_loss": None})], {})
    check("UNSTOPPABLE shout printed without a price",
          "UNSTOPPABLE POSITION NOPX" in out, out)
    check("no exit without a price", exits == [], str(exits))


def test_numeric_stop_stays_quiet_and_enforced():
    print("a real numeric stop: no shout, normal enforcement:")
    exits, out = run_guard([pos("OK", {"stop_loss": 90.0})], {"OK": 85.0})
    check("no UNSTOPPABLE shout", "UNSTOPPABLE" not in out, out)
    check("breach fires normally",
          any(e["ticker"] == "OK" and e["reason"] == "stop_loss_breached"
              for e in exits), str(exits))
    check("string-typed numeric stop also parses",
          run_guard([pos("STR", {"stop_loss": "90.0"})],
                    {"STR": 85.0})[0][0]["reason"] == "stop_loss_breached")


def test_no_plan_shout_unchanged():
    print("the original no-plan shout still fires (sanity):")
    exits, out = run_guard([{"ticker": "NP", "quantity": 1.0,
                             "original_plan": None}], {"NP": 95.0})
    check("UNSTOPPABLE shout printed", "UNSTOPPABLE POSITION NP" in out, out)
    check("no exit", exits == [], str(exits))


if __name__ == "__main__":
    for fn in (test_none_stop_shouts,
               test_unparseable_stop_shouts_and_does_not_crash_the_book,
               test_zero_stop_is_missing_not_a_level,
               test_horizon_enforcement_survives_a_missing_stop,
               test_shout_does_not_depend_on_a_fresh_price,
               test_numeric_stop_stays_quiet_and_enforced,
               test_no_plan_shout_unchanged):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All unstoppable-shout checks passed.")
