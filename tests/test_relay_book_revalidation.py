"""A queued order must re-clear the BOOK CAPS before it reaches the broker.

THE HOLE (audited 2026-07-19)
-----------------------------
scripts/execute_order_intents.py re-placed whatever sat in
state/order_intents.json after only alpaca_broker.validate_order_static — cash,
notional, market hours, live price ceiling. The full validator never ran again:
no portfolio heat, no theme risk, no theme notional, no factor stack, no beta,
no stress, no sector concentration.

Two consequences, one adversarial and one entirely honest:

  * state/order_intents.json is git-committed and a push to it TRIGGERS
    execute-orders.yml. So repo write access was broker access, and a bad merge,
    a rebase artifact or a hand-edit became a live BUY that no risk cap saw.

  * The queueing node validated against a book that is minutes to hours stale by
    the time the runner fires (execute-orders runs */15 and intents survive a
    market close). A BUY validated at 7.9% portfolio heat could execute into a
    book well past the 8% cap. The script's own docstring acknowledged the
    staleness — but the fix only ever covered FUNDING.

WHAT IS AND IS NOT RE-CHECKED
    Re-checked: the shape of the book, because that is what the passage of time
    can actually invalidate.
    Not re-checked: variant_perception, scenarios, thesis_invalidators, RR
    geometry. Those are judgements about the IDEA, made at commit time;
    re-litigating them here would let a transient data gap silently cancel an
    approved trade.

DIRECTION OF FAILURE
    Fail CLOSED. Everywhere else in this repo an unverifiable input tends to
    fail open, but here "we could not check" arrives precisely when the queued
    BUY is most likely to be wrong. Exits are never routed through this path.
"""

from __future__ import annotations

import pytest

import validator

CFG = validator.load_config()


def _book(equity=10_000.0, positions=None) -> dict:
    return {"total_equity_usd": equity, "cash_usd": equity,
            "positions": positions or []}


def _intent(**over) -> dict:
    i = {
        "ticker": "NVDA", "action": "BUY", "position_size_usd": 500.0,
        "entry_price_max": 100.0, "demand_driver": "ai_compute_gpu",
        "plan": {"stop_loss": 90.0, "entry_price_max": 100.0},
    }
    i.update(over)
    return i


# ---------------------------------------------------------------------------
# reconstruction
# ---------------------------------------------------------------------------

def test_intent_to_proposal_recovers_the_book_relevant_fields():
    p = validator.intent_to_proposal(_intent())
    assert p["ticker"] == "NVDA" and p["action"] == "BUY"
    assert p["position_size_usd"] == 500.0
    assert p["entry_price_max"] == 100.0
    assert p["stop_loss"] == 90.0            # lifted out of `plan`
    assert p["demand_driver"] == "ai_compute_gpu"


def test_entry_falls_back_to_reference_price():
    i = _intent(entry_price_max=None, reference_price=101.5)
    i["plan"] = {"stop_loss": 90.0}
    assert validator.intent_to_proposal(i)["entry_price_max"] == 101.5


# ---------------------------------------------------------------------------
# the caps actually fire
# ---------------------------------------------------------------------------

def test_a_sane_buy_still_executes():
    """The gate must not simply block everything — that would be a kill switch."""
    assert validator.revalidate_intent_against_book(_intent(), _book(), {}) == []


def test_an_oversized_intent_is_stopped_at_the_relay():
    r = validator.revalidate_intent_against_book(
        _intent(position_size_usd=90_000), _book(), {})
    assert any("size_exceeds_max_usd" in x for x in r), r


def test_heat_is_recomputed_against_the_book_as_it_stands_now():
    """THE DRIFT CASE. The intent was fine when queued; the book moved."""
    heavy = [
        {"ticker": t, "market_value_usd": 1500, "avg_cost": 100.0, "quantity": 15,
         "original_plan": {"stop_loss": 88.0, "entry_price_max": 100.0},
         "demand_driver": f"driver_{i}"}
        for i, t in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])
    ]
    r = validator.revalidate_intent_against_book(
        _intent(position_size_usd=1500), _book(positions=heavy), {})
    assert r, "a book already carrying heavy committed risk must reject a new BUY"


def test_sells_are_never_blocked():
    """Risk reduction is never gated, matching every other check in validator.py."""
    assert validator.revalidate_intent_against_book(
        _intent(action="SELL_TO_CLOSE", position_size_usd=90_000), _book(), {}) == []


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("portfolio", [{}, {"total_equity_usd": 0}, None])
def test_unverifiable_book_fails_closed(portfolio):
    r = validator.revalidate_intent_against_book(_intent(), portfolio or {}, {})
    assert any("unverifiable" in x for x in r), r


def test_missing_size_fails_closed():
    r = validator.revalidate_intent_against_book(
        _intent(position_size_usd=None), _book(), {})
    assert any("unverifiable" in x for x in r), r


def test_missing_stop_does_not_crash():
    """No stop -> zero computable risk. Must not raise; heat simply cannot bind."""
    i = _intent()
    i["plan"] = {}
    assert isinstance(
        validator.revalidate_intent_against_book(i, _book(), {}), list)


# ---------------------------------------------------------------------------
# wired into the executor, not merely importable
# ---------------------------------------------------------------------------

def test_executor_calls_the_revalidation():
    """A check nobody calls is decoration — the failure this repo keeps hitting.
    reconciles_with_ledger sat tested-but-uncalled for three days."""
    src = (validator.ROOT / "scripts" / "execute_order_intents.py").read_text()
    assert "_book_revalidation_reasons(intent)" in src, (
        "the relay executor does not call the book revalidation")
    assert "revalidate_intent_against_book" in src


def test_executor_rejects_before_placing():
    """The rejection must short-circuit BEFORE place_order, not after."""
    src = (validator.ROOT / "scripts" / "execute_order_intents.py").read_text()
    gate = src.index("_book_revalidation_reasons(intent)")
    place = src.index("alpaca_broker.place_order(order)")
    assert gate < place, "book revalidation runs after the order is placed"
