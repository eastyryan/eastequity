#!/usr/bin/env python3
"""Push a durable 'a run began' breadcrumb, BEFORE the heavy work starts.

WHY (2026-07-20). A run recorded nothing until log_run_summary at the very end, so a
session that died mid-run — the heavy evening review's documented context-death, a
pip/gather failure, any error before the final push — left ZERO trace. Three slots
vanished that way in one day and were indistinguishable from fires that never happened.
You cannot read why a run died when it wrote nothing until it succeeded.

The trading routines call this right after setup, before `--gather-only` and before the
brain reads the (large) context. It journals a run_start and PUSHES it, so the marker
survives the ephemeral cloud checkout. Then:

  * start present, summary present  -> healthy run
  * start present, NO summary       -> fired and DIED (heartbeat reports it, watchdog
                                       re-runs it) — the failure mode that was invisible
  * NO start                        -> the fire never happened (a routine/platform miss)

Fail-OPEN in every branch: a breadcrumb that could abort or wedge the run it is meant to
observe would cause the incident it exists to report. Any error here prints and exits 0.

Correlation to the end-of-run summary is BY SLOT/TIME, not run_id: the cloud flow runs
two separate orchestrator processes (gather-only, then act-on) with different run_ids,
and runlib.analytics.slot_report already matches records to slots by their timestamp.

  python3 scripts/mark_run_start.py                 # auto-detect the current slot
  python3 scripts/mark_run_start.py --stage gathered  # advance an existing run's marker
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# How close to a scheduled slot "now" must be to be labelled as that slot rather than
# off-slot. Matches the early-tolerance/grace band in runlib.analytics.slot_report so a
# marker and its later summary land in the same slot window.
SLOT_MATCH_H = 1.0


def _et_now_hour() -> tuple[float, str]:
    """(ET hour-of-day as float, 'HH:MM' label). UTC-4 fallback if zoneinfo is absent."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc) - timedelta(hours=4)
    return now.hour + now.minute / 60, now.strftime("%H:%M")


def _et_is_weekday() -> bool:
    """Whether TODAY is a weekday in ET, as its own seam.

    Split out from _current_slot_label (2026-07-25) because the hour and the date
    were being read from two different places: tests inject the hour through
    _et_now_hour but the weekday was computed off the real clock, so the whole
    slot-labelling suite went red every Saturday and Sunday — asserting "14:05
    means the 14:00 slot" while the weekend slot list ([0, 23.98]) was in force.
    Production behaviour is unchanged; the date now has a seam the hour already
    had, so a test can pin both halves of "when is it"."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc) - timedelta(hours=4)
    return now.weekday() < 5


def _current_slot_label() -> str | None:
    """The scheduled slot this run belongs to, or None if fired well off-slot.

    Uses the SAME slot list the heartbeat grades against, so a marker cannot claim a
    slot the heartbeat does not recognise."""
    try:
        from runlib.analytics import expected_slots
        now_h, _ = _et_now_hour()
        slots = expected_slots(_et_is_weekday())
        # Nearest slot at or before now (a run fires AT or just after its slot).
        candidates = [s for s in slots if s <= now_h + 0.25]
        if not candidates:
            return None
        slot = max(candidates)
        if now_h - slot > SLOT_MATCH_H:
            return None  # too far past any slot — an off-slot manual/adhoc run
        return f"{int(slot):02d}:{int(round((slot % 1) * 60)):02d}"
    except Exception:
        return None


def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=timeout)


def _push_marker(marker_path: Path, slot: str | None, stage: str) -> bool:
    """Commit + push the breadcrumb. One rebase-retry on a push race. Never raises."""
    try:
        _git("add", str(marker_path.relative_to(ROOT)))
        label = slot or "offslot"
        msg = f"[run-marker] {label} {stage} [vercel skip]"
        c = _git("commit", "-m", msg)
        if c.returncode != 0:
            return False  # nothing staged / nothing to do
        p = _git("push", "origin", "main")
        if p.returncode == 0:
            return True
        # Lost the race to a gather/stop-watch push — rebase our one tiny commit and retry.
        rb = _git("pull", "--rebase", "origin", "main")
        if rb.returncode != 0:
            _git("rebase", "--abort")
            return False
        return _git("push", "origin", "main").returncode == 0
    except Exception as e:
        print(f"  (run-start marker push failed, proceeding: {e})")
        return False


def run(stage: str = "start", no_push: bool = False) -> int:
    """Journal (and optionally push) the breadcrumb. Fail-OPEN: always returns 0."""
    try:
        import journal
        slot = _current_slot_label()
        _, hhmm = _et_now_hour()
        run_id = f"{datetime.now(timezone.utc):%Y%m%d}-mk{uuid.uuid4().hex[:4]}"
        path = journal.log_run_start(run_id, slot=slot, stage=stage,
                                     extra={"marked_at_et": hhmm})
        pushed = False if no_push else _push_marker(path, slot, stage)
        print(f"run-start marker: slot={slot or 'off-slot'} stage={stage} "
              f"pushed={pushed}")
        return 0
    except Exception as e:
        # Fail-open, always. This must never be the reason a run does not run.
        print(f"  (run-start marker skipped: {e})")
        return 0


# Test seam: exercise the fail-open logic without argparse/sys.argv.
main_for_test = run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", default="start",
                    help="lifecycle stage: start | gathered | brain_done")
    ap.add_argument("--no-push", action="store_true",
                    help="journal locally only (tests / local runs)")
    args = ap.parse_args()
    return run(stage=args.stage, no_push=args.no_push)


if __name__ == "__main__":
    raise SystemExit(main())
