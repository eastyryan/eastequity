"""F9 regression: queued BUY intents must carry portfolio heat.

    python3 tests/test_validator_audit_f9.py

Regression context (2026-08-05 audit): _sum_committed_risk read POSITIONS only,
so a BUY queued to state/order_intents.json was invisible to every later run's
heat and theme caps until it filled — the executor runs on a */15 cron and
intents survive a market close, so a later run could stack a same-theme BUY on
top of the invisible pending one and land the combined book past the caps the
moment both filled.

Also pinned here:
  * the fail-open-but-loud contract on a corrupt side file (heat must not
    brick every run on garbage, but must say so);
  * the double-count guard once an intent fills into a position;
  * the self-exclusion used by revalidate_intent_against_book;
  * the status tuple staying in sync with execution/broker.py.

Standalone and hermetic: no conftest, no network, no state/ writes — the
intents path is redirected to a temp file for every check.
"""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator

TMP = Path(tempfile.mkdtemp())
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


CFG = validator.load_config()
HEAT_CAP = CFG["position_sizing"]["risk_based_sizing"]["portfolio_heat_cap_pct"]
THEME_CAP = CFG["position_sizing"]["risk_based_sizing"]["theme_initial_risk_cap_pct"]
assert HEAT_CAP == 0.08 and THEME_CAP == 0.02, (
    "fixture arithmetic below assumes the documented 8%/2% caps — "
    "recompute the numbers if config moved")


def set_intents(payload):
    """Point the validator at a fresh temp intents file. None -> missing file."""
    f = TMP / f"order_intents_{set_intents.n}.json"
    set_intents.n += 1
    if payload is not None:
        f.write_text(payload if isinstance(payload, str)
                     else json.dumps(payload, indent=1))
    validator._INTENTS_FILE = f
    return f


set_intents.n = 0


def portfolio(history=None):
    # equity 1000; one open position with committed risk (100-92)*5 = $40.
    return {
        "total_equity_usd": 1000.0, "cash_usd": 600.0,
        "positions": [{"ticker": "AAA", "quantity": 5.0, "avg_cost": 100.0,
                       "market_value_usd": 500.0,
                       "plan": {"stop_loss": 92.0}}],
        "history": history or [],
    }


def queued_buy(risk_usd=30.0, **over):
    # size 300 @ entry 100 with stop 90 -> committed risk $30 by default.
    stop = 100.0 * (1 - risk_usd / 300.0)
    i = {"ticker": "NVDA", "action": "BUY", "position_size_usd": 300.0,
         "entry_price_max": 100.0, "demand_driver": "ai_compute_gpu",
         "plan": {"stop_loss": stop, "entry_price_max": 100.0},
         "order_id": "EE-f9pending01", "client_order_id": "EE-f9pending01",
         "status": "queued_intent"}
    i.update(over)
    return i


def heat_reasons(proposed_risk=20.0, pf=None, exclude=None):
    reasons = []
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        validator._check_portfolio_heat(
            {"action": "BUY"}, CFG, pf or portfolio(), proposed_risk, 0.0,
            reasons, {}, exclude_intent_ids=exclude)
    return reasons, out.getvalue()


def test_baseline_without_pending_intents():
    print("baseline: open $40 + new $20 = $60 < $80 cap, no queue file:")
    set_intents(None)
    r, _ = heat_reasons()
    check("approved", r == [], str(r))


def test_pending_buy_intent_counts_as_heat():
    print("THE BUG: a queued $30-risk BUY must push $40+$30+$20=$90 past the $80 cap:")
    set_intents({"intents": [queued_buy()]})
    r, _ = heat_reasons()
    check("rejected", any("portfolio_heat_cap_exceeded" in x for x in r), str(r))
    check("reason names the queued intent",
          any("queued intents" in x for x in r), str(r))


def test_resting_status_also_counts():
    print("a resting (placed, unfilled) intent is still committed risk:")
    set_intents({"intents": [queued_buy(status="resting_market_closed")]})
    r, _ = heat_reasons()
    check("rejected", any("portfolio_heat_cap_exceeded" in x for x in r), str(r))


