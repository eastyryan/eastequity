"""Run analytics, closed trades, calibration, charts helpers."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import validator
from tools.trigger_conditions import evaluate_trigger
from tools.watchlist_triggers import TRIGGER_TOLERANCE_PCT, parse_price_level
from runlib.core import ROOT, et_date, et_now, to_et_date, json_safe, light_prices

# Minimum closed trades in a bucket before a RATE is published rather than a count.
# Kept equal to tools/performance_breakdown.MIN_BUCKET_TRADES and
# calibration_gate.DEFAULTS["min_bucket_trades"] — three places computed statistics
# on the same trades and only one of them had a floor.
MIN_BUCKET_TRADES = 5

def expected_slots(weekday: bool) -> list[float]:
    """Scheduled run slots (ET hours) for a weekday vs a weekend day.
    KEEP IN SYNC with the slot gate in scripts/run_cycle.sh and the cloud
    routines: weekdays run SEVEN slots (user policy 2026-07-13) — 6am, 8:45am,
    10:30am, 12pm, 2pm, 4pm, 5:30pm (5:30 is a research review, no trading, but
    still journals a run summary); weekends run news-only at midnight and
    11:59pm. The nightly cloud midnight news run may journal an extra completed
    run — the heartbeat only alarms on MISSING runs, so that is harmless.

    09:00/10:00 -> 08:45/10:30 (user policy 2026-07-25). Two reasons:
      * SPACING. Sixty minutes apart was the tightest pair on the board while
        measured drift ran +15..30min, so their grace windows overlapped and one
        run could be credited to either. 105 minutes apart, each slot gets its
        full hour of grace with no ambiguity.
      * TIMING. 10:00 sits 30 minutes after the open, inside the opening range;
        10:30 is a full hour in, by which point the range has settled. For a
        swing book that is a better place to act, independent of the plumbing.
    """
    return [6, 8.75, 10.5, 12, 14, 15.5, 17.5] if weekday else [0, 23.98]


# A run may fire slightly early (cron jitter) and still belong to its slot; and a slot
# is not "missed" until it has had a full hour to land. GitHub coalesces cron under
# load — observed ~hourly on 2026-07-15 — so a grace shorter than this pages on jitter,
# and a muted alarm is worse than no alarm.
SLOT_EARLY_TOLERANCE_H = 0.25
SLOT_GRACE_H = 1.0
# A self-heal recovery run (scripts/find_missed_slot.py) re-runs the earliest still-open
# missed/died slot up to this many hours late. Such a run legitimately lands AFTER the
# slot's grace window closes but BEFORE the next slot, so the first-pass window (clamped
# at slot+grace to stop one run satisfying two slots) never credits it. slot_report gives
# it a second-pass credit so a slot that WAS successfully recovered stops reporting as
# "missed" and paging all day. Kept equal to scripts.find_missed_slot.MAX_SLOT_AGE_H —
# the two describe one window (first observed on the first real self-heal activation,
# 2026-07-23: 06:00 recovered at 07:19 ET yet paged "missed" until end of day).
RECOVERY_MAX_AGE_H = 3.0


def _to_et_hour(iso_ts: str | None) -> float | None:
    """ET hour-of-day as a float, or None if unparseable."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            dt = dt - timedelta(hours=4)
        return dt.hour + dt.minute / 60
    except Exception:
        return None


def _journal_dates_for_today_et() -> list[str]:
    """The UTC-named journal files that can hold TODAY's (ET) records.

    An ET calendar day D starts at 04:00/05:00 UTC on D and ends inside UTC day D+1,
    so exactly two files can carry it: D and D+1.

    DERIVED FROM et_date(), DELIBERATELY, not from the wall clock. This used to read
    `now_utc` and `now_utc - 1day` while the record filter below compared against
    et_date() — two different notions of "today" that agree only by luck. They diverge
    every evening once UTC rolls past ET (2026-07-22 00:00Z = 2026-07-21 20:00 ET), and
    they made the slot-report tests non-hermetic: the fixtures pin et_date to
    2026-07-20 and write 2026-07-20.jsonl, so the whole suite went red the moment real
    UTC left that date, reporting every slot as missed. Reading one seam keeps the
    discovery and the filter in agreement, and a permanently-red watchdog suite is how
    a real outage gets mistaken for noise.
    """
    d = date.fromisoformat(et_date())
    return [d.isoformat(), (d + timedelta(days=1)).isoformat()]


def completed_runs_today() -> list[dict]:
    """Scheduled runs journaled for TODAY (ET), newest last.

    Excludes halted runs (they did not trade) and MANUAL runs. Manual exclusion is
    load-bearing: a hand-fired run must never be able to fill in a slot the scheduler
    missed and turn the alarm green. See scripts/manual_run.sh.
    """
    out = []
    for day in _journal_dates_for_today_et():
        f = ROOT / "journal" / "runs" / f"{day}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if to_et_date(rec.get("ts")) != et_date():
                continue
            if "halted" in rec or rec.get("manual"):
                continue
            hour = _to_et_hour(rec.get("ts"))
            if hour is None:
                continue
            out.append({"et_hour": round(hour, 2), "ts": rec.get("ts"),
                        "run_id": rec.get("run_id"),
                        # Older records predate node stamping (added 2026-07-20).
                        "node": rec.get("node") or "unknown"})
    return sorted(out, key=lambda r: r["et_hour"])


def run_starts_today() -> list[dict]:
    """run_started breadcrumbs journaled for TODAY (ET), newest last.

    Written and pushed by scripts/mark_run_start.py at the START of a run, before the
    heavy work. A start whose slot never gets a matching summary is a run that fired and
    DIED — the failure mode that was invisible before 2026-07-20. Manual/off-slot markers
    (slot is None) are ignored: they cannot certify a scheduled slot.
    """
    out = []
    for day in _journal_dates_for_today_et():
        f = ROOT / "journal" / "run_starts" / f"{day}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if to_et_date(rec.get("ts")) != et_date():
                continue
            hour = _to_et_hour(rec.get("ts"))
            if hour is None:
                continue
            out.append({"et_hour": round(hour, 2), "ts": rec.get("ts"),
                        "slot": rec.get("slot"), "stage": rec.get("stage") or "start",
                        # Which slot a WATCHDOG RECOVERY is answering for. Stamped
                        # by scripts/mark_run_start.py --slot / $EE_RECOVERY_SLOT.
                        # Without it a recovery is matched purely on timestamp and
                        # steals the next slot's identity — see slot_report.
                        "recovery_for": rec.get("recovery_for"),
                        "node": rec.get("node") or "unknown"})
    return sorted(out, key=lambda r: r["et_hour"])


