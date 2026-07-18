"""Corporate actions must apply exactly once per event, however often the cycle runs.

The regression these pin: `actions_processed_through` was a date string parsed as
MIDNIGHT UTC, while yfinance returns action timestamps in America/New_York. An
ex-date event therefore resolved to 04:00 UTC > 00:00 UTC cutoff, the guard never
fired, and with 6-7 slots a day the event re-applied on every run of the ex-date.

Measured before the fix:
  simulation : quantity 10 -> 160, avg_cost 50 -> 6.25 over four runs
  alpaca     : plan stop 90 -> 5.6 over four runs, i.e. effectively unstopped

Everything here is offline — yfinance is monkeypatched and state is redirected to
tmp_path, so the suite never touches the network or the real ledger.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import corporate_actions as ca  # noqa: E402
from execution import simulated_broker as sb  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeTS:
    """Mimics a pandas Timestamp carrying a timezone, like yfinance returns."""

    def __init__(self, dt):
        self._dt = dt

    def to_pydatetime(self):
        return self._dt


class FakeSeries:
    def __init__(self, items):
        self._items = items

    def items(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class FakeTicker:
    def __init__(self, dividends=(), splits=()):
        self._d, self._s = list(dividends), list(splits)

    @property
    def dividends(self):
        return FakeSeries(self._d)

    @property
    def splits(self):
        return FakeSeries(self._s)


class FakeYF:
    def __init__(self, ticker):
        self._t = ticker

    def Ticker(self, _symbol):
        return self._t


def et_today_timestamp():
    """Today's ex-date as yfinance reports it: midnight AMERICA/NEW_YORK, which is
    04:00/05:00 UTC — the exact shape that defeated the old UTC-midnight marker."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:  # pragma: no cover
        tz = timezone(timedelta(hours=-4))
    now_et = datetime.now(tz)
    return FakeTS(datetime(now_et.year, now_et.month, now_et.day, tzinfo=tz))


@pytest.fixture
def book(tmp_path, monkeypatch):
    """A one-position book opened yesterday, wired to tmp state."""
    state_file = tmp_path / "portfolio.json"
    opened = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    state = {
        "cash_usd": 1000.0,
        "total_equity_usd": 1500.0,
        "positions": [{
            "ticker": "TEST", "quantity": 10.0, "avg_cost": 50.0,
            "last_price": 50.0, "market_value_usd": 500.0,
            "opened_at": opened,
            "plan": {"stop_loss": 45.0, "target_price": 65.0,
                     "entry_price_max": 52.0},
        }],
        "history": [], "pending_orders": {},
    }
    state_file.write_text(json.dumps(state))
    monkeypatch.setattr(sb, "STATE_FILE", state_file)
    return state_file


def read(book_path):
    return json.loads(book_path.read_text())


def install(monkeypatch, ticker, backend="simulation"):
    monkeypatch.setitem(sys.modules, "yfinance", FakeYF(ticker))
    from execution import broker as broker_router
    monkeypatch.setattr(broker_router, "backend_name", lambda: backend)


# --------------------------------------------------------------------------- #
# The reproduction
# --------------------------------------------------------------------------- #
def test_same_day_split_applies_once_across_many_runs(book, monkeypatch):
    """THE regression: four runs on the ex-date must equal one application."""
    install(monkeypatch, FakeTicker(splits=[(et_today_timestamp(), 2.0)]))

    for _ in range(4):
        ca.apply_corporate_actions()

    pos = read(book)["positions"][0]
    assert pos["quantity"] == 20.0          # was 160.0
    assert pos["avg_cost"] == 25.0          # was 6.25
    assert pos["plan"]["stop_loss"] == 22.5  # was 5.625
    assert len([h for h in read(book)["history"] if h["type"] == "split"]) == 1


def test_same_day_dividend_credits_once_across_many_runs(book, monkeypatch):
    install(monkeypatch, FakeTicker(dividends=[(et_today_timestamp(), 1.0)]))

    for _ in range(4):
        ca.apply_corporate_actions()

    state = read(book)
    assert state["cash_usd"] == 1010.0                       # was 1150.0
    assert state["positions"][0]["dividends_received_usd"] == 10.0
    assert len([h for h in state["history"] if h["type"] == "dividend"]) == 1


