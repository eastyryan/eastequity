#!/usr/bin/env python3
"""Name the slot a watchdog should re-run right now — or nothing.

The self-healing half of the 2026-07-20 fix. Sessions die mid-run (context death,
gather failure) and leave no trace; the previous design lost the whole slot. This lets
a watchdog routine ask, every ~30 min, "did any scheduled slot fail to produce a run,
and is it still worth re-running?" and re-run exactly that one.

Prints a single JSON object to stdout:
  {"slot": "14:00", "depth": "holdings_watchlist", "status": "died", "hhmm": "1400"}
  {"slot": null, "reason": "..."}                       # nothing to do

RECOVERABLE, not merely missed. A slot is worth re-running only while it is still THIS
slot's trade — before the NEXT slot's window opens. Re-running the 2pm cycle at 3:55pm,
moments before the 4pm full run, would just price a staler market twice; the next slot
supersedes it. So the detector returns the earliest missed/died slot whose successor has
not yet come due, and stays silent once the day has moved on.

Deliberately decision-only: it reads state and prints, it never trades, commits, or
pushes. The watchdog prompt does the acting, so the risky part stays in one audited
place and this stays trivially safe to call as often as you like.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Even a recoverable slot is pointless to re-run if it is ancient — a run is a snapshot
# decision and a 4-hour-late "2pm" cycle is not the 2pm trade. Belt-and-suspenders on top
# of the next-slot check: never resurrect a slot more than this many hours late.
MAX_SLOT_AGE_H = 3.0


def find(now_h: float | None = None, weekday: bool | None = None) -> dict:
    from runlib.analytics import slot_report, expected_slots, et_now
    from runlib.depths import slot_depth_from_hhmm

    now = et_now()
    if now_h is None:
        now_h = now.hour + now.minute / 60
    if weekday is None:
        weekday = now.weekday() < 5

    rep = slot_report(now_h=now_h, weekday=weekday)
    slots = expected_slots(weekday)
    # Map each slot value to the NEXT slot's start, so we can tell whether a missed slot
    # has been superseded by its successor coming due.
    nxt = {slots[i]: (slots[i + 1] if i + 1 < len(slots) else float("inf"))
           for i in range(len(slots))}

    for s in rep["slots"]:
        if s["status"] not in ("missed", "died"):
            continue
        slot = s["slot"]
        # Superseded: the next slot is already due — let it carry the day forward.
        if now_h >= nxt[slot]:
            continue
        # Too old to be this slot's trade any more.
        if now_h - slot > MAX_SLOT_AGE_H:
            continue
        hhmm = f"{int(slot):02d}{int(round((slot % 1) * 60)):02d}"
        return {"slot": s["label"], "hhmm": hhmm,
                "depth": slot_depth_from_hhmm(hhmm),
                "status": s["status"]}

    return {"slot": None, "reason": "no recoverable missed slot right now"}


def main() -> int:
    try:
        print(json.dumps(find()))
    except Exception as e:
        # Fail-quiet: a watchdog that crashes on a detector error should simply do
        # nothing this tick, not error out. Emit a null result and exit 0.
        print(json.dumps({"slot": None, "reason": f"detector error: {e}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