def slot_report(now_h: float | None = None, weekday: bool | None = None) -> dict:
    """PER-SLOT accounting: which scheduled slots actually produced a run today.

    Replaces bare subtraction (expected − completed). That arithmetic could not name
    WHICH slot was missed, and it silently miscounted whenever a slot fired twice: two
    runs in the 9am slot and nothing at 10am netted to "0 missed". Per-slot matching
    cannot be fooled that way — each run is consumed by at most one slot.

    A slot's window is [slot − early_tolerance, min(slot + grace, next_slot)). Clamping
    to the next slot matters because 9/10 and 16/17.5 sit closer together than the grace
    period, so an ungated window would let one 9:55 run satisfy both the 9am and 10am
    slots.
    """
    now = et_now()
    if now_h is None:
        now_h = now.hour + now.minute / 60
    if weekday is None:
        weekday = now.weekday() < 5
    slots = expected_slots(weekday)
    # Only runs that have actually happened by now_h. In production now IS now so this
    # is a no-op, but it keeps the function honest under an explicit now_h (replaying a
    # past heartbeat must not see runs from later in the day) and guards against a
    # future-dated ts from clock skew silently satisfying a slot that never ran.
    runs = [r for r in completed_runs_today() if r["et_hour"] <= now_h]
    starts = [s for s in run_starts_today() if s["et_hour"] <= now_h]
    used: set[int] = set()
    # PRE-PASS — honour a run that DECLARED which slot it answers for.
    #
    # A watchdog recovery of the 08:45 slot that lands at 10:29 is NOT the 10:30
    # run, and timestamp matching alone cannot tell the difference. This happened
    # verbatim on 2026-07-29 and 2026-08-03: 08:45 never fired, the recovery
    # landed 10:29 — one minute inside the 10:30 window — so 10:30 was marked hit,
    # 08:45 read MISSED all day, and the REAL 10:30 run at 10:44 was orphaned.
    # The same shape closes the day: a 16:00 recovery landing 17:29-17:35 takes
    # the 17:30 slot and orphans the real evening review. 15 in-band orphan runs
    # across 11 weekdays, all invisible because nothing ever read unmatched_runs.
    #
    # scripts/mark_run_start.py stamps `recovery_for`; the summary that follows
    # within the measured p90 run length (25 min) is that recovery's own run.
    declared: dict[float, int] = {}
    for st in starts:
        want = st.get("recovery_for")
        if not want:
            continue
        sv = next((s for s in slots
                   if f"{int(s):02d}:{int(round((s % 1) * 60)):02d}" == want), None)
        if sv is None or sv in declared:
            continue
        j = next((k for k, r in enumerate(runs)
                  if k not in used and st["et_hour"] <= r["et_hour"]
                  <= st["et_hour"] + 25 / 60), None)
        if j is not None:
            used.add(j)
            declared[sv] = j
    report = []
    for i, slot in enumerate(slots):
        nxt = slots[i + 1] if i + 1 < len(slots) else float("inf")
        lo, hi = slot - SLOT_EARLY_TOLERANCE_H, min(slot + SLOT_GRACE_H, nxt)
        hit = next((j for j, r in enumerate(runs)
                    if j not in used and lo <= r["et_hour"] < hi), None)
        label = f"{int(slot):02d}:{int(round((slot % 1) * 60)):02d}"
        if slot in declared:
            j = declared[slot]
            report.append({"slot": slot, "label": label, "status": "hit",
                           "recovered": True,
                           "drift_min": round((runs[j]["et_hour"] - slot) * 60),
                           "run_id": runs[j]["run_id"], "node": runs[j]["node"]})
            continue
        if hit is not None:
            used.add(hit)
            # DRIFT: how late (or early) the run that covered this slot actually
            # landed. Recorded because "covered" and "on time" are different facts
            # and only one of them was ever visible. Measured over 2026-07-20..24,
            # NO run landed on time — median +19 min, worst +43 — which is what
            # makes 10:00 the most-missed slot on the board: it sits 60 min after
            # 09:00, so a 09:00 run drifting past +30 walks into its neighbour's
            # window and the two slots collide.
            report.append({"slot": slot, "label": label, "status": "hit",
                           "drift_min": round((runs[hit]["et_hour"] - slot) * 60),
                           "run_id": runs[hit]["run_id"], "node": runs[hit]["node"]})
        elif now_h < hi:
            # Still inside its window — not late yet, so not a miss.
            report.append({"slot": slot, "label": label, "status": "pending"})
        else:
            # No summary and the window has closed — a miss. If a start breadcrumb
            # landed in the window, the run FIRED AND DIED (distinct, and diagnosable);
            # otherwise the fire never happened. Both are missed slots, but the label
            # tells you where to look: a "died" slot has a session to read, a "missed"
            # one means the routine/platform never triggered.
            died = any(lo <= s["et_hour"] < hi for s in starts)
            report.append({"slot": slot, "label": label,
                           "status": "died" if died else "missed"})

    # SECOND PASS — credit a self-heal RECOVERY run to the slot it recovered.
    #
    # The watchdog (scripts/find_missed_slot.py) re-runs a still-open missed/died slot up
    # to RECOVERY_MAX_AGE_H late, so a legitimate run can land PAST a slot's grace window
    # but before the next slot — a region the first pass leaves unmatched because its
    # window clamps at slot+grace to stop one run covering two adjacent slots. Without
    # this, a slot that was SUCCESSFULLY recovered stays "missed" the rest of the day and
    # pages on every heartbeat. The recovery window is [slot+grace, next_slot_early),
    # capped at RECOVERY_MAX_AGE_H after the slot — exactly the window the watchdog is
    # allowed to act in. Adjacent slots (9/10, 16/17.5) leave no room and get no recovery
    # credit, which is correct: the watchdog does not resurrect a slot its successor has
    # already reached.
    for i, entry in enumerate(report):
        if entry["status"] not in ("missed", "died"):
            continue
        slot = entry["slot"]
        nxt = slots[i + 1] if i + 1 < len(slots) else float("inf")
        lo = min(slot + SLOT_GRACE_H, nxt)
        hi = min(nxt - SLOT_EARLY_TOLERANCE_H, slot + RECOVERY_MAX_AGE_H)
        rec = next((j for j, r in enumerate(runs)
                    if j not in used and lo <= r["et_hour"] < hi), None)
        if rec is not None:
            used.add(rec)
            # A late recovery IS a covered slot — mark it "hit" so no status-based
            # consumer regresses, but flag it `recovered` so the lateness stays visible
            # on the dashboard and in the journal.
            entry["status"] = "hit"
            entry["recovered"] = True
            entry["drift_min"] = round((runs[rec]["et_hour"] - slot) * 60)
            entry["run_id"] = runs[rec]["run_id"]
            entry["node"] = runs[rec]["node"]

    # A died slot is still a slot that produced no run — count it as missed for paging,
    # but surface it separately so the alert can say "fired and died" vs "never fired".
    missed = [s["label"] for s in report if s["status"] in ("missed", "died")]
    died_slots = [s["label"] for s in report if s["status"] == "died"]
    nodes = sorted({runs[j]["node"] for j in used}) if used else []
    drifts = [s["drift_min"] for s in report if isinstance(s.get("drift_min"), int)]
    return {"slots": report, "missed_slots": missed, "died_slots": died_slots,
            "hit": len(used), "missed_count": len(missed),
            # Worst lateness among slots that DID land. A slot list whose drift
            # approaches the gap to the next slot is a schedule that will start
            # eating its own slots, which is a different problem from a dead node
            # and needs a different fix (move the slot, not restart the runner).
            "max_drift_min": max(drifts) if drifts else None,
            "elapsed": sum(1 for s in report if s["status"] != "pending"),
            "nodes_seen": nodes,
            "unmatched_runs": [r["run_id"] for j, r in enumerate(runs) if j not in used]}


