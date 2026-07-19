"""Learning artifacts must reconcile with the ledger, or they are studying fiction.

THE INCIDENT (2026-07-19). data/post_exit_runners.json carried a committed HPE
record in which every number contradicted the real exit autopsy:

    record : exit_price 40.00  realized_pnl -50.00  days_held 20  stop 41.00
    reality: exit_price 46.08  realized_pnl -45.33  days_held  4  stop 43.75

It was re-marked on every run for three days and the runner-learning loop computed
current_leftover_pct against it — measuring upside left on the table by a trade that
never happened. No code anywhere compared a learning artifact to the ledger, so
nothing could notice. Fabricated inputs are worse than missing ones: the loop
reported healthy the entire time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.post_exit_runners import reconciles_with_ledger  # noqa: E402


REAL_CLOSES = [
    {"ticker": "DELL", "action": "SELL_TO_CLOSE", "realized_pnl_usd": -135.19},
    {"ticker": "HPE", "action": "SELL_TO_CLOSE", "realized_pnl_usd": -45.33},
]


def test_the_actual_bogus_record_is_rejected():
    """Verbatim from the file as committed. This is the regression."""
    bogus = {"ticker": "HPE", "exit_price": 40.0, "realized_pnl_usd": -50.0,
             "days_held": 20}
    assert reconciles_with_ledger(bogus, REAL_CLOSES) is False


def test_the_real_trade_reconciles():
    """The mirror — without it, 'reject everything' passes the test above and the
    guard would silently delete every genuine tracking record."""
    real = {"ticker": "HPE", "exit_price": 46.082, "realized_pnl_usd": -45.33}
    assert reconciles_with_ledger(real, REAL_CLOSES) is True


def test_a_ticker_that_was_never_traded_is_rejected():
    assert reconciles_with_ledger(
        {"ticker": "TSLA", "realized_pnl_usd": -50.0}, REAL_CLOSES) is False


def test_small_rounding_differences_still_reconcile():
    """Fees and rounding move the last cent; the guard must not be brittle."""
    assert reconciles_with_ledger(
        {"ticker": "DELL", "realized_pnl_usd": -135.60}, REAL_CLOSES) is True


def test_no_ledger_means_no_verdict_not_a_deletion():
    """A reconciliation failure must never destroy real tracking data. With nothing
    to compare against, the honest answer is 'unfalsified', not 'fake'."""
    assert reconciles_with_ledger({"ticker": "HPE", "realized_pnl_usd": -50.0},
                                  []) is True


def test_a_record_claiming_no_pnl_is_not_falsifiable():
    assert reconciles_with_ledger({"ticker": "HPE"}, REAL_CLOSES) is True


def test_a_record_with_no_ticker_is_rejected():
    assert reconciles_with_ledger({"realized_pnl_usd": -45.33}, REAL_CLOSES) is False


def test_the_live_tracking_file_reconciles_today():
    """Guards the real artifact, so this class of pollution cannot come back
    unnoticed the way it did for three days."""
    import json
    p = Path(__file__).resolve().parent.parent / "data" / "post_exit_runners.json"
    if not p.exists():
        return
    from execution import simulated_broker
    state = simulated_broker._load()
    closed = [h for h in (state.get("history") or [])
              if "SELL" in str(h.get("action", "")).upper()]
    if not closed:
        return
    for rec in (json.loads(p.read_text()).get("tracking") or []):
        assert reconciles_with_ledger(rec, closed), rec
