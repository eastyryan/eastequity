"""Offline tests for tools.trigger_watch.decide — no network, no spawning."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.trigger_watch import decide, _in_et_window

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)  # 11:00 ET
DAY = "2026-07-15"


def _alert(t: str) -> dict:
    return {"ticker": t, "level": 100.0, "price": 100.5}


def test_two_tick_confirmation():
    # First sighting: pending only, no run.
    s1, run1 = decide({}, [_alert("ANET")], [], NOW, DAY)
    assert run1 == []
    assert "ANET" in s1["pending"]
    # Second tick 5 minutes later: confirmed.
    s2, run2 = decide(s1, [_alert("ANET")], [], NOW + timedelta(minutes=5), DAY)
    assert run2 == ["ANET"]
    assert "ANET" not in s2["pending"]
    assert s2["runs_by_day"][DAY] == 1


def test_single_wick_does_not_confirm():
    s1, _ = decide({}, [_alert("AMD")], [], NOW, DAY)
    # Price left the band on the next tick: pending cleared, never confirmed.
    s2, run2 = decide(s1, [], [], NOW + timedelta(minutes=5), DAY)
    assert run2 == []
    assert s2["pending"] == {}


def test_too_soon_and_stale_pending():
    s1, _ = decide({}, [_alert("MU")], [], NOW, DAY)
    # 1 minute later: same print, not a confirmation.
    s2, run2 = decide(s1, [_alert("MU")], [], NOW + timedelta(minutes=1), DAY)
    assert run2 == []
    # 30 minutes later (feeder gap / Mac asleep): confirmation restarts.
    s3, run3 = decide(s2, [_alert("MU")], [], NOW + timedelta(minutes=30), DAY)
    assert run3 == []
    assert "MU" in s3["pending"]


def test_held_ticker_skipped():
    s1, run1 = decide({}, [_alert("DELL")], ["DELL"], NOW, DAY)
    assert run1 == [] and s1["pending"] == {}


def test_per_ticker_cooldown():
    state = {"last_run": {"ANET": (NOW - timedelta(hours=1)).isoformat()},
             "pending": {"ANET": (NOW - timedelta(minutes=5)).isoformat()}}
    _, run = decide(state, [_alert("ANET")], [], NOW, DAY)
    assert run == []  # ran 1h ago, cooldown is 4h


def test_daily_cap_and_run_gap():
    pend = {"CRDO": (NOW - timedelta(minutes=5)).isoformat()}
    capped = {"pending": dict(pend), "runs_by_day": {DAY: 2}}
    _, run = decide(capped, [_alert("CRDO")], [], NOW, DAY)
    assert run == []  # daily cap
    recent = {"pending": dict(pend), "runs_by_day": {DAY: 1},
              "last_run": {"MU": (NOW - timedelta(minutes=10)).isoformat()}}
    _, run = decide(recent, [_alert("CRDO")], [], NOW, DAY)
    assert run == []  # another trigger run 10 min ago; min gap is 45


def test_multiple_confirmed_one_run():
    pend = {t: (NOW - timedelta(minutes=6)).isoformat() for t in ("ANET", "CEG")}
    s, run = decide({"pending": pend}, [_alert("ANET"), _alert("CEG")], [], NOW, DAY)
    assert sorted(run) == ["ANET", "CEG"]
    assert s["runs_by_day"][DAY] == 1  # one spawned run covers both


def test_et_window():
    et = timezone(timedelta(hours=-4))
    assert _in_et_window(datetime(2026, 7, 15, 10, 0, tzinfo=et))
    assert not _in_et_window(datetime(2026, 7, 15, 9, 20, tzinfo=et))   # pre-open
    assert not _in_et_window(datetime(2026, 7, 15, 15, 50, tzinfo=et))  # late day
    assert not _in_et_window(datetime(2026, 7, 18, 10, 0, tzinfo=et))   # Saturday


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\nAll trigger_watch tests passed.")
