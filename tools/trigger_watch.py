"""Event-driven trigger runs — act when a watchlist entry level actually trades.

Scheduled slots run at 9/10/12/2/4 ET. A watchlist name can hit its published
would_buy_at level minutes after a slot and sit there for two hours before the
agent looks again. The local live-price feeder already refreshes holdings +
watchlist quotes every ~5 minutes; this module rides that tick:

    live tick -> price inside the trigger band on TWO CONSECUTIVE ticks
              -> spawn ONE focused trading run (holdings_watchlist depth)

The spawned run is a NORMAL run: the brain re-underwrites the alert and
proposes a BUY only if the fat-pitch bar clears, then the risk desk, the
validator, and the broker gates apply unchanged. This module never places
orders itself — it only moves the research forward to the moment the level is
actually trading, instead of up to two hours later.

Deterministic guardrails (state in state/trigger_watch.json):
  - confirmation: two consecutive feeder ticks 4-20 minutes apart inside the
    band — a single print or wick does not confirm an entry point
  - live snapshot must be fresh (<= 15 min) — never confirm on stale quotes
  - ET window 9:35am-3:45pm weekdays
  - per-ticker cooldown 4h, max 2 trigger runs per ET day, >= 45 min between
    trigger runs; tickers already held are skipped
  - stands down when RUN_LOCK or KILL_SWITCH exists (and the spawned run's own
    preflight re-checks both, plus the cross-node lease)

CLI: python -m tools.trigger_watch          # check + maybe spawn (feeder hook)
     python -m tools.trigger_watch --dry    # decide + print, never spawn
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_FILE = ROOT / "state" / "trigger_watch.json"

CONFIRM_MIN_MINUTES = 4.0     # two ticks closer than this = same print, not confirmation
# Pending older than this = feed gap; restart confirmation.
#
# WAS 20.0, WHICH IS WHY THIS MODULE HAS NEVER FIRED A SINGLE RUN (state/
# trigger_watch.json still reads runs_by_day: {} as of 2026-07-23, with pending marks
# frozen at 07-16). 20 minutes was correct for the 5-minute launchd feeder this was
# built against — but that feeder is unloaded, and the cloud tick source is now the
# gather Action, which lands ~60-97 min apart because GitHub rations scheduled
# workflows to ~7-11 runs/day whatever interval you ask for (measured 2026-07-23).
# Every second sighting therefore arrived past the 20-minute ceiling, reset to "first
# sighting", and confirmation was structurally unreachable.
#
# 120 spans two consecutive gathers, so the two-tick sanity property is PRESERVED
# rather than dropped: a level still has to hold across two independent fetches, it
# just gets the real cadence to do it in. The tighter guards below are untouched and
# are what actually prevent run spam (4h per-ticker cooldown, 2 runs/day, 45-min gap,
# 9:35-15:45 ET only, held names skipped). And note what this gates: waking the brain,
# NOT buying. A false wake costs one cloud session; the validator, risk desk and
# fat-pitch bar still stand between a trigger and a fill.
CONFIRM_MAX_MINUTES = 120.0
# Unchanged at 15: the gather now calls refresh_from_book() immediately before this
# runs, so the snapshot really is minutes old. This stays a genuine freshness guard
# rather than a formality — if the fetch failed, we must not confirm on a stale mark.
SNAPSHOT_MAX_AGE_MIN = 15.0
COOLDOWN_HOURS = 4.0
MAX_RUNS_PER_DAY = 2
MIN_MINUTES_BETWEEN_RUNS = 45.0


def _load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text())
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1, default=str))
    except Exception:
        pass


def _minutes_since(iso: str, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(str(iso))).total_seconds() / 60.0
    except Exception:
        return None


def decide(state: dict, alerts: list[dict], held: list[str], now: datetime,
           et_date: str) -> tuple[dict, list[str]]:
    """Pure confirmation/cooldown/cap logic. Returns (new_state, tickers_to_run).

    tickers_to_run is the set of alerts confirmed THIS tick that pass every
    guard — the caller spawns at most one run covering all of them.
    """
    pending = dict(state.get("pending") or {})
    last_run = dict(state.get("last_run") or {})
    runs_by_day = dict(state.get("runs_by_day") or {})
    held_set = {str(t).upper() for t in held}
    alive = set()
    confirmed: list[str] = []

    for a in alerts:
        t = str(a.get("ticker", "")).upper()
        if not t or t in held_set:
            continue
        alive.add(t)
        since = _minutes_since(pending[t], now) if t in pending else None
        if since is None or since > CONFIRM_MAX_MINUTES:
            pending[t] = now.isoformat()          # first sighting (or stale restart)
            continue
        if since < CONFIRM_MIN_MINUTES:
            continue                              # same tick / too soon to confirm
        # Confirmed by two consecutive ticks. Per-ticker cooldown:
        lr = _minutes_since(last_run.get(t, ""), now)
        if lr is not None and lr < COOLDOWN_HOURS * 60:
            continue
        confirmed.append(t)

    # Names no longer alerting lose their pending mark (price left the band).
    pending = {t: ts for t, ts in pending.items() if t in alive}

    if not confirmed:
        return ({"pending": pending, "last_run": last_run,
                 "runs_by_day": runs_by_day}, [])

    # Global pacing: daily cap and minimum gap between trigger runs.
    if int(runs_by_day.get(et_date, 0)) >= MAX_RUNS_PER_DAY:
        return ({"pending": pending, "last_run": last_run,
                 "runs_by_day": runs_by_day}, [])
    gaps = [m for m in (_minutes_since(ts, now) for ts in last_run.values())
            if m is not None]
    if gaps and min(gaps) < MIN_MINUTES_BETWEEN_RUNS:
        return ({"pending": pending, "last_run": last_run,
                 "runs_by_day": runs_by_day}, [])

    for t in confirmed:
        last_run[t] = now.isoformat()
        pending.pop(t, None)
    runs_by_day = {et_date: int(runs_by_day.get(et_date, 0)) + 1}  # old days pruned
    return ({"pending": pending, "last_run": last_run,
             "runs_by_day": runs_by_day}, confirmed)


def _in_et_window(now_et: datetime) -> bool:
    if now_et.weekday() > 4:
        return False
    hhmm = now_et.hour * 100 + now_et.minute
    return 935 <= hhmm <= 1545


def main(dry: bool = False) -> int:
    from runlib.core import et_now, et_date
    from tools.watchlist_triggers import check_watchlist_triggers
    from tools.live_prices import load_live_prices

    now_et = et_now()
    if not _in_et_window(now_et):
        return 0
    now = datetime.now(timezone.utc)

    blob = load_live_prices(use_branch=False)  # feeder just wrote the local file
    age = _minutes_since(str(blob.get("as_of", "")), now)
    prices = blob.get("prices") or {}
    if age is None or age > SNAPSHOT_MAX_AGE_MIN or not prices:
        return 0

    try:
        latest = json.loads((ROOT / "dashboard" / "data" / "latest.json").read_text())
    except Exception:
        return 0
    watchlist = latest.get("watchlist") or []
    held = [p.get("ticker") for p in (latest.get("portfolio", {}).get("positions")
                                      or latest.get("positions") or [])]
    try:
        pf = json.loads((ROOT / "state" / "portfolio.json").read_text())
        held = [p.get("ticker") for p in pf.get("positions", [])]
    except Exception:
        pass

    alerts = check_watchlist_triggers(watchlist, prices)
    state, run_tickers = decide(_load_state(), alerts, held, now, et_date())
    _save_state(state)
    if not run_tickers:
        return 0

    summary = {"ts": now.isoformat(), "confirmed": run_tickers,
               "alerts": [a.get("ticker") for a in alerts]}
    if dry:
        print(json.dumps({**summary, "action": "dry_run"}))
        return 0
    if (ROOT / "state" / "KILL_SWITCH").exists():
        print(json.dumps({**summary, "action": "skipped_kill_switch"}))
        return 0
    if (ROOT / "state" / "RUN_LOCK").exists():
        print(json.dumps({**summary, "action": "skipped_run_in_flight"}))
        return 0

    log = (ROOT / "logs" / "trigger_runs.log").open("a")
    subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "-W", "ignore",
         str(ROOT / "orchestrator.py"), "--depth", "holdings_watchlist",
         "--trigger-run", ",".join(run_tickers)],
        cwd=str(ROOT), stdout=log, stderr=log, start_new_session=True)
    print(json.dumps({**summary, "action": "run_spawned"}))
    return 0


if __name__ == "__main__":
    sys.exit(main(dry="--dry" in sys.argv))
