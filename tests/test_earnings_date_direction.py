"""`next_earnings` must never be a date that has already passed.

THE BUG (2026-07-19). The digest sourced next_earnings from the news scrape, which
reads yfinance's `tk.calendar["Earnings Date"]` and takes element [0] — a list that
happily leads with a date already in the past. ZS reported next_earnings 2026-05-26
on 2026-07-19: its LAST print, 54 days gone. Meanwhile days_to_earnings said 45,
computed from the calendar's real `next` (2026-09-02), so two fields on the same
card disagreed about the same event.

That is not cosmetic. CLAUDE.md instructs the brain to choose the earnings path
"through binary vs around it" and to "respect the binary-print problem". A past date
presented as the next print says a catalyst is coming that already happened.
"""

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib.context_gather import _earnings_next  # noqa: E402


def test_never_returns_a_past_date_for_any_universe_name():
    """The property, asserted over the whole live calendar rather than one example."""
    import json
    cal_path = Path(__file__).resolve().parent.parent / "data" / "earnings_calendar.json"
    if not cal_path.exists():
        return
    today = datetime.date.today().isoformat()
    by_ticker = json.loads(cal_path.read_text()).get("by_ticker") or {}
    if not by_ticker:
        return
    checked = 0
    for ticker in by_ticker:
        nxt = _earnings_next(ticker)
        if nxt is None:
            continue
        checked += 1
        assert nxt >= today, f"{ticker}: next_earnings {nxt} is in the past"
    assert checked > 0, "calendar populated but every lookup returned None"


def test_zs_regression_the_original_case():
    """ZS carried last=2026-05-26 / next=2026-09-02 and served the LAST one."""
    import json
    cal_path = Path(__file__).resolve().parent.parent / "data" / "earnings_calendar.json"
    if not cal_path.exists():
        return
    row = (json.loads(cal_path.read_text()).get("by_ticker") or {}).get("ZS")
    if not row or not row.get("next"):
        return
    got = _earnings_next("ZS")
    assert got == str(row["next"])[:10], (got, row)
    assert got != str(row.get("last") or "")[:10], "served the LAST print as the next one"


def test_unknown_ticker_is_none_not_a_guess():
    assert _earnings_next("ZZZZ_NOT_A_TICKER") is None


def test_a_stale_next_is_suppressed_rather_than_served(tmp_path, monkeypatch):
    """If the calendar itself goes stale, a past `next` must read as unknown.

    The mirror of the property test: unknown is a safe answer, a past date is not.
    """
    import json
    import runlib.context_gather as cg
    cal = {"by_ticker": {"AAA": {"next": "2020-01-01T16:00:00-05:00"},
                         "BBB": {"next": "2099-01-01T16:00:00-05:00"}}}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "earnings_calendar.json").write_text(json.dumps(cal))
    monkeypatch.setattr(cg, "ROOT", tmp_path)
    assert cg._earnings_next("AAA") is None        # stale -> unknown
    assert cg._earnings_next("BBB") == "2099-01-01"  # future -> served
