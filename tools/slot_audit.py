"""Replay the run schedule over PAST days: which slots landed, how late, and why.

WHY THIS EXISTS (2026-08-03). runlib.analytics.slot_report answers "how is TODAY going"
and nothing could answer "what has the schedule actually been doing". So a systematic
failure — the 16:00 slot covered on 5 of 11 weekdays, every recovery run stealing the
next slot's identity — was only visible as a string of one-day alarms that each looked
like bad luck. The whole diagnosis in this file's docstring came out of a throwaway
script; this makes it a command.

THREE THINGS IT SEES THAT slot_report CANNOT:

  1. DRIFT DECOMPOSED. slot_report measures drift from the run SUMMARY timestamp, which
     is written when the run FINISHES. So "median +19 min drift" reads as "the cron is
     late" when the measurement says otherwise: pairing each run against its run_start
     breadcrumb over 2026-07-21..08-03 (n=52) gives a fire delay of +1..+10 min and a run
     DURATION of 12.5 min median / 23.4 p90. The schedule is punctual; the work takes
     time. Those two facts need completely different responses — you cannot fix a
     20-minute run by restarting a runner, and you cannot fix a late cron by moving a
     slot — and they were indistinguishable in every number the system published.

  2. ORPHANS. A run journaled inside the trading day that no slot claimed. 23 of them
     across 11 weekdays. An orphan next to a missed slot is the fingerprint of one run
     taking another slot's window.

  3. COLLISIONS. A slot credited to a run that was really a RECOVERY of an earlier slot
     (journal/run_starts carries `recovery_for` since 2026-08-03; before that the run
     text is the only evidence). Confirmed twice verbatim: "Recovery run for the missed
     08:45 ET holdings_watchlist slot" journaled at 10:29 on 2026-07-29 and 2026-08-03.

Read-only and offline: journal files in, table out. No network, no state written.

  python tools/slot_audit.py                     # last 14 weekdays
  python tools/slot_audit.py --since 2026-07-20
  python tools/slot_audit.py --json
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Kept equal to runlib.analytics.SLOT_EARLY_TOLERANCE_H / SLOT_GRACE_H / RECOVERY_MAX_AGE_H.
# Duplicated rather than imported so the audit can replay a day under a DIFFERENT slot
# list than the one analytics currently hardcodes — which is the only way to see that
# the 09:00/10:00 slots (retired 2026-07-25) were 100% covered and that the "+37 min
# drift at 08:45" everyone was chasing is an artifact of grading pre-07-25 runs against
# a post-07-25 slot list.
EARLY_TOLERANCE_H = 0.25
GRACE_H = 1.0
RECOVERY_MAX_AGE_H = 3.0

# The weekday slot list, by the date it took effect. runlib.analytics.expected_slots()
# only knows the CURRENT one, so replaying history through it silently mislabels every
# run from before a schedule change.
SLOT_HISTORY: list[tuple[str, list[float]]] = [
    ("0001-01-01", [6, 9, 10, 12, 14, 16, 17.5]),
    ("2026-07-25", [6, 8.75, 10.5, 12, 14, 16, 17.5]),
]


def slots_for(day: str) -> list[float]:
    """The weekday slot list in force on `day` (ISO date)."""
    out = SLOT_HISTORY[0][1]
    for since, slots in SLOT_HISTORY:
        if day >= since:
            out = slots
    return list(out)


def label(h: float) -> str:
    m = int(round(h * 60))
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def _et(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return dt.astimezone(timezone.utc) - timedelta(hours=4)


def _load(subdir: str, root: Path) -> dict[str, list[dict]]:
    """Journal records bucketed by their ET calendar date.

    Bucketing by ET date, not by the UTC filename, is the whole point: a 17:30 ET run is
    journaled into the NEXT UTC day's file, so reading files by name splits every
    evening off the day it belongs to.
    """
    out: dict[str, list[dict]] = {}
    d = root / "journal" / subdir
    if not d.exists():
        return out
    for f in sorted(d.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # journal.py appends non-atomically; a torn line is not a run
            dt = _et(rec.get("ts"))
            if dt is None:
                continue
            rec = dict(rec)
            rec["_et_hour"] = dt.hour + dt.minute / 60
            rec["_et_time"] = dt.strftime("%H:%M")
            out.setdefault(dt.date().isoformat(), []).append(rec)
    for day in out:
        out[day].sort(key=lambda r: r["_et_hour"])
    return out


def audit_day(day: str, runs: list[dict], starts: list[dict],
              slots: list[float] | None = None) -> dict:
    """Per-slot accounting for one ET day, plus orphans and drift decomposition.

    Mirrors runlib.analytics.slot_report's matching rules exactly (early tolerance,
    grace, next-slot clamp, the late-recovery second pass) so the statuses here are the
    statuses the heartbeat produced on the day — this is a replay, not a second opinion.
    """
    if slots is None:
        slots = slots_for(day)
    live = [r for r in runs if "halted" not in r and not r.get("manual")]
    used: set[int] = set()
    report = []
    for i, slot in enumerate(slots):
        nxt = slots[i + 1] if i + 1 < len(slots) else float("inf")
        lo, hi = slot - EARLY_TOLERANCE_H, min(slot + GRACE_H, nxt)
        hit = next((j for j, r in enumerate(live)
                    if j not in used and lo <= r["_et_hour"] < hi), None)
        entry = {"slot": slot, "label": label(slot)}
        if hit is not None:
            used.add(hit)
            entry.update(status="hit", drift_min=round((live[hit]["_et_hour"] - slot) * 60),
                         at=live[hit]["_et_time"], depth=live[hit].get("run_depth"),
                         run_id=live[hit].get("run_id"))
        else:
            fired = any(lo <= s["_et_hour"] < hi for s in starts)
            entry.update(status="died" if fired else "missed")
        report.append(entry)

    for i, entry in enumerate(report):
        if entry["status"] not in ("missed", "died"):
            continue
        slot = entry["slot"]
        nxt = slots[i + 1] if i + 1 < len(slots) else float("inf")
        lo = min(slot + GRACE_H, nxt)
        hi = min(nxt - EARLY_TOLERANCE_H, slot + RECOVERY_MAX_AGE_H)
        j = next((k for k, r in enumerate(live)
                  if k not in used and lo <= r["_et_hour"] < hi), None)
        if j is not None:
            used.add(j)
            entry.update(status="hit", recovered=True, at=live[j]["_et_time"],
                         drift_min=round((live[j]["_et_hour"] - slot) * 60),
                         depth=live[j].get("run_depth"), run_id=live[j].get("run_id"))

    # Fire delay vs run duration, per slot, from the breadcrumb that preceded the run —
    # and, from that same breadcrumb, whether the run was really a recovery of a
    # DIFFERENT slot.
    collisions = []
    for entry in report:
        if entry["status"] != "hit":
            continue
        slot = entry["slot"]
        lo = slot - EARLY_TOLERANCE_H
        pre = [s for s in starts if lo <= s["_et_hour"] <= entry["drift_min"] / 60 + slot]
        if not pre:
            continue
        # The CLOSEST preceding breadcrumb, not the earliest: a slot that fired twice
        # (07-22 17:40 and 17:49, 07-28 12:06 and 12:33) would otherwise be paired with
        # the abandoned first attempt and report a fictional run length.
        marker = pre[-1]
        entry["fired_at"] = marker["_et_time"]
        entry["fire_delay_min"] = round((marker["_et_hour"] - slot) * 60)
        entry["run_minutes"] = entry["drift_min"] - entry["fire_delay_min"]
        # `recovery_for` is stamped by scripts/mark_run_start.py --slot from 2026-08-03.
        # Before that a recovery was anonymous and the only evidence is the brain's own
        # prose in the run summary, which this does not try to parse.
        claimed = marker.get("recovery_for")
        if claimed and claimed != entry["label"]:
            entry["credited_a_recovery_of"] = claimed
            collisions.append(entry["label"])

    band_lo, band_hi = slots[0] - EARLY_TOLERANCE_H, slots[-1] + GRACE_H
    orphans = [{"at": r["_et_time"], "depth": r.get("run_depth"),
                "run_id": r.get("run_id"),
                "in_band": band_lo <= r["_et_hour"] <= band_hi}
               for j, r in enumerate(live) if j not in used]
    return {"day": day, "slots": report, "orphans": orphans,
            "in_band_orphans": [o for o in orphans if o["in_band"]],
            "collisions": collisions,
            "covered": sum(1 for e in report if e["status"] == "hit"),
            "expected": len(report)}


def audit(since: str | None = None, days: int = 14, root: Path | None = None) -> dict:
    root = root or ROOT
    runs, starts = _load("runs", root), _load("run_starts", root)
    all_days = sorted(runs)
    if since:
        all_days = [d for d in all_days if d >= since]
    weekdays = [d for d in all_days if date.fromisoformat(d).weekday() < 5][-days:]
    per_day = [audit_day(d, runs.get(d, []), starts.get(d, [])) for d in weekdays]

    by_slot: dict[str, dict] = {}
    for day in per_day:
        for e in day["slots"]:
            b = by_slot.setdefault(e["label"], {"hit": 0, "total": 0, "drift": [],
                                                "fire": [], "work": []})
            b["total"] += 1
            if e["status"] == "hit":
                b["hit"] += 1
                b["drift"].append(e["drift_min"])
                if "fire_delay_min" in e:
                    b["fire"].append(e["fire_delay_min"])
                    b["work"].append(e["run_minutes"])
    for b in by_slot.values():
        b["median_drift_min"] = statistics.median(b["drift"]) if b["drift"] else None
        b["median_fire_delay_min"] = statistics.median(b["fire"]) if b["fire"] else None
        b["median_run_minutes"] = statistics.median(b["work"]) if b["work"] else None
    return {"days": per_day, "by_slot": by_slot,
            "orphans_total": sum(len(d["in_band_orphans"]) for d in per_day)}


def _print(result: dict) -> None:
    for d in result["days"]:
        cells = []
        for e in d["slots"]:
            if e["status"] == "hit":
                tag = "R" if e.get("recovered") else ""
                cells.append(f"{e['label']}:{e['drift_min']:+d}{tag}")
            else:
                cells.append(f"{e['label']}:{e['status'].upper()}")
        orph = d["in_band_orphans"]
        line = f"{d['day']}  " + "  ".join(cells)
        if orph:
            line += "   orphans: " + ", ".join(o["at"] for o in orph)
        if d["collisions"]:
            line += "   COLLISION: " + ", ".join(d["collisions"])
        print(line)
    print(f"\n{'slot':6} {'covered':>9}  {'drift':>6}  {'fire':>5}  {'work':>5}"
          "   (median minutes; drift = fire delay + work)")
    for k in sorted(result["by_slot"]):
        b = result["by_slot"][k]
        def _n(v): return "-" if v is None else f"{v:.0f}"
        print(f"{k:6} {b['hit']:>4}/{b['total']:<4}  {_n(b['median_drift_min']):>6}  "
              f"{_n(b['median_fire_delay_min']):>5}  {_n(b['median_run_minutes']):>5}")
    print(f"\nin-band orphan runs (credited to no slot): {result['orphans_total']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=None, help="first ET date to include (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=14, help="how many weekdays (default 14)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    result = audit(since=args.since, days=args.days)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