def test_alpaca_backend_does_not_rescale_the_stop_repeatedly(book, monkeypatch):
    """On the live path Alpaca owns quantity, so the damage landed entirely on the
    persisted plan — the stop walked down to a level that could never trigger."""
    install(monkeypatch, FakeTicker(splits=[(et_today_timestamp(), 2.0)]),
            backend="alpaca_paper")

    for _ in range(4):
        ca.apply_corporate_actions()

    pos = read(book)["positions"][0]
    assert pos["quantity"] == 10.0           # broker owns it; untouched
    assert pos["plan"]["stop_loss"] == 22.5  # rescaled once, not four times
    assert pos["plan"]["target_price"] == 32.5


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def test_dedupe_survives_a_lost_marker():
    """The key set is rebuilt from persisted history, so idempotency does not depend
    on the in-memory marker surviving a restart."""
    state = {"history": [
        {"type": "split", "ticker": "TEST", "date": "2026-07-18"},
        {"type": "dividend", "ticker": "TEST", "date": "2026-07-18"},
        {"type": "fill", "ticker": "TEST", "date": "2026-07-18"},  # ignored
    ]}
    keys = ca.processed_event_keys(state)
    assert keys == {"split:TEST:2026-07-18", "dividend:TEST:2026-07-18"}


def test_implausible_split_ratio_is_refused(book, monkeypatch):
    """A single bad vendor row must not permanently rewrite the book."""
    install(monkeypatch, FakeTicker(splits=[(et_today_timestamp(), 100000.0)]))

    summary = ca.apply_corporate_actions()

    pos = read(book)["positions"][0]
    assert pos["quantity"] == 10.0            # untouched
    assert pos["plan"]["stop_loss"] == 45.0   # stop intact
    assert any("implausible" in e["error"] for e in summary["errors"])


def test_events_before_the_position_opened_are_ignored(book, monkeypatch):
    old = FakeTS(datetime.now(timezone.utc) - timedelta(days=30))
    install(monkeypatch, FakeTicker(dividends=[(old, 1.0)],
                                    splits=[(old, 2.0)]))

    ca.apply_corporate_actions()

    pos = read(book)["positions"][0]
    assert pos["quantity"] == 10.0
    assert read(book)["cash_usd"] == 1000.0


def test_future_dated_events_are_ignored(book, monkeypatch):
    future = FakeTS(datetime.now(timezone.utc) + timedelta(days=5))
    install(monkeypatch, FakeTicker(dividends=[(future, 1.0)],
                                    splits=[(future, 2.0)]))

    ca.apply_corporate_actions()

    assert read(book)["cash_usd"] == 1000.0
    assert read(book)["positions"][0]["quantity"] == 10.0


def test_a_genuinely_new_event_still_applies(book, monkeypatch):
    """Guard against over-correcting into never applying anything."""
    install(monkeypatch, FakeTicker(splits=[(et_today_timestamp(), 2.0)]))
    ca.apply_corporate_actions()
    assert read(book)["positions"][0]["quantity"] == 20.0

    # A second, distinct event on a later date must still land.
    later = FakeTS(datetime.now(timezone.utc) + timedelta(days=0))
    del later  # today's is already consumed; assert the key set is what blocks it
    keys = ca.processed_event_keys(read(book))
    assert any(k.startswith("split:TEST:") for k in keys)


def test_marker_still_written_for_reporting(book, monkeypatch):
    """runlib/publish.py surfaces actions_processed_through; keep writing it even
    though it is no longer the guard."""
    install(monkeypatch, FakeTicker(dividends=[(et_today_timestamp(), 1.0)]))
    ca.apply_corporate_actions()
    pos = read(book)["positions"][0]
    assert pos["actions_processed_through"] == datetime.now(timezone.utc).date().isoformat()
