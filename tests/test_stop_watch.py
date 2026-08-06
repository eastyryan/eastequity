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


def write_snapshot(tmp_path, monkeypatch, prices, age_min=1.0, via="latest_trade",
                   trade_age_min=None):
    """A snapshot in the shape production now writes.

    price_meta carries the TRADE time and the ladder rung per ticker. It is not
    optional decoration: stop_watch refuses to enforce a stop off a price whose
    provenance it cannot establish, because a prior-session close used to be stamped
    with a fresh wall clock and read as age 0.0m. Defaults here describe a healthy
    live tick; pass `via`/`trade_age_min` to describe a stale or non-intraday one.
    """
    now = datetime.now(timezone.utc)
    as_of = (now - timedelta(minutes=age_min)).isoformat()
    t_age = age_min if trade_age_min is None else trade_age_min
    traded = (now - timedelta(minutes=t_age)).isoformat()
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    (d / "live_prices.json").write_text(json.dumps({
        "as_of": as_of, "source": "test", "prices": prices, "n": len(prices),
        "price_meta": {str(t).upper(): {"asof": traded, "via": via} for t in prices},
    }))
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
    # release_run_lock now takes the owning run_id (ownership-checked release,
    # 2026-08-05) and the maintenance pass takes its own lock before the exec
    # lock — so assert acquire/release PAIRING rather than a fixed count.
    acquired = {"n": 0}

    def fake_acquire(rid):
        acquired.update(n=acquired["n"] + 1)
        return True

    monkeypatch.setattr(stop_watch.preflight, "acquire_run_lock", fake_acquire)
    released = {"n": 0}
    monkeypatch.setattr(stop_watch.preflight, "release_run_lock",
                        lambda *a, **k: released.update(n=released["n"] + 1))

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(stop_watch.exit_guard, "execute_forced_exits", boom)

    with pytest.raises(RuntimeError):
        stop_watch.run(dry_run=False)
    assert released["n"] == acquired["n"] >= 1


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


# --------------------------------------------------------------------------- #
# Trade-time freshness (2026-07-19). Freshness used to be measured on SNAPSHOT
# time only — how long ago the feeder RAN — so a prevDailyBar close from a prior
# session was written with a fresh wall clock and read as age 0.0m, indistinguishable
# from a live tick. The failure is silent and directional: the stale price is the
# PRE-selloff price, it sits above the stop, and the stop does not fire.
# --------------------------------------------------------------------------- #
def test_a_prior_session_close_cannot_decide_a_stop(tmp_path, monkeypatch):
    """dailyBar/prevDailyBar are a session CLOSE, not an intraday mark."""
    write_snapshot(tmp_path, monkeypatch, {"DELL": 400.0}, age_min=1.0,
                   via="prev_daily_bar")
    prices, meta = stop_watch._live_prices(30)
    assert prices == {}
    assert meta["n_rejected"] == 1


def test_a_fresh_snapshot_carrying_a_stale_trade_is_rejected(tmp_path, monkeypatch):
    """THE bug: the feeder ran 1 minute ago, but the last IEX print is 90 minutes
    old. On a thin name that is routine — IEX is ~2-3% of consolidated volume."""
    write_snapshot(tmp_path, monkeypatch, {"DELL": 400.0}, age_min=1.0,
                   trade_age_min=90.0)
    prices, meta = stop_watch._live_prices(30)
    assert prices == {}
    assert "90m old" in meta["rejected_prices"][0]


def test_a_genuine_live_tick_is_enforceable(tmp_path, monkeypatch):
    """The mirror — without it, 'reject everything' passes both tests above and no
    stop ever fires."""
    write_snapshot(tmp_path, monkeypatch, {"DELL": 400.0}, age_min=1.0)
    prices, meta = stop_watch._live_prices(30)
    assert prices == {"DELL": 400.0}
    assert meta.get("n_rejected") is None


def test_missing_provenance_fails_closed(tmp_path, monkeypatch):
    """An old-format snapshot with no price_meta must not be trusted."""
    import json as _j
    snap = write_snapshot(tmp_path, monkeypatch, {"DELL": 400.0}, age_min=1.0)
    blob = _j.loads(snap.read_text()); blob.pop("price_meta")
    snap.write_text(_j.dumps(blob))
    prices, _ = stop_watch._live_prices(30)
    assert prices == {}
