"""Between-cycle stop enforcement — the ONLY job is honoring stops already recorded.

WHY THIS EXISTS

Stops in this system are numbers in state/portfolio.json, not resting orders at the
broker: every order body in execution/ is `type: "market"`. Enforcement therefore
only happened when a full trading cycle ran, and the configured slots are
06:00/09:00/10:00/12:00/14:00/16:00/17:30 ET. Measured exposure:

    10:00 -> 12:00, 12:00 -> 14:00, 14:00 -> 16:00   ~2 hours each
    16:00 -> next actionable session                 ~17.5 hours
    Friday close -> Monday open                      ~65.5 hours

A stop breached at 10:05 was not acted on until 12:00. This closes the INTRADAY
half of that gap by reusing the existing, fully-integrated exit_guard path on the
5-minute live-price tick that already runs (scripts/push_live_prices.sh +
.github/workflows/live-prices.yml), which until now refreshed quotes and called
trigger_watch — a WATCHLIST tool that explicitly skips held tickers. It did
everything except look at the stops.

Overnight and weekend gaps are NOT addressed here and cannot be without resting
broker orders; that requires an out-of-band fill ingestion path first (a broker-side
fill is invisible to reconcile(), produces no journal line, no closed trade, and
leaves a phantom position that exit_guard re-fires on forever).

DESIGN CONSTRAINTS, each load-bearing

* NO LLM. exit_guard is pure Python. This adds zero `claude -p` invocations, so it
  costs no usage and cannot be affected by model availability.
* NEVER OPENS A POSITION. Only SELL_TO_CLOSE via exit_guard. There is no path here
  that can increase risk.
* PROTECTIVE EXITS IGNORE THE KILL SWITCH AND THE RUN BUDGET. orchestrator.py
  returns on any preflight halt BEFORE apply_safety_layer, so flipping the emergency
  switch, or merely exhausting the daily run budget, silently disabled stop
  enforcement. scripts/execute_order_intents.py already got this right ("protective
  exits must never be blocked by a halt") and this mirrors it. A kill switch should
  stop the system OPENING risk, not stop it CLOSING risk.
* TAKES THE RUN LOCK. A trading cycle mutating the ledger concurrently would race.
  If a cycle holds the lock this exits quietly — the cycle enforces stops itself.
* REFUSES TO ACT ON STALE PRICES. Enforcing a stop against a stale prior close is
  worse than waiting; the next tick is minutes away.

KNOWN LIMITATION, deliberately not papered over: exit_guard compares a SAMPLED mark,
never the intraday low. A stop breached at 10:30 that recovers by 10:35 is still not
recorded as a stop-out. Tighter polling narrows the window; only a resting order
removes the bias.

CLI: python -m scripts.stop_watch [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution import exit_guard  # noqa: E402
from runlib import preflight  # noqa: E402
from tools.envload import load_env  # noqa: E402
from tools.portfolio_state import get_portfolio_state  # noqa: E402

load_env()

# A mark older than this is not a basis for firing a stop. Mirrors the overlay
# window the trading cycle uses (schedule.live_prices.max_age_min).
DEFAULT_MAX_AGE_MIN = 30


def _cfg() -> dict:
    return json.loads((ROOT / "autonomy_config.json").read_text())


def _live_prices(max_age_min: int) -> tuple[dict, dict]:
    """({TICKER: price}, meta). Empty when the snapshot is missing or too old."""
    meta = {"status": "ok", "age_min": None, "n": 0}
    snap_file = ROOT / "state" / "live_prices.json"
    if not snap_file.is_file():
        meta["status"] = "no_snapshot"
        return {}, meta
    try:
        blob = json.loads(snap_file.read_text())
    except Exception as e:
        meta["status"] = f"unreadable:{e}"
        return {}, meta

    as_of = blob.get("as_of")
    try:
        ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    except Exception:
        meta["status"] = "unparseable_as_of"
        return {}, meta

    meta["age_min"] = round(age_min, 1)
    if age_min > max_age_min:
        # Fail CLOSED on the ACTION, not on the position: skip this tick rather than
        # fire a stop from a stale prior close. The next tick is minutes away.
        meta["status"] = "stale"
        return {}, meta

    prices = {}
    for t, v in (blob.get("prices") or {}).items():
        px = v.get("price") if isinstance(v, dict) else v
        if isinstance(px, (int, float)) and px > 0:
            prices[str(t).upper()] = float(px)
    meta["n"] = len(prices)
    return prices, meta


def run(dry_run: bool = False) -> dict:
    """One stop-enforcement pass. Never raises; returns a structured result."""
    started = datetime.now(timezone.utc).isoformat()
    out = {"status": "ok", "started_at": started, "checked": 0,
           "forced": [], "fills": [], "note": None}
    cfg = _cfg()

    if cfg.get("mode", {}).get("trading_mode") == "dry_run":
        out.update(status="skipped", note="trading_mode=dry_run")
        return out

    portfolio = get_portfolio_state()
    positions = portfolio.get("positions") or []
    if not positions:
        out.update(status="skipped", note="flat book")
        return out
    out["checked"] = len(positions)

    max_age = ((cfg.get("schedule") or {}).get("live_prices") or {}).get(
        "max_age_min", DEFAULT_MAX_AGE_MIN)
    prices, meta = _live_prices(max_age)
    out["price_meta"] = meta
    if not prices:
        out.update(status="skipped",
                   note=f"no usable live prices ({meta['status']}, "
                        f"age={meta.get('age_min')}m) — refusing to enforce stops "
                        f"against a stale mark")
        return out

    held = {str(p.get("ticker", "")).upper() for p in positions}
    if not held & set(prices):
        out.update(status="skipped", note="no fresh price for any holding")
        return out

    atr_map = {}
    try:
        scan = json.loads((ROOT / "data" / "universe_scan.json").read_text())
        atr_map = scan.get("atr_by_ticker") or {}
    except Exception:
        pass  # gap modeling degrades; enforcement does not

    forced = exit_guard.check_forced_exits(portfolio, prices, atr_map)
    out["forced"] = forced
    if not forced:
        return out

    if dry_run:
        out.update(status="dry_run", note=f"{len(forced)} exit(s) would fire")
        return out

    # The lock is taken ONLY once there is something to execute: a read-only tick
    # must never block a scheduled trading cycle from starting.
    run_id = f"stopwatch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"
    if not preflight.acquire_run_lock(run_id):
        out.update(status="deferred",
                   note="a trading cycle holds the run lock; it enforces stops itself")
        return out
    try:
        out["fills"] = exit_guard.execute_forced_exits(forced, prices, run_id, atr_map)
        out["run_id"] = run_id
    finally:
        preflight.release_run_lock()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would fire without placing orders")
    args = ap.parse_args()
    try:
        res = run(dry_run=args.dry_run)
    except Exception as e:  # a watchdog that crashes is worse than one that reports
        res = {"status": "error", "error": str(e)}
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