def build_health() -> dict:
    """Runs heartbeat: expected schedule slots so far today (ET) vs runs actually
    journaled. A silently-dead pipeline (the plan-mode parse bug ran for DAYS
    unnoticed) now shows up on the dashboard as missed runs instead of nothing."""
    now = et_now()
    weekday = now.weekday() < 5
    now_h = now.hour + now.minute / 60
    runs = completed_runs_today()
    report = slot_report(now_h=now_h, weekday=weekday)
    # `expected` counts slots whose window has CLOSED, not slots merely past their
    # start time. The old version counted a slot as expected the instant the clock
    # passed it, so the alarm reported a miss during the grace period it had itself
    # granted, and every threshold was evaluated one slot early.
    expected = report["elapsed"]
    completed = len(runs)
    last_ts = runs[-1]["ts"] if runs else None
    missed = report["missed_count"]
    # Bundle-age alarm: the relay bundle is the cloud runs' data supply. If the
    # gatherers (GH Action / local relay) die, the bundle ages - warn well BEFORE
    # the 4h stale threshold so it gets fixed, not discovered after the fact.
    bundle_age_h = None
    try:
        b = json.loads((ROOT / "data" / "cloud_context.json").read_text())
        bundle_age_h = round((datetime.now(timezone.utc)
                              - datetime.fromisoformat(b["run_date"])).total_seconds() / 3600, 1)
    except Exception:
        pass
    market_hours = weekday and 9.5 <= now_h <= 16
    bundle_alarm = bool(bundle_age_h is not None and
                        (bundle_age_h > 2 if market_hours else bundle_age_h > 8))
    learning = learning_loop_freshness()
    try:
        spend = brain_call_report(days=1)
        dead = run_produced_nothing()
    except Exception:
        spend, dead = {}, {}
    status = "ok"
    if missed:
        # Every missed slot is a lost chance to enter, exit, or learn (user policy
        # 2026-07-20), so ONE is already worth saying on the dashboard. The alarm's
        # own paging threshold is separate and lives in scripts/heartbeat_check.py.
        detail = ", ".join(report["missed_slots"])
        if report["died_slots"]:
            detail += f" (fired-and-died: {', '.join(report['died_slots'])})"
        status = f"DEGRADED - missed scheduled run(s): {detail}"
    elif dead.get("empty"):
        # A run that completes and says nothing used to count as healthy.
        status = f"DEGRADED - {dead['note']}"
    elif bundle_alarm:
        status = f"WARNING - data bundle is {bundle_age_h}h old (gatherers may be down)"
    elif learning.get("stale"):
        status = f"WARNING - {learning['note']}"
    return {
        "as_of_et": now.isoformat(timespec="minutes"),
        "expected_runs_so_far": expected,
        "completed_scheduled_runs": completed,
        # TWO DIFFERENT FACTS, AND CONFLATING THEM IS WHY THE ALARM READ AS
        # NONSENSE. `completed_scheduled_runs` is a RAW COUNT of runs journaled
        # today — it includes recovery runs, manual runs, and a slot that fired
        # twice. `slots_covered` is how many scheduled slots actually produced a
        # run. Only the second is comparable to expected_runs_so_far/missed, which
        # are per-slot. Reporting the raw count against a per-slot expectation is
        # what produced heartbeat pages like "1 scheduled run(s) missed today
        # [10:00 ET] (expected 3, completed 3)" and "(expected 7, completed 9)" —
        # arithmetic that looks broken, trains you to ignore the alarm, and hides
        # the real signal (a specific slot never ran).
        "slots_covered": report["hit"],
        "runs_journaled": completed,
        "max_drift_min": report.get("max_drift_min"),
        "missed": missed,
        # WHICH slots, not just how many — "missed 14:00" is actionable, "missed 1" is
        # a number you have to go investigate before you can do anything with it.
        "missed_slots": report["missed_slots"],
        # Slots that fired and DIED (a start breadcrumb but no summary) — these have a
        # session to read; a plain "missed" slot means the fire never happened.
        "died_slots": report["died_slots"],
        # Runs that landed inside the scheduled day and matched NO slot. slot_report
        # has always computed this and nothing ever forwarded it, so 15 orphan runs
        # across 11 weekdays were invisible — including the recovery runs that took
        # a later slot's window and left the real run for that slot unaccounted.
        # An orphan is the signature of a slot collision, so it belongs in health.
        "unmatched_runs": report["unmatched_runs"],
        "slots": report["slots"],
        # Which node(s) actually ran today. Run records carried no node identity until
        # 2026-07-20, so a live cloud trader masked a completely dead local one and the
        # aggregate count looked merely low rather than half-dead.
        "nodes_seen": report["nodes_seen"],
        "bundle_age_hours": bundle_age_h,
        "last_scheduled_run_utc": last_ts,
        "learning_loop": learning,
        "brain_spend_today": spend,
        "last_brain_call": dead,
        "status": status,
    }


def brain_call_report(days: int = 1) -> dict:
    """Spend, latency and research behaviour from journal/brain_calls/.

    Before 2026-07-19 nothing recorded any of this: the system made 15-30 Opus
    sessions a day and the only cost control was a 12-runs/day cap, which cannot
    tell a 4k-token run from a 400k one.

    Three things worth watching, and each answers a question that was previously
    unanswerable:

      * cost / tokens - what a day of this actually costs.
      * cache_read vs cache_creation - the 7 daily slots share ~95% identical
        context, but they are hours apart and the prompt cache does not live that
        long, so most of it is re-created rather than re-read. The CLI CAN reuse a
        session (-c/--continue), but that would carry conversation state between
        runs and CLAUDE.md's whole design is a brain invoked fresh each cycle. So
        this is measured, not "fixed": the number is here to make that trade-off
        an informed decision rather than an assumption.
      * buys_without_web_search - CLAUDE.md declares "WebSearch MANDATORY before
        any BUY" and nothing had ever verified it. usage.server_tool_use gives the
        count, so a brain call that proposed while searching nothing is now
        visible. Reported, never enforced: this is evidence for a human, and a
        run legitimately searches zero times when it proposes nothing.
    """
    from datetime import timedelta as _td
    out = {"days": days, "n_calls": 0, "total_cost_usd": 0.0,
           "input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_creation_tokens": 0,
           "failures": 0, "timeouts": 0, "slowest_s": None,
           "web_search_requests": 0, "calls_with_zero_search": 0,
           "models": []}
    models: set = set()
    slowest = 0.0
    for delta in range(days):
        day = (datetime.now(timezone.utc) - _td(days=delta)).date().isoformat()
        f = ROOT / "journal" / "brain_calls" / f"{day}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out["n_calls"] += 1
            out["total_cost_usd"] += float(rec.get("total_cost_usd") or 0)
            for k, j in (("input_tokens", "input_tokens"),
                         ("output_tokens", "output_tokens"),
                         ("cache_read_tokens", "cache_read_input_tokens"),
                         ("cache_creation_tokens", "cache_creation_input_tokens")):
                out[k] += int(rec.get(j) or 0)
            if not rec.get("ok"):
                out["failures"] += 1
                if "timeout" in str(rec.get("error") or ""):
                    out["timeouts"] += 1
            slowest = max(slowest, float(rec.get("elapsed_s") or 0))
            ws = rec.get("web_search_requests")
            if ws is not None:
                out["web_search_requests"] += int(ws)
                if int(ws) == 0 and str(rec.get("call", "")).startswith("brain"):
                    out["calls_with_zero_search"] += 1
            for m in (rec.get("models_served_by") or []):
                models.add(str(m))
    out["total_cost_usd"] = round(out["total_cost_usd"], 4)
    out["slowest_s"] = slowest or None
    out["models"] = sorted(models)
    tot_cache = out["cache_read_tokens"] + out["cache_creation_tokens"]
    out["cache_hit_pct"] = (round(100 * out["cache_read_tokens"] / tot_cache, 1)
                            if tot_cache else None)
    return out


