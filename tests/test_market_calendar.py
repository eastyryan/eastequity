"""Real NYSE sessions, not a hardcoded Mon-Fri 09:30-16:00 assumption.

Every session boundary in the system was hardcoded and none knew about holidays or
half-days: the feeder gated on DOW<=5 && 0900<=HHMM<=1630, the live-prices workflow on
`*/5 13-21 * * 1-5` (UTC, so the ET window shifts an hour across both DST
transitions), and preflight on strftime("%a") -- whose own message claimed a "holiday
guard" that did not exist.
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import market_calendar as MC  # noqa: E402


def _fake_days(monkeypatch, days):
    monkeypatch.setattr(MC, "_days", lambda: days)


def test_a_half_day_close_is_read_from_the_calendar(monkeypatch):
    """2026-11-27 and 2026-12-24 close at 13:00. The old code treated 15:00 as live."""
    _fake_days(monkeypatch, {"2026-11-27": {"open": "09:30", "close": "13:00"}})
    s = MC.session(date(2026, 11, 27))
    assert s["is_trading_day"] and s["is_half_day"] and s["source"] == "calendar"
    assert str(s["close"]) == "13:00:00"


def test_after_a_half_day_close_the_market_is_shut(monkeypatch):
    _fake_days(monkeypatch, {"2026-11-27": {"open": "09:30", "close": "13:00"}})
    assert MC.is_market_open(datetime(2026, 11, 27, 12, 0, tzinfo=MC._ET)) is True
    assert MC.is_market_open(datetime(2026, 11, 27, 15, 0, tzinfo=MC._ET)) is False


def test_a_holiday_absent_from_the_calendar_is_not_a_trading_day(monkeypatch):
    """Christmas is a WEEKDAY. strftime("%a") says Friday; the market is shut."""
    _fake_days(monkeypatch, {"2026-12-24": {"open": "09:30", "close": "13:00"}})
    s = MC.session(date(2026, 12, 25))
    assert s["is_trading_day"] is False and s["source"] == "calendar"


def test_a_normal_session_is_still_a_normal_session(monkeypatch):
    """The mirror — without it, 'nothing is a trading day' passes the tests above."""
    _fake_days(monkeypatch, {"2026-07-20": {"open": "09:30", "close": "16:00"}})
    s = MC.session(date(2026, 7, 20))
    assert s["is_trading_day"] and not s["is_half_day"]
    assert MC.is_market_open(datetime(2026, 7, 20, 11, 0, tzinfo=MC._ET)) is True
    assert MC.is_market_open(datetime(2026, 7, 20, 8, 0, tzinfo=MC._ET)) is False


def test_no_calendar_falls_back_to_mon_fri_and_SAYS_it_is_guessing(monkeypatch):
    """Fail-SAFE, not fail-open. Refusing to trade because a calendar fetch failed
    would be worse than the bug — but a guess must never be reported as knowledge."""
    _fake_days(monkeypatch, {})
    weekday, weekend = MC.session(date(2026, 7, 20)), MC.session(date(2026, 7, 19))
    assert weekday["is_trading_day"] is True and weekday["source"] == "assumed"
    assert weekend["is_trading_day"] is False


def test_preflight_consults_the_calendar():
    """The wiring. Without this the module is a library nobody calls — which is the
    exact failure mode of the last three days."""
    import inspect
    from runlib import preflight
    src = inspect.getsource(preflight.preflight)
    assert "market_calendar" in src and "market holiday" in src


def test_a_failed_refresh_never_erases_a_good_calendar(monkeypatch, tmp_path):
    cache = tmp_path / "market_calendar.json"
    cache.write_text('{"built_at": "2020-01-01T00:00:00+00:00", "n": 1, '
                     '"days": {"2026-07-20": {"open": "09:30", "close": "16:00"}}}')
    monkeypatch.setattr(MC, "CACHE", cache)
    monkeypatch.setattr("execution.alpaca_broker._req", lambda *a, **k: (500, None))
    assert "2026-07-20" in MC.refresh(force=True)
