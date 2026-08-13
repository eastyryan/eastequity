#!/usr/bin/env python3
"""Exit 0 if this Grok-cycle role already completed today (ET). Else exit 1.

Stops a DST-twin cron or a GitHub double-dispatch from running the same slot
twice. Watchdog is never "already done" — it is allowed to fire every :15.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def study_landed_today() -> bool:
    from runlib.core import et_date
    day = et_date().replace("-", "")
    return any((ROOT / "state").glob(f"study_{day}-*.json"))


def clock_slot_hit() -> bool:
    from runlib.analytics import slot_report
    from runlib.core import et_now
    now_h = et_now().hour + et_now().minute / 60
    for row in (slot_report().get("slots") or []):
        if row.get("status") != "hit":
            continue
        slot = float(row.get("slot") or -99)
        # Same 20-minute window cloud_slot.sh uses.
        if 0 <= (now_h - slot) * 60 < 20:
            return True
    return False


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
