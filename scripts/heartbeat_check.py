#!/usr/bin/env python3
"""Out-of-band alarm for a silently dead pipeline.

runlib/analytics.build_health() has computed missed slots and bundle age for a long
time, and its own docstring names the reason: "the plan-mode parse bug ran for DAYS
unnoticed". The remedy shipped at the time was a number on a webpage. A grep for
`notify|alert|pushover|slack|smtp` across scripts/, runlib/, orchestrator.py and
.github/workflows/ returned NOTHING — so the only consumer was a dashboard card that
nobody is paged about. A metric with no egress is not monitoring; it is a metric.

This gives the health signal a way OUT of the box, with no new dependencies:

  1. EXIT CODE. Non-zero when the pipeline is unhealthy, so a scheduled GitHub
     Action turns red. A red badge on your own repo is the cheapest real alerting
     that exists, and it reaches you without any credential, service or webhook.
  2. WEBHOOK (optional). POSTs a compact JSON alert when EE_ALERT_WEBHOOK is set.
     Works with Slack/Discord incoming webhooks as-is.
  3. macOS NOTIFICATION (optional). --notify posts to Notification Center when run
     from the local LaunchAgent, where a non-zero exit is invisible.

Deliberately read-only over journals and dashboard data: an alarm that can mutate
state is an alarm that can cause the incident it is meant to report.

  python scripts/heartbeat_check.py            # exit 1 if unhealthy
  python scripts/heartbeat_check.py --notify   # + macOS banner
  python scripts/heartbeat_check.py --json     # machine-readable, always exit 0
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# How many scheduled slots may be missed before this is an alarm rather than noise.
#
# WAS 2, AND THAT IS WHY THE 2026-07-20 OUTAGE WENT UNREPORTED. The check is
# `missed > MAX_MISSED_SLOTS`, and the morning heartbeat fires at 10:30 ET when only
# THREE slots have elapsed (6am, 9am, 10am). A tolerance of 2 therefore demanded a
# 100% morning failure before it would page — on the day in question the local trader
# was entirely dead, the cloud had covered exactly one slot, `missed` came to 2, and
# `2 > 2` is False. The alarm reported healthy through a total node outage.
#
# Now 0 — ANY missed slot pages. Two reasons that is not the alarm-fatigue mistake it
# looks like:
#
#  1. JITTER TOLERANCE MOVED TO THE RIGHT PLACE. It used to live here, as a count. It
#     now lives in runlib.analytics.slot_report as a one-hour GRACE WINDOW per slot: a
#     slot that is merely late is "pending" and never reaches this check. GitHub's cron
#     coalescing (tools/live_prices.py:22-24, ~hourly on 2026-07-15) is absorbed by
#     time, which is the dimension it actually varies in. A count threshold could not
#     tell a late slot from a dead one; a window can.
#  2. A SLOT AN HOUR LATE IS NOT A LATE SLOT, IT IS A DIFFERENT TRADE. A 2pm run landing
#     at 3:30pm prices a different market. Per user policy 2026-07-20, every missed slot
#     is a lost chance to enter, exit, or learn — so one is worth knowing about.
MAX_MISSED_SLOTS = 0
# The relay bundle goes stale at 4h (orchestrator's own threshold). Warn before that.
#
# THIS IS A BAND, NOT A CONSTANT, and it used to be a flat 6.0 that paged all
# weekend. The gather cron runs "40 3 * * *" on non-trading days — once daily — so
# a perfectly healthy Saturday bundle is routinely 12-24h old, and a flat 6h
# threshold turned every weekend into a red badge for a pipeline with nothing to
# do. runlib.analytics.build_health had already worked this out (2h inside market
# hours, 8h outside) and this file simply did not read it. Same idea, one more
# step: on a day the market never opens, staleness is not a symptom.
MAX_BUNDLE_AGE_H = 6.0            # weekday, outside market hours
MAX_BUNDLE_AGE_H_MARKET = 3.0     # weekday, 9:30-16:00 ET — a stale bundle here
                                  # is the price-staleness incident of 2026-07-23
MAX_BUNDLE_AGE_H_CLOSED = 26.0    # weekend/holiday — one daily gather + headroom


def assess() -> dict:
    """(healthy, reasons, health) — pure over on-disk state."""
    from runlib.analytics import build_health
    health = build_health()
    reasons = []

    # Key names come from runlib.analytics.build_health() — verified against real
    # output, not assumed. A mis-named key here would make the alarm silently never
    # fire, which is precisely the failure class this script exists to end.
    # A holiday has no scheduled slots, so counting them as missed would page you
    # every Thanksgiving and train you to ignore the alarm.
    try:
        from tools.market_calendar import session as _session
        _s = _session()
        holiday = _s["source"] == "calendar" and not _s["is_trading_day"]
    except Exception:
        holiday = False

    missed = health.get("missed")
    if holiday and isinstance(missed, int) and missed > 0:
        missed = 0
    if isinstance(missed, int) and missed > MAX_MISSED_SLOTS:
        which = health.get("missed_slots") or []
        died = health.get("died_slots") or []
        detail = f" [{', '.join(which)} ET]" if which else ""
        # A died slot fired and left a session behind; a plain missed slot never fired.
        # Naming which is which points the operator straight at the right thing to read.
        died_note = f"; fired-and-died (session to read): {', '.join(died)}" if died else ""
        # REPORT ON ONE BASIS. This used to read "(expected N, completed M)" where
        # N counted SLOTS and M counted RUNS, so a real miss was announced as
        # "1 scheduled run(s) missed today [10:00 ET] (expected 3, completed 3)" —
        # and on 07-23, "(expected 7, completed 9)". Both are arithmetic that
        # cannot be true, both were correct underneath, and an alarm nobody can
        # parse is an alarm nobody reads. Slots are now compared to slots, with
        # the raw run count kept as a separate, labelled number.
        covered = health.get("slots_covered")
        elapsed = health.get("expected_runs_so_far")
        drift = health.get("max_drift_min")
        drift_note = (f"; worst drift +{drift}min" if isinstance(drift, int)
                      and drift > 0 else "")
        reasons.append(
            f"{missed} scheduled slot(s) missed today{detail} "
            f"({covered}/{elapsed} elapsed slots covered by "
            f"{health.get('runs_journaled')} journaled run(s)){drift_note}{died_note}")

    age = health.get("bundle_age_hours")
    # Pick the band that matches what the pipeline is supposed to be doing right
    # now. Both flags derive from `holiday` — i.e. from tools.market_calendar —
    # rather than from datetime.weekday(). The calendar already knows a Saturday
    # is not a trading day, and routing through it keeps this mockable: tests pin
    # the calendar via _force_trading_day so they assert on the THRESHOLD instead
    # of passing or failing depending on which day the suite happens to run.
    closed_day = holiday
    try:
        from runlib.core import et_now as _et_now
        _n = _et_now()
        _h = _n.hour + _n.minute / 60
        in_market = (not closed_day) and 9.5 <= _h <= 16
    except Exception:
        in_market = False
    limit = (MAX_BUNDLE_AGE_H_MARKET if in_market
             else MAX_BUNDLE_AGE_H_CLOSED if closed_day
             else MAX_BUNDLE_AGE_H)
    if isinstance(age, (int, float)) and age > limit:
        when = ("during market hours" if in_market
                else "on a non-trading day" if closed_day else "off-hours")
        reasons.append(
            f"relay bundle is {age:.1f}h old (> {limit}h {when}) — the gatherers "
            f"feeding cloud runs may be dead")

    # Capability audit: the same probes the run uses, surfaced out of band. A guard
    # that quietly stops being wired is exactly what this whole alarm exists for.
    try:
        import sys as _sys
        _sys.path.insert(0, str(ROOT))
        from runlib.capabilities import audit_capabilities
        caps = audit_capabilities()
        if not caps["ok"]:
            for name in caps["dead"]:
                reasons.append(f"capability DEAD: {name} — "
                               f"{caps['results'][name]['detail'][:160]}")
    except Exception as e:
        reasons.append(f"capability audit could not run: {e}")

    if (ROOT / "state" / "KILL_SWITCH").exists():
        # Not a failure, but it must be surfaced: a halted system looks identical to
        # a healthy idle one from the outside, and that is how a halt outlives its
        # reason.
        reasons.append("KILL_SWITCH is engaged — no runs will execute until removed")

    return {"healthy": not reasons, "reasons": reasons, "health": health}


def notify_macos(title: str, message: str) -> None:
    """Best-effort Notification Center banner. Never raises."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def post_webhook(url: str, payload: dict) -> bool:
    """POST the alert. Returns success; never raises."""
    try:
        body = json.dumps({"text": payload["text"], **payload}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notify", action="store_true",
                    help="post a macOS notification when unhealthy")
    ap.add_argument("--json", action="store_true",
                    help="print the assessment as JSON and always exit 0")
    args = ap.parse_args()

    try:
        result = assess()
    except Exception as e:
        # The alarm failing is itself an alarm — never swallow it into a green exit.
        print(f"heartbeat check FAILED to run: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    if result["healthy"]:
        h = result["health"]
        drift = h.get("max_drift_min")
        drift_note = (f", worst drift +{drift}min" if isinstance(drift, int)
                      and drift > 0 else "")
        # Same one-basis rule as the unhealthy path: slots against slots.
        print(f"OK: {h.get('slots_covered')}/{h.get('expected_runs_so_far')} "
              f"elapsed slots covered ({h.get('runs_journaled')} runs journaled), "
              f"bundle {h.get('bundle_age_hours')}h old{drift_note}")
        return 0

    summary = "; ".join(result["reasons"])
    print(f"UNHEALTHY: {summary}", file=sys.stderr)

    if args.notify:
        notify_macos("East Equity Agent", summary[:240])

    hook = os.environ.get("EE_ALERT_WEBHOOK")
    if hook:
        ok = post_webhook(hook, {"text": f"East Equity Agent UNHEALTHY: {summary}",
                                 "reasons": result["reasons"]})
        print(f"  webhook: {'sent' if ok else 'FAILED'}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
