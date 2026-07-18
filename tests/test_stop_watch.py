"""The between-cycle stop watcher must fire on a real breach and stay inert otherwise.

Closes the INTRADAY half of the stop-enforcement gap. Stops here are numbers in
state/portfolio.json, not resting broker orders, so enforcement only happened when a
full cycle ran — leaving ~2h intraday windows (and ~17.5h overnight, ~65h weekends,
which this does NOT address and cannot without resting orders).

Everything offline: no network, no LLM, tmp state.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import stop_watch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def write_snapshot(tmp_path, monkeypatch, prices, age_min=1.0):
    as_of = (datetime.now(timezone.utc) - timedelta(minutes=age_min)).isoformat()
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    (d / "live_prices.json").write_text(json.dumps(
        {"as_of": as_of, "source": "test", "prices": prices, "n": len(prices)}))
    monkeypatch.setattr(stop_watch, "ROOT", tmp_path)
    return d / "live_prices.json"


def book(last=90.0, stop=95.0):
    """One position whose plan stop sits ABOVE the current mark → a breach."""
    return {"positions": [{
        "ticker": "AAA", "quantity": 10.0, "avg_cost": 100.0,
        "market_value_usd": last * 10,
        "opened_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        "plan": {"stop_loss": stop, "target_price": 130.0,
                 "holding_horizon_days": 30},
        "original_plan": {"stop_loss": stop, "target_price": 130.0,
                          "holding_horizon_days": 30},
        "days_held": 3,
    }]}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    cfg = {"mode": {"trading_mode": "paper"},
           "schedule": {"live_prices": {"max_age_min": 30}}}
    monkeypatch.setattr(stop_watch, "_cfg", lambda: cfg)
    return cfg


def test_fires_on_a_genuine_breach(tmp_path, monkeypatch):
    """THE point of the watcher: a stop breached between cycles is acted on now."""
    write_snapshot(tmp_path, monkeypatch, {"AAA": 90.0})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book(stop=95.0))

    res = stop_watch.run(dry_run=True)

    assert res["status"] == "dry_run"
    assert len(res["forced"]) == 1
    assert res["forced"][0]["ticker"] == "AAA"
    assert res["forced"][0]["reason"] in ("stop_loss_breached", "trailing_stop_breached")


def test_silent_when_the_stop_is_not_breached(tmp_path, monkeypatch):
    write_snapshot(tmp_path, monkeypatch, {"AAA": 110.0})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book(stop=95.0))

    res = stop_watch.run(dry_run=True)
    assert res["status"] == "ok"
    assert res["forced"] == []


def test_refuses_to_act_on_a_stale_mark(tmp_path, monkeypatch):
    """Enforcing against a stale prior close is worse than waiting — a stop fired on
    yesterday's price is an invented exit. The next tick is minutes away."""
    write_snapshot(tmp_path, monkeypatch, {"AAA": 90.0}, age_min=240.0)
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book(stop=95.0))

    res = stop_watch.run(dry_run=True)
    assert res["status"] == "skipped"
    assert res["price_meta"]["status"] == "stale"
    assert res["forced"] == []


def test_skips_when_no_snapshot_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(stop_watch, "ROOT", tmp_path)
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book())
    res = stop_watch.run(dry_run=True)
    assert res["status"] == "skipped"
    assert res["price_meta"]["status"] == "no_snapshot"


def test_skips_a_flat_book(tmp_path, monkeypatch):
    write_snapshot(tmp_path, monkeypatch, {"AAA": 90.0})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: {"positions": []})
    assert stop_watch.run(dry_run=True)["status"] == "skipped"


def test_skips_when_the_held_name_has_no_fresh_price(tmp_path, monkeypatch):
    write_snapshot(tmp_path, monkeypatch, {"ZZZ": 5.0})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book())
    res = stop_watch.run(dry_run=True)
    assert res["status"] == "skipped"
    assert "no fresh price" in res["note"]


def test_defers_when_a_trading_cycle_holds_the_lock(tmp_path, monkeypatch):
    """A concurrent cycle mutating the ledger would race — and it enforces stops
    itself, so standing down loses nothing."""
    write_snapshot(tmp_path, monkeypatch, {"AAA": 90.0})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book(stop=95.0))
    monkeypatch.setattr(stop_watch.preflight, "acquire_run_lock", lambda rid: False)

    called = {"n": 0}
    monkeypatch.setattr(stop_watch.exit_guard, "execute_forced_exits",
                        lambda *a, **k: called.update(n=called["n"] + 1) or [])

    res = stop_watch.run(dry_run=False)
    assert res["status"] == "deferred"
    assert called["n"] == 0, "must not execute while another run holds the lock"


def test_releases_the_lock_even_if_execution_raises(tmp_path, monkeypatch):
    """A watcher that leaks the lock would block every subsequent trading cycle
    for LOCK_STALE_SECONDS."""
    write_snapshot(tmp_path, monkeypatch, {"AAA": 90.0})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book(stop=95.0))
    monkeypatch.setattr(stop_watch.preflight, "acquire_run_lock", lambda rid: True)
    released = {"n": 0}
    monkeypatch.setattr(stop_watch.preflight, "release_run_lock",
                        lambda: released.update(n=released["n"] + 1))

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(stop_watch.exit_guard, "execute_forced_exits", boom)

    with pytest.raises(RuntimeError):
        stop_watch.run(dry_run=False)
    assert released["n"] == 1


def test_dry_run_never_places_an_order(tmp_path, monkeypatch):
    write_snapshot(tmp_path, monkeypatch, {"AAA": 90.0})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book(stop=95.0))
    called = {"n": 0}
    monkeypatch.setattr(stop_watch.exit_guard, "execute_forced_exits",
                        lambda *a, **k: called.update(n=called["n"] + 1) or [])
    stop_watch.run(dry_run=True)
    assert called["n"] == 0


def test_respects_dry_run_trading_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(stop_watch, "_cfg",
                        lambda: {"mode": {"trading_mode": "dry_run"}, "schedule": {}})
    monkeypatch.setattr(stop_watch, "get_portfolio_state", lambda: book())
    assert stop_watch.run()["status"] == "skipped"


# --------------------------------------------------------------------------- #
# Structural guarantees
# --------------------------------------------------------------------------- #
def test_watcher_never_opens_a_position():
    """There must be no path here that increases risk."""
    src = (ROOT / "scripts" / "stop_watch.py").read_text()
    code = src.split('"""', 2)[-1]  # strip the module docstring
    assert '"BUY"' not in code and "place_order" not in code


def test_watcher_invokes_no_llm():
    """Pure Python — costs no usage and cannot be affected by model availability."""
    src = (ROOT / "scripts" / "stop_watch.py").read_text()
    code = src.split('"""', 2)[-1]
    for token in ("claude", "ask_claude", "run_claude", "brain_io"):
        assert token not in code, f"stop watcher must not reach the LLM ({token})"