def run_produced_nothing(*, min_response_chars: int = 400) -> dict:
    """Did the most recent brain call complete but say essentially nothing?

    build_health counts MISSED runs and bundle age. A run that completes, parses
    nothing, and publishes the fallback "did not include a machine-readable order
    block" string increments `completed` and reports status ok. heartbeat.yml
    cites the plan-mode parse bug that "ran for DAYS unnoticed" as its reason for
    existing — and the check as built would not have caught it, because what it
    watches is the ABSENCE of runs, not the EMPTINESS of them.

    response_chars from the brain-call journal is the cheap signal: a successful
    call that returned almost nothing is a semantically dead run.
    """
    out = {"checked": False, "empty": False, "response_chars": None}
    from datetime import timedelta as _td
    for delta in (0, 1):
        day = (datetime.now(timezone.utc) - _td(days=delta)).date().isoformat()
        f = ROOT / "journal" / "brain_calls" / f"{day}.jsonl"
        if not f.exists():
            continue
        rows = []
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ok") and str(rec.get("call", "")).startswith("brain"):
                rows.append(rec)
        if rows:
            last = rows[-1]
            n = int(last.get("response_chars") or 0)
            out.update(checked=True, response_chars=n,
                       empty=n < min_response_chars,
                       run_id=last.get("run_id"), ts=last.get("ts"))
            if out["empty"]:
                out["note"] = (
                    f"last brain call returned only {n} characters — the run "
                    f"completed but produced almost nothing. A parse failure or a "
                    f"truncated answer looks identical to a deliberate quiet day "
                    f"in the run counters.")
            return out
    return out


def learning_loop_freshness(*, stale_after_days: int = 4) -> dict:
    """Is the proactive learning loop still producing? Days since the last lesson.

    THE GAP THIS CLOSES (audited 2026-07-19). CLAUDE.md states that "every weekday
    after the close, a dedicated STUDY SESSION researches ONE curriculum topic".
    The scheduling for it is real — scripts/run_cycle.sh chains `--study` on the
    weekday 17:30 slot — but across ~8 eligible weekdays only TWO sessions ever
    ran, both on 2026-07-17 six minutes apart, and one of the two failed to parse.
    Total yield: one lesson.

    Nothing noticed. build_health counts MISSED RUNS and bundle age; a knowledge
    base that has not grown in a week looks identical to one that is merely
    well-curated. This is the same failure class as the rest of the audit — a loop
    that stops producing and reports nothing — applied to the learning layer.

    Deliberately NOT an error: a quiet learning loop is a warning, never something
    that should block trading. Exits and risk reduction are never gated on it.
    """
    out: dict = {"stale": False, "n_lessons": None, "days_since_last_lesson": None}
    try:
        store = json.loads((ROOT / "data" / "knowledge_base.json").read_text())
    except Exception:
        out["note"] = "knowledge base unreadable — learning freshness unknown"
        return out

    entries = store.get("entries") or []
    out["n_lessons"] = len(entries)

    newest = None
    for e in entries:
        for key in ("learned_at", "created_at", "studied_at"):
            raw = e.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            newest = ts if newest is None or ts > newest else newest
            break
    # Fall back to the store's own stamp so a lesson missing learned_at (the
    # current single entry has learned_at: None) does not read as "never ran".
    if newest is None:
        try:
            raw = store.get("updated_at")
            newest = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            out["basis"] = "store_updated_at"
        except Exception:
            newest = None

    if newest is None:
        out["note"] = ("no dated lesson in the knowledge base — the study loop has "
                       "produced nothing datable")
        out["stale"] = bool(entries)
        return out

    days = round((datetime.now(timezone.utc) - newest).total_seconds() / 86400, 1)
    out["days_since_last_lesson"] = days
    out["last_lesson_utc"] = newest.isoformat()
    if days > stale_after_days:
        out["stale"] = True
        out["note"] = (
            f"learning loop quiet: no new lesson in {days:.0f} days "
            f"({len(entries)} total). CLAUDE.md promises a weekday study session; "
            f"check that the scheduler is loaded and that --study is parsing.")
    return out


def _universe_audit_summary() -> dict | None:
    """Compact summary of the latest ALL-universe freshness audit for the bundle.
    None when no audit exists or it is too old to trust (>8 days)."""
    f = ROOT / "dashboard" / "data" / "freshness_audit.json"
    try:
        a = json.loads(f.read_text())
        audited_at = a.get("audited_at_et", "")
        age_days = None
        try:
            age_days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(audited_at)).days
        except Exception:
            pass
        if age_days is not None and age_days > 8:
            return None
        return {"audited_at_et": audited_at, "audited": a.get("audited"),
                "fresh": a.get("fresh"), "stale_tickers": a.get("stale_tickers", []),
                "error_tickers": a.get("error_tickers", []),
                "foreign_annual_filers": a.get("foreign_annual_filers", [])}
    except Exception:
        return None


def build_volatility_context(scan: dict, options_signals: dict) -> dict:
    """Per-ticker volatility for the deterministic stop floor: {TICKER: {atr_pct,
    expected_move_pct}}. ATR comes from the scanner (every name; also present in the
    relayed cloud bundle); expected move from options when that data loaded. This is
    the single map fed to BOTH the validator and the brain-facing stop_engineering
    block, so what the brain is told matches what the validator enforces."""
    vol: dict[str, dict] = {}
    scan = scan or {}
    # ATR for the whole scanned universe (new field), with a fallback to the
    # per-row atr on top_setups/contrarian for bundles gathered before that field.
    for t, atr in (scan.get("atr_by_ticker") or {}).items():
        vol.setdefault(t.upper(), {})["atr_pct"] = atr
    for r in (scan.get("top_setups") or []) + (scan.get("contrarian_setups") or []):
        t = str(r.get("ticker", "")).upper()
        if t and r.get("atr_pct") is not None:
            vol.setdefault(t, {}).setdefault("atr_pct", r["atr_pct"])
    for t, sig in ((options_signals or {}).get("tickers") or {}).items():
        if isinstance(sig, dict) and sig.get("expected_move_pct") is not None:
            vol.setdefault(t.upper(), {})["expected_move_pct"] = sig["expected_move_pct"]
    return vol


