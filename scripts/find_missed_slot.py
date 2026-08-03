#!/usr/bin/env python3
"""Name the slot a watchdog should re-run right now — or nothing.

The self-healing half of the 2026-07-20 fix. Sessions die mid-run (context death,
gather failure) and leave no trace; the previous design lost the whole slot. This lets
a watchdog routine ask, every ~30 min, "did any scheduled slot fail to produce a run,
and is it still worth re-running?" and re-run exactly that one.

Prints a single JSON object to stdout:
  {"slot": "14:00", "depth": "holdings_watchlist", "status": "died", "hhmm": "1400", ...}
  {"slot": null, "reason": "...", "blocked": [...]}    # nothing to do, and WHY

RECOVERABLE, not merely missed. A slot is worth re-running only while it is still THIS
slot's trade — before the NEXT slot's window opens. Re-running the 2pm cycle at 3:55pm,
moments before the 4pm full run, would just price a staler market twice; the next slot
supersedes it. So the detector returns the earliest missed/died slot whose successor has
not yet come due, and stays silent once the day has moved on.

Deliberately decision-only: it reads state and prints, it never trades, commits, or
pushes. The watchdog prompt does the acting, so the risky part stays in one audited
place and this stays trivially safe to call as often as you like.

TWO MEASURED DEFECTS FIXED 2026-08-03, both found by replaying journal/runs +
journal/run_starts across 2026-07-20..08-03 (11 weekdays):

  1. THE RECOVERY WAS STEALING THE NEXT SLOT'S IDENTITY. The rule above was written
     against the moment the DETECTOR runs and forgot that the recovery run itself takes
     12-25 minutes to produce a decision (measured: n=52 breadcrumb->summary pairs,
     median 12.5 min, p90 23.4). So a recovery launched just inside the "before the next
     slot" bound lands INSIDE the next slot's window, and runlib.analytics.slot_report —
     which matches runs to slots purely by timestamp — credits it to the successor.
     Observed twice verbatim in the journal: on 2026-07-29 and 2026-08-03 the 08:45 slot
     never fired, the watchdog re-ran it, and the recovery landed at 10:29 ET
     ("Recovery run for the missed 08:45 ET holdings_watchlist slot"), one minute inside
     the 10:30 window [10:15, 11:30). Result on both days: 08:45 still read MISSED all
     day, 10:30 read HIT, and the REAL 10:30 run (10:44) became an orphan credited to
     nothing. The same shape appears at the other end of the day on five weekdays
     (07-23/27/28/29/31): the 16:00 routine never fired, the watchdog recovered it, and
     the recovery landed 17:29-17:35 — inside the 17:30 window, ~90 minutes after the
     closing bell, i.e. useless as a trade AND destructive as a record.
     FIX: RECOVERY_RUN_H below. A slot is recoverable only while a run launched NOW
     would still LAND before the next slot's window opens.

  2. DETECTION WAS AN HOUR TOO SLOW, WHICH IS WHAT MADE (1) INEVITABLE. This consumed
     slot_report's status, and a slot is not called "missed" there until its full
     one-hour GRACE window closes. For a 16:00 slot that is 17:00 — so the earliest
     possible recovery decision was 17:00, the run landed 17:15-17:30, and it could
     never be anything but too late. But the grace window exists to absorb JITTER, and
     jitter is measurable: the run-start breadcrumb (scripts/mark_run_start.py) is
     pushed within 1-10 minutes of every slot that actually fires (n=52, worst +10).
     A slot with NO breadcrumb 30 minutes in did not fire; waiting the other 30 minutes
     to say so buys nothing and costs the recovery its usefulness.
     FIX: NO_SHOW_H below. A "pending" slot with no breadcrumb and no summary after
     NO_SHOW_H is treated as a no-show and becomes recoverable immediately.

  The watchdog MUST `git pull` before calling this: breadcrumbs and run summaries are
  pushed to origin/main by the trading node, and a stale checkout reads a live slot as
  a no-show.
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

# How long a recovery run takes to go from "launched" to "decision journaled".
# MEASURED, not guessed: 52 run_start->run_summary pairs across 2026-07-21..08-03 give a
# median of 12.5 min and a p90 of 23.4 min (worst 59, a single stalled evening review).
# p90 is the right choice — sizing on the median would still put one recovery in ten
# inside the next slot's window, and sizing on the max would make recovery impossible on
# the 90-minute gaps. Expressed in hours to match the slot arithmetic everywhere else.
RECOVERY_RUN_H = 25 / 60

# How long after its slot a run with NO run-start breadcrumb is declared a no-show.
# The breadcrumb is pushed before the heavy work, within 1-10 minutes of the slot on
# every one of the 52 measured fires, so 30 minutes is a 3x margin over the worst
# observed fire delay. This deliberately undercuts slot_report's 1-hour grace window:
# grace is the right answer for an ALARM (do not page on jitter) and the wrong answer for
# a REPAIR (a repair decided an hour late cannot be executed in time to matter).
#
# LOCAL RUNS WRITE NO BREADCRUMB (orchestrator only marks under --gather-only, the cloud
# path), so a local run that takes longer than NO_SHOW_H could be double-fired here. That
# is why the no-show test also requires that no SUMMARY has landed, and why the run lock
# and the cross-node lease still exist underneath.
NO_SHOW_H = 0.5


def _load_cfg() -> dict | None:
    """Live autonomy_config, or None. Read so the recovery run gets the depth the
    schedule ACTUALLY carries today — this used to call slot_depth_from_hhmm() with no
    cfg, silently grading against runlib.depths' hardcoded defaults. They happen to
    agree right now (10:30 was promoted to full in both on 2026-08-03), and a config
    that drifts from the defaults would have sent a recovery run at the wrong depth
    with nothing to catch it."""
    try:
        return json.loads((ROOT / "autonomy_config.json").read_text())
    except Exception:
        return None


def _label(slot: float) -> str:
    """'HH:MM' for an ET hour-of-day float. Total minutes rather than hour/minute
    separately, so a value like 11.9999 renders 12:00 instead of 11:60."""
    if slot in (float("inf"), float("-inf")):
        return "--:--"
    m = int(round(slot * 60))
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def find(now_h: float | None = None, weekday: bool | None = None,
         cfg: dict | None = None) -> dict:
    from runlib.analytics import (SLOT_EARLY_TOLERANCE_H, SLOT_GRACE_H, et_now,
                                  expected_slots, run_starts_today, slot_report)
    from runlib.depths import slot_depth_from_hhmm

    now = et_now()
    if now_h is None:
        now_h = now.hour + now.minute / 60
    if weekday is None:
        weekday = now.weekday() < 5
    if cfg is None:
        cfg = _load_cfg()

    rep = slot_report(now_h=now_h, weekday=weekday)
    slots = expected_slots(weekday)
    # Map each slot value to the NEXT slot's start, so we can tell whether a missed slot
    # has been superseded by its successor coming due.
    nxt = {slots[i]: (slots[i + 1] if i + 1 < len(slots) else float("inf"))
           for i in range(len(slots))}
    starts = [s for s in run_starts_today() if s["et_hour"] <= now_h]

    # Every slot we decline to recover, and why. A watchdog that prints only
    # {"slot": null} teaches the operator nothing: on 2026-07-31 the 16:00 slot was
    # missed AND unrecoverable AND the detector said "no recoverable missed slot right
    # now", which reads identically to a healthy day.
    blocked: list[dict] = []

    for s in rep["slots"]:
        slot, status = s["slot"], s["status"]
        if status == "hit":
            continue
        if status == "pending":
            # EARLY NO-SHOW DETECTION (see module docstring, defect 2). Inside the grace
            # window slot_report cannot yet tell "late" from "never fired" — but the
            # breadcrumb can, and it is the difference between a recovery that lands in
            # time and one that lands on top of the next slot.
            if now_h - slot < NO_SHOW_H:
                continue
            lo = slot - SLOT_EARLY_TOLERANCE_H
            hi = min(slot + SLOT_GRACE_H, nxt[slot])
            if any(lo <= st["et_hour"] < hi for st in starts):
                continue  # it FIRED and is still inside its grace — let it finish
            status = "no_show"

        # status is now missed / died / no_show — a slot that owes a run.
        label = s["label"]
        if now_h >= nxt[slot]:
            blocked.append({"slot": label, "status": status, "why": "superseded",
                            "detail": f"the {_label(nxt[slot])} slot is already due"})
            continue
        if now_h - slot > MAX_SLOT_AGE_H:
            blocked.append({"slot": label, "status": status, "why": "too_old",
                            "detail": f"{(now_h - slot) * 60:.0f} min late "
                                      f"(cap {MAX_SLOT_AGE_H * 60:.0f})"})
            continue
        # THE LANDING CHECK (see module docstring, defect 1). Not "may I start?" but
        # "will I have FINISHED before the next slot's window opens?".
        landing = now_h + RECOVERY_RUN_H
        deadline = nxt[slot] - SLOT_EARLY_TOLERANCE_H
        if landing >= deadline:
            blocked.append({
                "slot": label, "status": status, "why": "would_land_in_next_window",
                "detail": (f"a run launched now lands ~{_label(landing)}, inside the "
                           f"{_label(nxt[slot])} window that opens {_label(deadline)}; "
                           f"it would be credited to {_label(nxt[slot])} and orphan the "
                           f"real run")})
            continue

        hhmm = f"{int(slot):02d}{int(round((slot % 1) * 60)):02d}"
        # The last slot of the day has no successor to collide with, so its launch
        # deadline comes from the age cap instead of the (infinite) next-slot bound.
        launch_by = min(deadline, slot + MAX_SLOT_AGE_H) - RECOVERY_RUN_H
        return {
            "slot": label, "hhmm": hhmm,
            "depth": slot_depth_from_hhmm(hhmm, cfg),
            "status": status,
            # The recovery run MUST stamp the slot it is recovering onto its breadcrumb,
            # or slot_report will match it by timestamp to whatever window it lands in.
            # Either mechanism works; the env var also reaches the marker that
            # orchestrator.py re-invokes for itself under --gather-only.
            "mark_run_start_args": ["--slot", label, "--stage", "recovery"],
            "env": {"EE_RECOVERY_SLOT": label},
            "launch_by_et": _label(launch_by),
            "expected_landing_et": _label(landing),
            "blocked": blocked,
        }

    return {"slot": None,
            "reason": ("no recoverable missed slot right now" if not blocked else
                       "; ".join(f"{b['slot']} {b['why']}" for b in blocked)),
            "blocked": blocked}


def main() -> int:
    # DELIBERATELY NO ARGPARSE. This is invoked by a watchdog that passes nothing, and
    # main() is called directly from the tests; an argument parser here reads pytest's
    # own argv and exits 2 in the middle of the suite. Replay/debug goes through find().
    try:
        print(json.dumps(find()))
    except Exception as e:
        # Fail-quiet: a watchdog that crashes on a detector error should simply do
        # nothing this tick, not error out. Emit a null result and exit 0.
        print(json.dumps({"slot": None, "reason": f"detector error: {e}", "blocked": []}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
