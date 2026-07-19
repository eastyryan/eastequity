"""Entries rest at the broker and carry their own stop when whole shares allow it.

Alpaca's fractional/notional orders are time_in_force=DAY only and accept no
order_class, so a notional entry can neither rest overnight nor carry a
broker-managed stop. A whole-share qty order can do both, which is what makes
event-driven entry possible without a local watcher: the limit rests until price
reaches the level and the exchange fills it with no run in progress.

OTO rather than bracket is deliberate — a bracket demands a take-profit leg, and
targets here are explicitly milestones, not tripwires.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.alpaca_broker import MIN_WHOLE_SHARES_FOR_LIMIT, _buy_body  # noqa: E402


def order(entry=100.0, stop=92.0):
    return {"entry_price_max": entry, "plan": {"stop_loss": stop}}


def test_a_normal_entry_rests_as_a_gtc_limit_with_an_attached_stop():
    o = order(entry=100.0, stop=92.0)
    body = _buy_body("NVDA", 1400.0, o, "EE-1")          # 14 whole shares
    assert body["type"] == "limit"
    assert body["time_in_force"] == "gtc"                 # survives overnight
    assert body["qty"] == "14"
    assert body["limit_price"] == "100.00"
    assert body["order_class"] == "oto"
    assert body["stop_loss"] == {"stop_price": "92.00"}
    assert "notional" not in body
    assert o["entry_mode"] == "whole_share_limit_oto"


def test_no_take_profit_leg_is_ever_attached():
    """A binding take-profit would contradict 'the target is a milestone, not a
    tripwire' and cap the right tail the strategy depends on."""
    body = _buy_body("NVDA", 1400.0, order(), "EE-1")
    assert "take_profit" not in body
    assert body["order_class"] != "bracket"


def test_a_high_priced_name_falls_back_to_notional():
    """MELI at ~$1,814 buys 0 whole shares at a $1,400 position. It must stay
    tradeable rather than become unbuyable."""
    o = order(entry=1814.0, stop=1650.0)
    body = _buy_body("MELI", 1400.0, o, "EE-1")
    assert body["type"] == "market" and body["time_in_force"] == "day"
    assert body["notional"] == "1400.00"
    assert o["entry_mode"] == "notional_market"
    assert "0 whole share" in o["entry_fallback_reason"]


def test_the_granularity_threshold_is_respected_on_both_sides():
    at = MIN_WHOLE_SHARES_FOR_LIMIT * 100.0          # exactly the threshold
    assert _buy_body("X", at, order(entry=100.0), "1")["type"] == "limit"
    below = (MIN_WHOLE_SHARES_FOR_LIMIT - 1) * 100.0
    assert _buy_body("X", below, order(entry=100.0), "1")["type"] == "market"


def test_a_missing_stop_falls_back_rather_than_resting_unprotected():
    """An OTO with no stop leg would rest an entry that fills into no protection."""
    o = {"entry_price_max": 100.0, "plan": {}}
    body = _buy_body("NVDA", 1400.0, o, "EE-1")
    assert body["type"] == "market"
    assert o["entry_fallback_reason"] == "no plan stop"


def test_a_missing_entry_ceiling_falls_back():
    o = {"plan": {"stop_loss": 92.0}}
    body = _buy_body("NVDA", 1400.0, o, "EE-1")
    assert body["type"] == "market"
    assert o["entry_fallback_reason"] == "no entry_price_max"


def test_an_inverted_stop_never_produces_a_resting_order():
    """stop above the limit would arm a stop that triggers instantly on fill."""
    o = order(entry=100.0, stop=105.0)
    assert _buy_body("NVDA", 1400.0, o, "EE-1")["type"] == "market"


def test_the_chosen_shape_is_always_recorded():
    """A position must never be ambiguous about what is protecting it."""
    for notional, entry in ((1400.0, 100.0), (1400.0, 1814.0)):
        o = order(entry=entry)
        _buy_body("X", notional, o, "1")
        assert o.get("entry_mode") in ("whole_share_limit_oto", "notional_market")