def build_stop_engineering(focus: list, vol: dict, cfg: dict) -> dict:
    """Brain-facing: the enforced minimum stop distance per focus name, so the agent
    engineers stops OUTSIDE the noise band on the first try instead of being rejected."""
    floors = {}
    for t in focus:
        t = str(t).upper()
        v = vol.get(t)
        if not v:
            continue
        floor = validator.stop_floor_pct(v.get("atr_pct"), v.get("expected_move_pct"), cfg)
        if floor is None:
            continue
        floors[t] = {
            "atr_pct": v.get("atr_pct"),
            "expected_move_pct": v.get("expected_move_pct"),
            "min_stop_distance_pct": round(floor * 100, 2),
            "tradeable": floor <= cfg["trade_quality_requirements"]["max_stop_loss_distance_pct"],
        }
    return {
        "note": "ENFORCED stop floor per name. Your stop_loss must sit at least "
                "min_stop_distance_pct below entry or the validator rejects it "
                "(stop_inside_noise_band). This is a floor, not a target - for a swing "
                "hold, aim WIDER (roughly 1.5-2x ATR, or clearly beyond the expected "
                "move) so ordinary volatility does not stop you out. If tradeable is "
                "false the name is too volatile for a valid stop under the 15% cap; do "
                "not propose it.",
        "floors": floors,
    }


def build_position_stop_cushion(portfolio: dict, vol: dict, cfg: dict) -> dict:
    """Per open position: how much room is left between today's price and the
    EFFECTIVE stop — max(plan stop, chandelier trailing_stop) — measured in the
    name's own volatility, so the brain sees the level the safety layer will
    actually enforce. A cushion under ~1 ATR means an ordinary session could
    trip the stop - the brain should decide deliberately (hold through, or exit
    on its own terms) rather than be noise-stopped."""
    out = {}
    for pos in portfolio.get("positions", []):
        t = str(pos.get("ticker", "")).upper()
        plan = pos.get("original_plan") or {}
        stop = plan.get("stop_loss")
        last = pos.get("last_price")
        v = vol.get(t) or {}
        atr = v.get("atr_pct")
        if not (stop and last):
            continue
        try:
            stop, last = float(stop), float(last)
        except (TypeError, ValueError):
            continue
        # Effective stop: the ratcheted chandelier trail can only RAISE the
        # enforced level, never lower it. Missing/garbage trail -> plan stop
        # only (identical to the pre-trail behaviour).
        try:
            trail = float(pos.get("trailing_stop") or 0.0) or None
        except (TypeError, ValueError):
            trail = None
        eff_stop = max(stop, trail) if trail is not None else stop
        cushion_pct = (last - eff_stop) / last * 100 if last else None
        entry = plan.get("entry_price_max") or pos.get("avg_cost")
        info = {
            "last_price": round(last, 2),
            "recorded_stop": round(stop, 2),
            "cushion_to_stop_pct": round(cushion_pct, 2) if cushion_pct is not None else None,
            "atr_pct": atr,
            "expected_move_pct": v.get("expected_move_pct"),
        }
        if trail is not None and trail > stop:
            info["trailing_stop"] = round(trail, 2)
            info["effective_stop"] = round(eff_stop, 2)
        if atr and cushion_pct is not None and atr > 0:
            info["cushion_in_atr"] = round(cushion_pct / atr, 2)
            info["inside_noise_band"] = cushion_pct < atr  # < ~1 average day's range
        if entry:
            try:
                info["stop_distance_from_entry_pct"] = round((float(entry) - eff_stop) / float(entry) * 100, 2)
            except (TypeError, ValueError):
                pass
        # Stall detection (soft time stop, forces a DECISION not an exit): two weeks
        # in with the price going nowhere is unpriced opportunity cost - the brain
        # must justify continuing to hold or rotate. The horizon force-close
        # remains the hard time stop.
        days_held = pos.get("days_held")
        avg_cost = pos.get("avg_cost")
        try:
            if days_held is not None and avg_cost:
                pnl_pct = (last / float(avg_cost) - 1) * 100
                info["unrealized_pnl_pct"] = round(pnl_pct, 2)
                info["stalled"] = bool(days_held >= 14 and abs(pnl_pct) < 3.0)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        out[t] = info
    return out

def benchmark_close(ticker: str = "SPY") -> float | None:
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="5d")
        return round(float(h["Close"].iloc[-1]), 2)
    except Exception as e:
        print(f"  WARNING: benchmark fetch failed ({e}) - S&P comparison will show a gap")
        return None


