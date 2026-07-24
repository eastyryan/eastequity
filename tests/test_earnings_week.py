"""earnings_week(): the universe-wide 'who reports this week' schedule.

Guards the gap this block was added to close (2026-07-23): earnings reached the brain
only through the scanner's per-ticker enrichment, capped at ~30 names a run, so ~157 of
184 universe names arrived with no earnings clock at all. The calendar already knew all
of them; nothing surfaced it as a schedule.

Pure over an injected by_ticker map and an injected date — no network, no clock, no IO.
"""
from datetime import date

from tools.earnings_calendar import earnings_week

TODAY = date(2026, 7, 23)

CAL = {
    # today, both sessions
    "RTX": {"next": "2026-07-23T07:00:00-04:00", "next_when": "bmo"},
    "DLR": {"next": "2026-07-23T16:00:00-04:00", "next_when": "amc"},
    # inside the window
    "MSFT": {"next": "2026-07-29T16:00:00-04:00", "next_when": "amc"},
    "AAPL": {"next": "2026-07-30T16:00:00-04:00", "next_when": "amc"},
    # boundary: exactly the horizon edge (today + 7)
    "EDGE": {"next": "2026-07-30T08:00:00-04:00", "next_when": "bmo"},
    # outside the window
    "LATER": {"next": "2026-08-15T08:00:00-04:00", "next_when": "bmo"},
    # already reported — must not appear as upcoming
    "PAST": {"next": "2026-07-01T08:00:00-04:00", "next_when": "bmo"},
    # no scheduled print
    "NONE": {"next": None, "next_when": None},
    # malformed date must be skipped, not crash the block
    "JUNK": {"next": "not-a-date", "next_when": "bmo"},
}


def _tickers(block, day):
    return [r["ticker"] for r in block["by_date"].get(day, [])]


def test_today_is_included():
    """A print happening TODAY is the most decision-relevant one there is.

    The whole reason this exists: days_to_earnings went NULL on the day of the print
    (calendar dates parse to midnight, so `nxt > now` dropped them), which reads as
    'no print imminent' — the exact inverse of the truth.
    """
    w = earnings_week(CAL, today=TODAY, days=7)
    assert _tickers(w, "2026-07-23") == ["RTX", "DLR"]      # bmo before amc
    assert [r["ticker"] for r in w["today"]] == ["RTX", "DLR"]


def test_window_bounds_are_inclusive_and_exclude_outside():
    w = earnings_week(CAL, today=TODAY, days=7)
    assert w["through"] == "2026-07-30"
    assert "EDGE" in _tickers(w, "2026-07-30")             # horizon edge included
    assert "LATER" not in {r["ticker"] for d in w["by_date"].values() for r in d}
    assert "PAST" not in {r["ticker"] for d in w["by_date"].values() for r in d}


def test_bad_and_missing_dates_are_skipped_not_fatal():
    """A malformed row must not take the schedule down — losing the whole block
    would put us back to 'no earnings data', the failure this replaces."""
    w = earnings_week(CAL, today=TODAY, days=7)
    names = {r["ticker"] for d in w["by_date"].values() for r in d}
    assert "JUNK" not in names and "NONE" not in names
    assert w["count"] == 5                                  # RTX DLR MSFT AAPL EDGE


def test_covers_every_name_not_just_a_focus_subset():
    """The point of the block: it is NOT limited to the ~30 enriched names."""
    w = earnings_week(CAL, today=TODAY, days=7)
    assert {"MSFT", "AAPL"} <= {r["ticker"] for d in w["by_date"].values() for r in d}


def test_universe_filter_applies_when_given():
    w = earnings_week(CAL, today=TODAY, days=7, universe=["RTX", "MSFT"])
    names = {r["ticker"] for d in w["by_date"].values() for r in d}
    assert names == {"RTX", "MSFT"}


def test_sessions_ordered_bmo_then_amc_then_unknown():
    cal = {"Z": {"next": "2026-07-24T00:00:00-04:00", "next_when": "unknown"},
           "A": {"next": "2026-07-24T16:00:00-04:00", "next_when": "amc"},
           "M": {"next": "2026-07-24T07:00:00-04:00", "next_when": "bmo"}}
    w = earnings_week(cal, today=TODAY, days=7)
    assert _tickers(w, "2026-07-24") == ["M", "A", "Z"]


def test_empty_calendar_reports_zero_not_an_exception():
    w = earnings_week({}, today=TODAY, days=7)
    assert w["count"] == 0 and w["by_date"] == {} and w["today"] == []
