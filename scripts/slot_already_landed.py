#!/usr/bin/env python3
"""Exit 0 if this Grok-cycle role already completed today (ET). Else exit 1.

Stops a DST-twin cron or a GitHub double-dispatch from running the same slot
twice. Watchdog is never "already done" — it is allowed every late fire.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Keep in sync with scripts/cloud_slot.sh PRIMARY_WINDOW and
# runlib.depths.slot_depth_from_hhmm tolerance_minutes.
SLOT_WINDOW_MIN = 75


def study_landed_today() -> bool:
    from runlib.core import et_date
    day = et_date().replace("-", "")
    return any((ROOT / "state").glob(f"study_{day}-*.json"))


def clock_slot_hit() -> bool:
    """True if the nearest elapsed scheduled slot already has a hit today.

    Used to suppress a second primary fire for the *same* slot when GitHub
    double-dispatches inside the primary window. A hit on an earlier slot
    must not block a later primary (e.g. 06:00 hit must not block 08:45).
    """
    from runlib.analytics import slot_report
    from runlib.core import et_now
    now_h = et_now().hour + et_now().minute / 60
    # Prefer the most recent slot still inside the primary window.
    best = None
    best_age = None
    for row in (slot_report().get("slots") or []):
        slot = float(row.get("slot") or -99)
        age_min = (now_h - slot) * 60
        if age_min < 0 or age_min >= SLOT_WINDOW_MIN:
            continue
        if best_age is None or age_min < best_age:
            best, best_age = row, age_min
    if best is None:
        return False
    return best.get("status") == "hit"


def already_landed(role: str) -> bool:
    role = (role or "").strip().lower()
    if role in ("", "watchdog", "auto"):
        return False
    if role == "study":
        return study_landed_today()
    return clock_slot_hit()


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(0 if already_landed(role) else 1)