def trade_plans() -> dict:
    """Latest FILLED BUY proposal per ticker from the journal (thesis, stop,
    target, horizon). Rejected/unfilled proposals are excluded: the journal logs
    every validated proposal, and an unfiltered 'latest per ticker' let a later
    rejected proposal supply the plan a closed trade was graded against."""
    from tools.portfolio_state import filled_buy_proposal_ids
    filled = filled_buy_proposal_ids()
    plans: dict = {}
    for f in sorted((ROOT / "journal" / "proposals").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            p = rec.get("proposal", {})
            if str(p.get("action", "")).upper() == "BUY" and p.get("ticker"):
                tk = p["ticker"].upper()
                if rec.get("run_id") not in filled.get(tk, ()):
                    continue
                plans[tk] = p
    return plans


def compute_closed_trades() -> list[dict]:
    """Closed trades with a thesis verdict, from the broker history + trade plans."""
    state_file = ROOT / "state" / "portfolio.json"
    if not state_file.exists():
        return []
    history = json.loads(state_file.read_text()).get("history", [])
    plans = trade_plans()
    opens: dict[str, dict] = {}
    closed = []
    for fill in history:
        if fill.get("status") != "filled":
            continue
        t = fill["ticker"].upper()
        if fill["action"] == "BUY":
            opens[t] = fill
        elif fill["action"] == "SELL_TO_CLOSE" and (fill.get("avg_cost") or t in opens):
            plan = plans.get(t, {})
            # Grading numbers come from the position's OWN plan stamped on the
            # sell fill (entry_plan) whenever present; the journal join is the
            # legacy fallback and supplies the narrative (thesis) only.
            entry_plan = fill.get("entry_plan") or {}
            # New-style fills stamp entry data directly (adds/partials make
            # open/close pairing unreliable); legacy fills fall back to pairing.
            if fill.get("avg_cost"):
                entry = float(fill["avg_cost"])
                opened_ts = fill.get("position_opened_at") or fill["filled_at"]
                opens.pop(t, None)
            else:
                entry_fill = opens.pop(t)
                entry = float(entry_fill["fill_price"])
                opened_ts = entry_fill["filled_at"]
            exit_px = float(fill["fill_price"])
            stop = float(entry_plan.get("stop_loss") or plan.get("stop_loss") or 0)
            target = float(entry_plan.get("target_price") or plan.get("target_price") or 0)
            days_held = max((datetime.fromisoformat(fill["filled_at"])
                             - datetime.fromisoformat(opened_ts)).days, 0)
            horizon = entry_plan.get("holding_horizon_days") or plan.get("holding_horizon_days")
            if target and exit_px >= target * 0.995:
                verdict = "Hit target"
            elif stop and exit_px <= stop * 1.005:
                verdict = "Stopped out"
            elif horizon and days_held >= float(horizon):
                verdict = "Time-limit exit"
            else:
                verdict = "Thesis exit"
            r_multiple = round((exit_px - entry) / (entry - stop), 2) if stop and entry > stop else None
            # pnl_usd stays price-only (net of fees) for trade GRADING; total_pnl_usd adds
            # attributed dividends for honest RETURN. Dividends already hit cash when paid,
            # so never sum both into the same total (see compute_performance_stats).
            price_pnl = fill.get("realized_pnl_usd")
            divs = fill.get("dividends_received_usd") or 0.0
            total_pnl = fill.get("total_realized_pnl_usd")
            if total_pnl is None:
                total_pnl = (price_pnl or 0.0) + divs
            closed.append({
                "ticker": t, "entry_price": entry, "exit_price": exit_px,
                "opened_at": to_et_date(opened_ts), "closed_at": to_et_date(fill["filled_at"]),
                "days_held": days_held, "pnl_usd": price_pnl,
                "dividends_usd": round(divs, 2), "total_pnl_usd": round(total_pnl, 2),
                "fees_usd": fill.get("fees_usd"), "gap_modeled": fill.get("gap_modeled", False),
                "r_multiple": r_multiple, "verdict": verdict,
                "confidence": entry_plan.get("confidence") or plan.get("confidence"),
                "thesis": plan.get("thesis"),
            })
    return closed


def compute_performance_stats(closed: list[dict], equity_hist: list[dict]) -> dict | None:
    if not closed:
        return None
    pnls = [t["pnl_usd"] or 0 for t in closed]          # price-only, net of fees
    total_pnls = [t.get("total_pnl_usd", t["pnl_usd"] or 0) or 0 for t in closed]  # + dividends
    wins = [p for p in pnls if p > 0]
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    fees = sum((t.get("fees_usd") or {}).get(k, 0) or 0
               for t in closed for k in ("commission", "sec_fee", "taf"))
    peak, max_dd = 0.0, 0.0
    for h in equity_hist:
        peak = max(peak, h["equity"])
        if peak:
            max_dd = max(max_dd, (peak - h["equity"]) / peak)
    stats = {
        "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
        "realized_pnl_usd": round(sum(pnls), 2),               # price-only
        "realized_pnl_incl_dividends_usd": round(sum(total_pnls), 2),
        "total_fees_paid_usd": round(fees, 2),                 # cost drag, shown for honesty
        "avg_r_multiple": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_days_held": round(sum(t["days_held"] for t in closed) / len(closed), 1),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    # BENCHMARK ATTRIBUTION. Everything above is ABSOLUTE P&L, which for a long-only
    # AI/semis book in a bull tape is dominated by beta — and win_rate_pct is not
    # cosmetic: calibration_gate flags "losing" buckets on it and then demands an
    # exception paragraph to trade them. Without the excess column that gate is a
    # beta detector wearing a skill detector's clothes. Fail-soft: attribution is
    # reporting, and a yfinance outage must never break the run summary.
    try:
        from tools.benchmark import (attach_benchmark_to_closed, book_risk_metrics,
                                     summarize_excess)
        attach_benchmark_to_closed(closed)
        stats["vs_benchmark"] = summarize_excess(closed)
        stats["risk_adjusted"] = book_risk_metrics(equity_hist)
    except Exception as e:
        stats["vs_benchmark"] = {"status": "unavailable", "error": str(e)}
    return stats


def compute_calibration(closed: list[dict]) -> dict | None:
    """Are the brain's stated confidences honest? Bucket closed trades by the confidence
    it claimed at entry and compare to the realized win rate. Directly feeds the CLAUDE.md
    rule 'if your 0.70+ bucket wins <50%, your scale is inflated - recalibrate'. Returns
    None until there is anything to measure."""
    graded = [t for t in closed if isinstance(t.get("confidence"), (int, float))]
    if not graded:
        return None
    # 0.50-0.59 exists for calibration-probe fills (autonomy_config
    # trade_quality_requirements.calibration_probe): the probes' whole purpose
    # is to be measured, so they must not fall out of the published table.
    buckets = {"0.50-0.59": (0.50, 0.60), "0.60-0.69": (0.60, 0.70),
               "0.70-0.79": (0.70, 0.80), "0.80+": (0.80, 1.01)}
    out = {}
    for label, (lo, hi) in buckets.items():
        rows = [t for t in graded if lo <= t["confidence"] < hi]
        if not rows:
            continue
        wins = sum(1 for t in rows if (t.get("pnl_usd") or 0) > 0)
        avg_conf = round(sum(t["confidence"] for t in rows) / len(rows) * 100, 1)
        # Sample floor. A rate computed on one or two trades is noise wearing a
        # decimal point, and this one shipped to a PUBLIC dashboard: at n=2 it
        # published calibration_gap_pct -65.0. Counts remain facts and are always
        # shown; RATES are withheld until they mean something. tools/benchmark.py
        # (MIN_OBS = 60, returns None below it) is the pattern being copied.
        enough = len(rows) >= MIN_BUCKET_TRADES
        win_rate = round(wins / len(rows) * 100, 1) if enough else None
        out[label] = {"trades": len(rows), "wins": wins,
                      "win_rate_pct": win_rate,
                      "avg_stated_confidence_pct": avg_conf,
                      "calibration_gap_pct": (round(win_rate - avg_conf, 1)
                                              if enough else None),
                      "sufficient_sample": enough,
                      "min_trades_for_rate": MIN_BUCKET_TRADES}
    high = [t for t in graded if t["confidence"] >= 0.70]
    high_enough = len(high) >= MIN_BUCKET_TRADES
    high_wr = (round(sum(1 for t in high if (t.get("pnl_usd") or 0) > 0)
                     / len(high) * 100, 1) if high and high_enough else None)
    return {
        "note": "Realized win rate vs the confidence you STATED at entry, by bucket. A "
                "large negative calibration_gap_pct means your confidence is inflated; "
                "cap stated confidence until the gap closes (per the Learning Protocol).",
        "by_confidence": out,
        "total_graded_trades": len(graded),
        "sufficient_sample": len(graded) >= MIN_BUCKET_TRADES,
        "high_conf_0_70_plus": {"trades": len(high), "win_rate_pct": high_wr,
                                "sufficient_sample": high_enough,
                                "inflated": bool(high_wr is not None and high_wr < 50)},
    }


def recent_improvements(limit: int = 30) -> list[dict]:
    notes = []
    for f in sorted((ROOT / "journal" / "improvements").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            notes.append({"date": rec["ts"][:10], "note": rec["note"]})
    return notes[-limit:][::-1]


# ---------------------------------------------------------------------------
# Time: user-facing dates use the MARKET timezone (ET), never UTC. An evening run
# (after 8pm ET) is still ~02:00 UTC the NEXT day - stamping UTC would show viewers
# "tomorrow's" date on the review blurb, the equity curve, and X posts.
# ---------------------------------------------------------------------------
def et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # tzdata unavailable (some minimal Linux images): approximate ET as UTC-4 (EDT).
        # Only affects a date stamp; off by at most an hour near midnight in winter.
        return datetime.now(timezone.utc) - timedelta(hours=4)


def et_date() -> str:
    return et_now().date().isoformat()


def to_et_date(iso_ts: str | None) -> str | None:
    """Convert a UTC ISO timestamp to its ET calendar date (YYYY-MM-DD)."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        try:
            return (datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
                    - timedelta(hours=4)).date().isoformat()
        except Exception:
            return iso_ts[:10]


def proposal_ev(p: dict):
    """Probability-weighted expected value derived from a BUY proposal's own
    scenarios - published next to the proposal so every thesis carries its
    honest math. None for non-BUYs or when scenarios are unusable."""
    try:
        if str(p.get("action", "")).upper() != "BUY":
            return None
        from tools.scenario_ev import expected_value
        return expected_value(p.get("scenarios"), p.get("entry_price_max"),
                              p.get("holding_horizon_days"))
    except Exception:
        return None


def sector_map() -> dict:
    """ticker -> sector, from data/universe.json (for dashboard exposure)."""
    try:
        sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
        return {t.upper(): s for s, ts in sectors.items() for t in ts}
    except Exception:
        return {}


def sector_exposure(portfolio: dict) -> list[dict]:
    """Market value + share of equity per sector for the open book — the
    machine-readable counterpart of the enforced sector-concentration cap."""
    smap = sector_map()
    equity = portfolio.get("total_equity_usd") or 0
    by: dict = {}
    for p in portfolio.get("positions", []):
        s = smap.get(str(p.get("ticker", "")).upper()) or "unmapped"
        by[s] = by.get(s, 0.0) + (p.get("market_value_usd") or 0.0)
    return sorted(({"sector": s, "value_usd": round(v, 2),
                    "pct_of_equity": round(v / equity * 100, 1) if equity else None}
                   for s, v in by.items()),
                  key=lambda d: -(d["value_usd"] or 0))


def build_trade_events() -> list[dict]:
    """Every filled BUY/SELL with its ET date - the equity curve annotates these so the
    line tells a story (entries, exits, stop-outs) instead of being an anonymous squiggle."""
    state_file = ROOT / "state" / "portfolio.json"
    if not state_file.exists():
        return []
    history = json.loads(state_file.read_text()).get("history", [])
    verdicts = {(t["ticker"].upper(), t["closed_at"]): t.get("verdict")
                for t in compute_closed_trades()}
    events = []
    for fill in history:
        if fill.get("status") != "filled":
            continue
        d = to_et_date(fill.get("filled_at"))
        ev = {"date": d, "ticker": fill["ticker"].upper(),
              "action": fill["action"], "price": fill.get("fill_price")}
        if fill["action"] == "SELL_TO_CLOSE":
            ev["verdict"] = verdicts.get((fill["ticker"].upper(), d))
        events.append(ev)
    return events


def recent_rejected_ideas(scan_runs: int = 25, limit: int = 12) -> list[dict]:
    """The names the agent LOOKED AT and passed on across the most recent runs, deduped
    to one row per ticker. This is the "why it didn't buy" surface: on an empty book the
    formal proposals list is blank every run, but rejected_ideas is where the real work is
    - each is a ticker the agent researched and consciously skipped, with the reason.

    Sources journal/runs/*.jsonl (each run summary carries a rejected_ideas list of
    {ticker, reason}). Scans the most recent `scan_runs` run records newest-first, keeps
    the MOST RECENT reason per ticker, counts how many recent runs rejected it, and stamps
    the ET date it was last passed on. Returns at most `limit` rows, freshest first."""
    d = ROOT / "journal" / "runs"
    if not d.exists():
        return []
    records: list[dict] = []
    for f in sorted(d.glob("*.jsonl")):
        try:
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
    # Newest first; a missing ts sorts oldest so a malformed row can't hide a fresh one.
    records.sort(key=lambda r: r.get("ts") or "", reverse=True)
    seen: dict[str, dict] = {}
    for rec in records[:scan_runs]:
        when = to_et_date(rec.get("ts"))
        for idea in (rec.get("rejected_ideas") or []):
            tk = str((idea or {}).get("ticker", "")).upper().strip()
            reason = str((idea or {}).get("reason", "")).strip()
            if not tk:
                continue
            row = seen.get(tk)
            if row is None:
                # First (newest) sighting sets the displayed reason and date.
                seen[tk] = {"ticker": tk, "reason": reason,
                            "last_seen": when, "count": 1}
            else:
                row["count"] += 1
    rows = list(seen.values())
    # Freshest last_seen first, then the most-repeatedly-rejected.
    rows.sort(key=lambda r: (r.get("last_seen") or "", r.get("count", 0)), reverse=True)
    return rows[:limit]


def build_position_charts(positions: list[dict]) -> dict:
    """~90 daily OHLC bars per open holding plus its plan levels, for the per-position
    charts (entry/stop/target drawn on the tape). Fail-soft: a blocked fetch just omits
    that name. Written to its own file so latest.json stays slim."""
    out = {}
    if not positions:
        return out
    try:
        import yfinance as yf
    except Exception:
        return out
    for pos in positions:
        t = pos.get("ticker", "").upper()
        plan = pos.get("original_plan") or {}
        try:
            df = yf.download(t, period="5mo", interval="1d", auto_adjust=True,
                             progress=False)
            if df is None or df.empty:
                continue
            bars = []
            for idx, row in df.tail(90).iterrows():
                bars.append({
                    "date": idx.date().isoformat(),
                    "open": round(float(row["Open"]), 2), "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2), "close": round(float(row["Close"]), 2),
                })
            out[t] = {
                "bars": bars,
                "avg_cost": pos.get("avg_cost"),
                "last_price": pos.get("last_price"),
                "entry": plan.get("entry_price_max"),
                "stop": plan.get("stop_loss"),
                "target": plan.get("target_price"),
                "opened_at": to_et_date(pos.get("opened_at")),
            }
        except Exception:
            continue
    return out


def update_watchlist_outcomes(watchlist: list, prices: dict, positions: list) -> list[dict]:
    """Track whether the agent's watchlist calls play out: when a name was first watched
    and at what price, whether price later reached its stated would_buy_at level, whether
    the agent actually bought it, and how far it has moved since. Persisted across runs so
    the site can grade the agent's foresight, not just its trades."""
    f = ROOT / "dashboard" / "data" / "watchlist_outcomes.json"
    try:
        tracked = {d["ticker"]: d for d in json.loads(f.read_text())} if f.exists() else {}
    except Exception:
        tracked = {}
    held = {p.get("ticker", "").upper() for p in positions}
    ever_bought = held | {e["ticker"].upper() for e in build_trade_events()
                          if e["action"] == "BUY"}
    today = et_date()
    current = {str(w.get("ticker", "")).upper(): w for w in (watchlist or []) if w.get("ticker")}

    for tk, w in current.items():
        px = prices.get(tk)
        rec = tracked.get(tk) or {"ticker": tk, "first_watched": today,
                                  "price_when_added": px, "hit_buy_level": False}
        rec["currently_watched"] = True
        rec["would_buy_at"] = w.get("would_buy_at")
        rec["one_line"] = w.get("one_line")
        if px is not None:
            rec["latest_price"] = px
            base = rec.get("price_when_added") or px
            rec["move_pct_since_watched"] = round((px / base - 1) * 100, 1) if base else None
        # Buy level via the SAME $-anchored parser the trigger checker uses -
        # the old first-bare-number regex read "50-over-200" as a $50 level and
        # "7/29 earnings" as $7, so the public foresight grades were wrong.
        lvl = parse_price_level(w.get("would_buy_at"))
        if rec.get("parsed_level") != lvl:
            # Level changed (or is no longer a price): a sticky hit graded
            # against the OLD text is stale evidence - reset and regrade.
            rec["parsed_level"] = lvl
            rec["hit_buy_level"] = False
            rec.pop("hit_date", None)
            # The unbought-hit count is decay evidence against THIS level, so it
            # resets with it. A genuine requalification (a new, honestly argued
            # level) therefore earns a clean slate; re-typing the same number
            # does not, because the parsed level is unchanged.
            rec.pop("unbought_hit_days", None)
        at_level = bool(lvl) and px is not None \
            and abs(px / lvl - 1) <= TRIGGER_TOLERANCE_PCT
        # THE EVENT/DATE GATE (2026-08-03, lesson LP-0693555695 from run
        # 20260717-e6c993). parse_price_level reads ONE number out of the
        # sentence and this function then graded the whole compound condition as
        # met the moment price came within 2% of it. On 07-17 that scored AMD as
        # having reached "a confirmed positive reaction to the 7/22-23 Advancing
        # AI event ... basing $500-520 -- not a bounce off today's low alone" —
        # five days BEFORE the event the sentence is entirely about, against a
        # clause that rules out the pre-event dip in plain words.
        #
        # A false hit is no longer just a wrong foresight grade. It feeds
        # tools/engagement.count_unbought_hits -> decay_watchlist, so three of
        # them FORCE-DROP the name off the stored watchlist for misses that never
        # happened (the setup was not live, so there was nothing to miss), and
        # each one obliges the run to file a trigger_reviews row about it.
        #
        # tools/trigger_conditions fails open by construction: only a hard gate
        # with a resolved date, present in EVERY "or" branch, holds a hit back.
        # `would_buy_at_original` is passed because signal_discipline's dead-leg
        # excision takes the rest of the clause with it and can carry the event
        # gate away ("close above $175 with volume confirmation after the 8/4
        # print" is stored as "close above $175"), so the pre-sanitize text is
        # where the gate still lives.
        gate = evaluate_trigger(w.get("would_buy_at"), as_of=today,
                                original=w.get("would_buy_at_original")) \
            if at_level else {"suppress": False}
        rec.pop("trigger_gate", None)
        if at_level and gate.get("suppress"):
            # Recorded, never silent: an invisible filter is how a REAL miss
            # would go unnoticed, which is the more expensive failure now.
            rec["trigger_gate"] = {"held_on": today, "reason": gate.get("reason"),
                                   "note": gate.get("note"),
                                   "gates": gate.get("blocking") or []}
        elif at_level:
            rec["hit_buy_level"], rec["hit_date"] = True, rec.get("hit_date") or today
            # DISTINCT DAYS at the stated level without a buy. hit_buy_level is
            # sticky (one flag, set once), which is right for foresight grading
            # but cannot answer "how many times has this name reached my level
            # while I did nothing" — the ANET/GE question. Seven runs in one day
            # is one miss, so days are the unit. Reset with parsed_level above,
            # because a hit graded against an old level is stale evidence.
            if tk not in ever_bought:
                days = rec.get("unbought_hit_days")
                days = days if isinstance(days, list) else []
                if today not in days:
                    days.append(today)
                rec["unbought_hit_days"] = days[-20:]
        rec["acted"] = tk in ever_bought
        if rec["acted"]:
            rec.pop("unbought_hit_days", None)
        tracked[tk] = rec

    for tk, rec in tracked.items():
        if tk not in current:
            rec["currently_watched"] = False
            rec.setdefault("dropped_date", today)
            px = prices.get(tk)
            if px is not None:
                rec["latest_price"] = px
                base = rec.get("price_when_added") or px
                rec["move_pct_since_watched"] = round((px / base - 1) * 100, 1) if base else None
            rec["acted"] = rec.get("acted") or (tk in ever_bought)

    rows = sorted(tracked.values(),
                  key=lambda r: (not r.get("currently_watched"), r.get("first_watched") or ""),
                  reverse=False)[:60]
    try:
        f.write_text(json.dumps(json_safe(rows), indent=2))
    except Exception:
        pass
    return rows


def append_runs_index(run_id: str, mode: str, fills: list, commentary: str | None,
                      no_trade_reason: str | None,
                      rejected_ideas: list | None = None) -> None:
    """A compact index of every published run so the site can offer a browsable archive
    linking each run's full reasoning (dashboard/data/run_<id>.json). Carries the tickers
    the agent passed on this run (rejected_tickers) so the archive can show "why it didn't
    buy" at a glance without opening each run - on an empty book that is the only content
    most runs have."""
    f = ROOT / "dashboard" / "data" / "runs_index.json"
    try:
        idx = json.loads(f.read_text()) if f.exists() else []
    except Exception:
        idx = []
    headline = (commentary or no_trade_reason or "").strip().split(". ")[0][:180]
    rejected_tickers = sorted({str((r or {}).get("ticker", "")).upper().strip()
                               for r in (rejected_ideas or [])
                               if str((r or {}).get("ticker", "")).strip()})
    entry = {"run_id": run_id, "date": et_date(), "mode": mode,
             "n_fills": len(fills or []),
             "tickers_traded": sorted({str(x.get("ticker", "")).upper() for x in (fills or [])}),
             "rejected_tickers": rejected_tickers,
             "headline": headline}
    idx = [e for e in idx if e.get("run_id") != run_id]
    idx.append(entry)
    try:
        f.write_text(json.dumps(idx[-400:], indent=2))
    except Exception:
        pass
