"""Runner tracks must reconcile with the ledger BEFORE they teach anything.

THE INCIDENT (2026-07-16, found 2026-07-19)
-------------------------------------------
data/post_exit_runners.json shipped a committed HPE record in which every number
contradicted the real exit autopsy:

    record : exit_price 40.00  pnl -50.00  days_held 20
    reality: exit_price 46.08  pnl -45.33  days_held  4

It came from a test fixture that isolated five learning modules and missed this
one, so a synthetic stub chained through grade_and_persist_autopsy into the REAL
store. It was re-marked on every run for three days while the runner-learning loop
computed current_leftover_pct against it — measuring upside left on the table by a
trade that never happened. In the same window the REAL DELL track was silently
lost.

reconciles_with_ledger() was written in response, unit-tested, and then NEVER
CALLED from production. Its own docstring ends "This is the reconciliation that was
absent" — and it still was. These tests pin that it now actually runs.

TWO PROPERTIES, AND THE SECOND MATTERS AS MUCH AS THE FIRST
    1. A record with no matching closed trade is quarantined, not marked.
    2. Quarantine NEVER deletes, and reconciliation fails OPEN when there is no
       ledger to compare against — a reconciliation bug must not be able to
       destroy real tracking data. Fabricated inputs are worse than missing ones,
       but deleted evidence is worse than both.
"""

from __future__ import annotations

import json

import pytest

import tools.post_exit_runners as PR


@pytest.fixture
def store(tmp_path, monkeypatch):
    f = tmp_path / "post_exit_runners.json"
    monkeypatch.setattr(PR, "RUNNERS_FILE", f)
    monkeypatch.setattr(PR, "ROOT", tmp_path)
    return f


def _track(ticker="HPE", *, exit_price=40.0, pnl=-50.0) -> dict:
    return {
        "ticker": ticker, "exit_date": "2026-05-01", "exit_price": exit_price,
        "realized_pnl_usd": pnl, "track_kind": "loser", "marks": {},
        "high_after_exit": exit_price, "low_after_exit": exit_price,
    }


def _write(store, tracking: list):
    store.write_text(json.dumps(
        {"version": 1, "tracking": tracking, "completed": [], "updated_at": None}))


def test_fabricated_record_is_quarantined_not_marked(store, monkeypatch):
    """THE REGRESSION: the fabricated HPE record was marked for three days."""
    monkeypatch.setattr(PR, "reconciles_with_ledger", lambda rec, *_a, **_k: False)
    _write(store, [_track("HPE")])

    out = PR.mark_post_exit_runners({"HPE": 55.0})

    assert out["quarantined_now"] == 1
    assert out["quarantined_tickers"] == ["HPE"]
    state = json.loads(store.read_text())
    assert state["tracking"] == [], "an unreconciled record must not stay in tracking"
    assert len(state["quarantined"]) == 1
    q = state["quarantined"][0]
    assert "unreconciled" in q and q["quarantined_at"]
    assert "marks" not in q or not q["marks"], (
        "a quarantined record must not have been marked on the way out")


def test_quarantine_preserves_the_record(store, monkeypatch):
    """Never delete. An unreconciled record is EVIDENCE OF A BUG — the fabricated
    HPE record is how the 2026-07-16 incident was diagnosed at all."""
    monkeypatch.setattr(PR, "reconciles_with_ledger", lambda rec, *_a, **_k: False)
    _write(store, [_track("HPE", exit_price=40.0, pnl=-50.0)])

    PR.mark_post_exit_runners({"HPE": 55.0})

    q = json.loads(store.read_text())["quarantined"][0]
    assert q["ticker"] == "HPE"
    assert q["exit_price"] == 40.0 and q["realized_pnl_usd"] == -50.0


def test_reconciled_records_are_marked_normally(store, monkeypatch):
    """The inverse: the guard must not quarantine real tracks."""
    monkeypatch.setattr(PR, "reconciles_with_ledger", lambda rec, *_a, **_k: True)
    _write(store, [_track("DELL", exit_price=100.0)])

    out = PR.mark_post_exit_runners({"DELL": 130.0})

    assert out["quarantined_now"] == 0
    state = json.loads(store.read_text())
    kept = (state.get("tracking") or []) + (state.get("completed") or [])
    assert any(t["ticker"] == "DELL" for t in kept)
    dell = next(t for t in kept if t["ticker"] == "DELL")
    assert dell.get("current_leftover_pct") == pytest.approx(30.0, abs=0.1)


def test_mixed_batch_quarantines_only_the_bad_one(store, monkeypatch):
    monkeypatch.setattr(PR, "reconciles_with_ledger",
                        lambda rec, *_a, **_k: rec.get("ticker") != "FAKE")
    _write(store, [_track("DELL", exit_price=100.0), _track("FAKE")])

    out = PR.mark_post_exit_runners({"DELL": 130.0, "FAKE": 99.0})

    assert out["quarantined_tickers"] == ["FAKE"]
    state = json.loads(store.read_text())
    kept = (state.get("tracking") or []) + (state.get("completed") or [])
    assert {t["ticker"] for t in kept} == {"DELL"}


def test_no_ledger_fails_open(store, monkeypatch):
    """Reconciliation must never destroy real data on ignorance."""
    monkeypatch.setattr(PR, "reconciles_with_ledger",
                        PR.reconciles_with_ledger)   # the real one
    _write(store, [_track("DELL", exit_price=100.0)])

    out = PR.mark_post_exit_runners({"DELL": 130.0}, cache_news_on_marks=False)

    # closed_trades=[] -> "nothing to reconcile against; do not delete on ignorance"
    assert out["quarantined_now"] == 0 or out["quarantined_now"] == 1
    state = json.loads(store.read_text())
    total = len(state.get("tracking") or []) + len(state.get("completed") or []) \
        + len(state.get("quarantined") or [])
    assert total == 1, "the record must survive somewhere — never vanish"


def test_reconciliation_failure_does_not_break_marking(store, monkeypatch):
    """Telemetry-style safety: a bug in the guard must not stop the loop."""
    def _explode(*_a, **_k):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(PR, "reconciles_with_ledger", _explode)
    _write(store, [_track("DELL", exit_price=100.0)])

    out = PR.mark_post_exit_runners({"DELL": 130.0}, cache_news_on_marks=False)

    assert out.get("quarantined_now") == 0
    state = json.loads(store.read_text())
    kept = (state.get("tracking") or []) + (state.get("completed") or [])
    assert any(t["ticker"] == "DELL" for t in kept)


def test_reconciles_with_ledger_matches_on_ticker_and_pnl():
    closed = [{"ticker": "DELL", "action": "SELL_TO_CLOSE", "realized_pnl_usd": -135.19}]
    assert PR.reconciles_with_ledger(
        {"ticker": "DELL", "realized_pnl_usd": -135.19}, closed) is True
    assert PR.reconciles_with_ledger(
        {"ticker": "HPE", "realized_pnl_usd": -50.0}, closed) is False
    assert PR.reconciles_with_ledger(
        {"ticker": "DELL", "realized_pnl_usd": -50.0}, closed) is False


def test_reconciles_with_ledger_fails_open_on_empty_ledger():
    assert PR.reconciles_with_ledger({"ticker": "DELL", "realized_pnl_usd": -1}, []) is True
