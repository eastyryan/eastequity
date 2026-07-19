"""Two nodes reacting to the same stop breach must not sell twice.

THE HOLE (audited 2026-07-19)
-----------------------------
Every other order path in this repo is idempotent. Queued intents are re-placed
with their ORIGINAL client_order_id, Alpaca enforces client_order_id uniqueness,
and the 409 path resolves a duplicate submit to the existing broker order — so a
crashed or retried executor can never double-trade.

Forced exits built their order dict with NO client_order_id, so place_order
minted a fresh EE-{uuid4} on every call. The dedupe protecting every other path
did not cover them.

That matters because nothing serialises a cloud stop-watch against a local run:
stop_watch takes acquire_run_lock but never acquire_cross_node_lease, and the two
nodes hold separate locks on separate filesystems. Both read the same breach,
both fire, and with random ids they become two genuinely different market sells.
On a fractional or partially-filled position that OVER-SELLS — and _apply_fill's
broker-order-id dedupe cannot help, because the ids really are different.

Keyed on the trading DAY rather than the timestamp so two nodes reacting minutes
apart still collide, which is the entire point. A legitimate second forced exit
of the same name on the same day cannot occur: the first one closes the position.
"""

from __future__ import annotations

import re

from execution.exit_guard import forced_exit_client_order_id as cid


def test_same_breach_same_day_produces_one_id():
    """THE REGRESSION: two nodes, same breach, minutes apart."""
    a = cid("DELL", "stop_loss_breached", "2026-07-19")
    b = cid("DELL", "stop_loss_breached", "2026-07-19")
    assert a == b, "two nodes on the same breach must collide at the broker"


def test_id_is_stable_across_ticker_case_and_reason_formatting():
    assert cid("dell", "Stop_Loss_Breached", "2026-07-19") == \
           cid("DELL", "stop loss breached", "2026-07-19")


def test_different_days_are_different_orders():
    """A breach today and a breach tomorrow are genuinely different exits."""
    assert cid("DELL", "stop_loss_breached", "2026-07-19") != \
           cid("DELL", "stop_loss_breached", "2026-07-20")


def test_different_tickers_and_reasons_are_distinct():
    assert cid("DELL", "stop_loss_breached", "2026-07-19") != \
           cid("HPE", "stop_loss_breached", "2026-07-19")
    assert cid("DELL", "stop_loss_breached", "2026-07-19") != \
           cid("DELL", "horizon_expired", "2026-07-19")


def test_id_is_broker_safe():
    """Alpaca client_order_ids: no spaces, bounded length, printable ascii."""
    got = cid("DELL", "trailing stop breached (chandelier @ 3x ATR)", "2026-07-19")
    assert " " not in got
    assert len(got) <= 64
    assert re.fullmatch(r"[A-Za-z0-9\-]+", got), got


def test_empty_reason_still_yields_a_usable_id():
    got = cid("DELL", "", "2026-07-19")
    assert got.startswith("EEXIT-DELL-2026-07-19")
    assert not got.endswith("-")


def test_forced_exits_actually_pass_the_id():
    """A helper nobody wires in is decoration — reconciles_with_ledger sat
    tested-but-uncalled for three days."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "execution" / "exit_guard.py").read_text()
    assert "forced_exit_client_order_id(" in src
    order_block = src[src.index("def execute_forced_exits"):]
    place = order_block.index("broker.place_order(")
    nxt = order_block.index("})", place)
    assert "client_order_id" in order_block[place:nxt], (
        "execute_forced_exits does not pass a client_order_id, so place_order "
        "mints a fresh uuid and the cross-node dedupe does not cover it")
