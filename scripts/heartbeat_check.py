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
# One missed slot is routine: GitHub coalesces cron under load (documented in
# tools/live_prices.py:22-24 and observed ~hourly on 2026-07-15). Two consecutive
# misses is a pipeline that is actually down.
MAX_MISSED_SLOTS = 2
# The relay bundle goes stale at 4h (orchestrator's own threshold). Warn before that.
MAX_BUNDLE_AGE_H = 6.0


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
        reasons.append(
            f"{missed} scheduled run(s) missed today "
            f"(expected {health.get('expected_runs_so_far')}, "
            f"completed {health.get('completed_scheduled_runs')})")

    age = health.get("bundle_age_hours")
    if isinstance(age, (int, float)) and age > MAX_BUNDLE_AGE_H:
        reasons.append(
            f"relay bundle is {age:.1f}h old (> {MAX_BUNDLE_AGE_H}h) — the gatherers "
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
        print(f"OK: {h.get('completed_scheduled_runs')}/"
              f"{h.get('expected_runs_so_far')} runs today, "
              f"bundle {h.get('bundle_age_hours')}h old")
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