def test_sell_intents_do_not_count():
    print("a queued SELL reduces risk and must add no heat:")
    set_intents({"intents": [queued_buy(action="SELL_TO_CLOSE")]})
    r, _ = heat_reasons()
    check("approved", r == [], str(r))


def test_filled_intent_is_not_double_counted():
    print("once the intent fills into a position its row must stop counting:")
    set_intents({"intents": [queued_buy()]})
    filled = {"ticker": "NVDA", "action": "BUY", "status": "filled",
              "order_id": "EE-f9pending01", "client_order_id": "EE-f9pending01"}
    r, _ = heat_reasons(pf=portfolio(history=[filled]))
    check("approved (risk lives in the position now)", r == [], str(r))


def test_self_exclusion_for_revalidation():
    print("revalidate must not count the intent under judgement against itself:")
    set_intents({"intents": [queued_buy()]})
    risk, notes = validator._pending_buy_intents_risk(
        portfolio(), exclude_ids={"EE-f9pending01"})
    check("own risk excluded", risk == 0.0 and not notes, f"{risk} {notes}")
    risk, _ = validator._pending_buy_intents_risk(portfolio())
    check("counted without exclusion", abs(risk - 30.0) < 1e-9, str(risk))


def test_corrupt_file_fails_open_loudly():
    print("a corrupt side file must not brick the heat cap — but must say so:")
    set_intents("{not json")
    r, out = heat_reasons()
    check("approved (fail open)", r == [], str(r))
    check("printed a loud note", "order_intents_unreadable" in out, out)


def test_unparseable_intent_geometry_fails_open_loudly():
    print("a pending BUY with no parseable stop is skipped with a note, not a crash:")
    set_intents({"intents": [queued_buy(plan={})]})
    r, out = heat_reasons()
    check("approved (its risk is unknowable)", r == [], str(r))
    check("printed a loud note", "pending_intent_risk_unknown" in out, out)


def test_theme_cap_sees_pending_intents():
    print("theme cap: pending $30 on ai_compute_gpu vs the $20 (2%) theme budget:")
    set_intents({"intents": [queued_buy()]})
    reasons = []
    with contextlib.redirect_stdout(io.StringIO()):
        validator._check_theme_initial_risk(
            {"action": "BUY", "demand_driver": "ai_compute_gpu"},
            CFG, portfolio(), 5.0, {}, reasons)
    check("rejected", any("theme_risk_cap_exceeded" in x for x in reasons),
          str(reasons))
    check("reason names the queued intents",
          any("queued intents" in x for x in reasons), str(reasons))
    set_intents({"intents": [queued_buy(demand_driver="fintech",
                                        plan={"stop_loss": 90.0})]})
    reasons = []
    with contextlib.redirect_stdout(io.StringIO()):
        validator._check_theme_initial_risk(
            {"action": "BUY", "demand_driver": "ai_compute_gpu"},
            CFG, portfolio(), 5.0, {}, reasons)
    check("a different theme's pending intent does not count",
          reasons == [], str(reasons))


def test_status_tuple_mirrors_broker():
    print("the restated status tuple must match execution/broker.py "
          "(validator may not import broker modules):")
    from execution import broker
    check("statuses in sync",
          set(validator._PENDING_INTENT_STATUSES)
          == set(broker.QUEUED_STATUSES + broker.RESTING_STATUSES),
          f"{validator._PENDING_INTENT_STATUSES} vs "
          f"{broker.QUEUED_STATUSES + broker.RESTING_STATUSES}")


if __name__ == "__main__":
    for fn in (test_baseline_without_pending_intents,
               test_pending_buy_intent_counts_as_heat,
               test_resting_status_also_counts,
               test_sell_intents_do_not_count,
               test_filled_intent_is_not_double_counted,
               test_self_exclusion_for_revalidation,
               test_corrupt_file_fails_open_loudly,
               test_unparseable_intent_geometry_fails_open_loudly,
               test_theme_cap_sees_pending_intents,
               test_status_tuple_mirrors_broker):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All F9 pending-intent heat checks passed.")
