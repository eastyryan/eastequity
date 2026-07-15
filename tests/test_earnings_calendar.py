"""Offline tests for the earnings-calendar deep-dive trigger — no network."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib.depths import earnings_deep_dive_session
from tools.earnings_calendar import (
    classify_when,
    reporters_for_session,
    session_window_start,
)

ET = ZoneInfo("America/New_York")
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    assert cond, f"{name}{' - ' + detail if detail else ''}"


def _dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_classify_when():
    print("classify_when:")
    check("7am -> bmo", classify_when(_dt(2026, 7, 15, 7, 0)) == "bmo")
    check("11:59am -> bmo", classify_when(_dt(2026, 7, 15, 11, 59)) == "bmo")
    check("4pm -> amc", classify_when(_dt(2026, 7, 15, 16, 5)) == "amc")
    check("8pm -> amc", classify_when(_dt(2026, 7, 15, 20, 0)) == "amc")
    check("1pm midday -> unknown", classify_when(_dt(2026, 7, 15, 13, 0)) == "unknown")
    check("midnight date-only -> unknown", classify_when(_dt(2026, 7, 15, 0, 0)) == "unknown")
    check("None -> unknown", classify_when(None) == "unknown")


def test_window_start():
    print("session_window_start:")
    now = _dt(2026, 7, 15, 8, 40)
    start = session_window_start("morning", now)
    check("morning starts prior 16:00 ET", start == _dt(2026, 7, 14, 16, 0),
          detail=str(start))
    check("evening has no start window", session_window_start("evening", now) is None)


def test_morning_session():
    print("reporters_for_session morning (gather ~8:40am ET):")
    now = _dt(2026, 7, 15, 8, 40)
    cal = {
        # ASML: pre-market beat this morning -> should fire
        "ASML": {"last": _dt(2026, 7, 15, 7, 0).isoformat(), "last_when": "bmo"},
        # NVDA: reported yesterday after close -> in [yest 16:00, now] safety net
        "NVDA": {"last": _dt(2026, 7, 14, 16, 20).isoformat(), "last_when": "amc"},
        # AAPL: reported a week ago -> too old, no fire
        "AAPL": {"last": _dt(2026, 7, 8, 7, 0).isoformat(), "last_when": "bmo"},
        # MSFT: reports LATER today after close -> still 'future' logically; here we
        # simulate yfinance already showing a future 'last' by clock slop -> excluded
        "MSFT": {"last": _dt(2026, 7, 15, 16, 30).isoformat(), "last_when": "amc"},
        # date-only pre-market today -> caught (00:00 today >= yesterday 16:00)
        "AMD": {"last": _dt(2026, 7, 15, 0, 0).isoformat()},
    }
    reps = reporters_for_session(cal, "morning", now)
    check("ASML fires (BMO today)", "ASML" in reps)
    check("NVDA fires (overnight safety net)", "NVDA" in reps)
    check("AMD fires (date-only today)", "AMD" in reps)
    check("AAPL excluded (stale)", "AAPL" not in reps)
    check("MSFT excluded (future beyond tol)", "MSFT" not in reps)
    check("newest first ordering", reps[0] == "ASML", detail=str(reps))


def test_evening_session():
    print("reporters_for_session evening (gather ~5:40pm ET):")
    now = _dt(2026, 7, 15, 17, 40)
    cal = {
        # ASML: after-hours print at 4:15pm today -> should fire
        "ASML": {"last": _dt(2026, 7, 15, 16, 15).isoformat(), "last_when": "amc"},
        # DELL: reported this MORNING (BMO) -> already caught at 9am, exclude
        "DELL": {"last": _dt(2026, 7, 15, 7, 0).isoformat(), "last_when": "bmo"},
        # HPE: reported yesterday afternoon -> not today, exclude
        "HPE": {"last": _dt(2026, 7, 14, 16, 10).isoformat(), "last_when": "amc"},
        # CRDO: reported today with unknown/date-only time -> not clearly BMO, fire
        "CRDO": {"last": _dt(2026, 7, 15, 0, 0).isoformat()},
    }
    reps = reporters_for_session(cal, "evening", now)
    check("ASML fires (AMC today)", "ASML" in reps)
    check("CRDO fires (today, not BMO)", "CRDO" in reps)
    check("DELL excluded (morning print)", "DELL" not in reps)
    check("HPE excluded (yesterday)", "HPE" not in reps)


def test_session_mapping():
    print("earnings_deep_dive_session slot mapping:")
    check("0900 -> morning", earnings_deep_dive_session("0900") == "morning")
    check("1730 -> evening", earnings_deep_dive_session("1730") == "evening")
    check("0840 gather -> morning", earnings_deep_dive_session("0840") == "morning")
    check("1740 gather -> evening", earnings_deep_dive_session("1740") == "evening")
    check("1200 midday -> None", earnings_deep_dive_session("1200") is None)
    check("0600 light -> None", earnings_deep_dive_session("0600") is None)
    check("garbage -> None", earnings_deep_dive_session("noon") is None)
    check("None -> None", earnings_deep_dive_session(None) is None)
    disabled = {"schedule": {"earnings_deep_dive": {"enabled": False}}}
    check("disabled cfg -> None", earnings_deep_dive_session("0900", disabled) is None)
    custom = {"schedule": {"earnings_deep_dive":
                           {"enabled": True, "slots": {"1000": "morning"}}}}
    check("custom slot map honored", earnings_deep_dive_session("1000", custom) == "morning")
    # 1730 is >75 min from the sole custom slot (1000) -> outside tolerance -> None.
    check("custom map: 1730 no longer evening",
          earnings_deep_dive_session("1730", custom) is None)


if __name__ == "__main__":
    test_classify_when()
    test_window_start()
    test_morning_session()
    test_evening_session()
    test_session_mapping()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        sys.exit(1)
    print("\nAll earnings_calendar tests passed.")
