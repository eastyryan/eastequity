"""Between-cycle stop enforcement — the ONLY job is honoring stops already recorded.

WHY THIS EXISTS

Stops in this system are numbers in state/portfolio.json, not resting orders at the
broker: every order body in execution/ is `type: "market"`. Enforcement therefore
only happened when a full trading cycle ran, and the configured slots are
06:00/08:45/10:30/12:00/14:00/16:00/17:30 ET. Measured exposure:

    10:30 -> 12:00, 12:00 -> 14:00, 14:00 -> 16:00   ~1.5-2 hours each
    16:00 -> next actionable session                 ~17.5 hours
    Friday close -> Monday open                      ~65.5 hours

A stop breached at 10:35 was not acted on until 12:00. This closes the INTRADAY
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
* TAKES THE RUN LOCK — for BOTH ledger-writing phases, briefly each (fixed
  2026-08-05: this bullet used to be claimed but only half true — broker
  maintenance ran with NO lock while a trading cycle's apply_corporate_actions
  read-modify-write could silently overwrite a tick-ingested fill or a fresh
  protective_stop record). Maintenance (fill ingestion + stop re-arming) locks
  first and is DEFERRED when a cycle holds the lock — the next tick is five
  minutes away, and reconciling is never urgent enough to race a live cycle for
  the file. Forced-exit execution locks again only once there is something to
  fire. The price read and breach check between the two stay lock-free, so a
  tick almost never blocks a scheduled cycle from starting.
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
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution import broker, exit_guard  # noqa: E402
from execution.reconcile_runner import run_reconcile  # noqa: E402
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

    # PER-TICKER trade-age gate. The snapshot age above only says how long ago the
    # feeder RAN. Alpaca's ladder falls back latest_trade -> minute_bar -> dailyBar
    # -> prevDailyBar, and a prior-session close was previously written with a fresh
    # wall-clock stamp, so it read as age 0.0m and was indistinguishable from a live
    # tick. On a thin name IEX can go 15-60 minutes between prints. The failure is
    # directional and silent: the stale price is the PRE-selloff price, it sits above
    # the stop, the stop does not fire, and the layer reports itself healthy.
    #
    # A price may only decide a stop if it is a genuine intraday mark whose TRADE
    # time is inside the window. Unknown provenance fails CLOSED — skipping this tick
    # is cheap (the next is minutes away); firing on a day-old close is not, and
    # neither is silently not firing.
    from tools.live_prices import price_is_enforceable
    price_meta = blob.get("price_meta") or {}
    prices, rejected = {}, []
    for t, v in (blob.get("prices") or {}).items():
        px = v.get("price") if isinstance(v, dict) else v
        if not (isinstance(px, (int, float)) and px > 0):
            continue
        tu = str(t).upper()
        ok, why = price_is_enforceable(price_meta.get(tu),
                                       max_age_min=max_age_min,
                                       require_intraday=True)
        if ok:
            prices[tu] = float(px)
        else:
            rejected.append(f"{tu}:{why}")
    if rejected:
        meta["rejected_prices"] = rejected[:12]
        meta["n_rejected"] = len(rejected)
        print(f"  ({len(rejected)} price(s) not enforceable: {rejected[:4]})")
    meta["n"] = len(prices)
    return prices, meta


def _load_atr_map() -> dict:
    """{TICKER: atr_pct} for the chandelier trail and the simulated gap model.

    This used to read data/universe_scan.json, WHICH HAS NEVER EXISTED - the scan is
    built in memory by tools/universe_scanner.py and only ever persisted inside a
    context bundle. A bare `except: pass` turned that permanently-failing read into a
    silent no-op, so atr_map was {} on every single tick, with two consequences:

      1. exit_guard._chandelier_stop returns None without ATR, so the trail NEVER
         ratcheted on a tick - only during full cycles, 4-6x/day, despite the
         documented "ratchets as the trade works". The existing persisted trail was
         still honoured, so this under-protected rather than over-fired.
      2. On the simulation backend the stop gap-through model needs atr_pct, so every
         tick-driven stop exit filled AT the stop with no gap - exactly the optimism
         execution_costs.model_stop_gaps was added to remove after the DELL
         408.00 -> 389.75 case.

    Reads the committed relay bundle first, then the newest local context archive.
    Returns {} only when genuinely nothing is available, and says so out loud.
    """
    for path in (ROOT / "data" / "cloud_context.json",
                 *sorted((ROOT / "state").glob("context_2*.json"),
                         key=lambda f: f.stat().st_mtime, reverse=True)[:1]):
        try:
            scan = json.loads(path.read_text()).get("universe_scan") or {}
            atr = scan.get("atr_by_ticker") or {}
            if atr:
                return atr
        except Exception:
            continue
    print("  (no atr_by_ticker available - trail cannot ratchet this tick)")
    return {}


def _sync_repo_ff(out: dict) -> None:
    """Best-effort fetch + fast-forward from origin BEFORE reconciling fills.

    CROSS-NODE DOUBLE-APPLY MITIGATION (audited 2026-08-05). Every idempotency
    layer around fill ingestion is per-checkout — history ids, the persisted
    ingested_broker_orders memo, and the journal are shared only "after git
    syncs" (execution/alpaca_broker.py, the ALREADY-APPLIED GUARD). The Actions
    executor runs on a fresh origin clone; this watchdog runs on the persistent
    local checkout. Both can see the same overnight stop fill before either
    pushes, and a double-applied realization corrupts the permanent track
    record and can trip broker_sync_suspect. Pulling origin first means fills
    the executor already applied and pushed are visible to THIS node's dedupe
    layers before it reconciles — narrowing the window (full closure would need
    a shared store, which this repo deliberately does not have).

    Strictly fail-open: this is a SAFETY tick, and a sync failure (offline,
    diverged history, dirty tree) must never brick the stop check that follows.
    --ff-only guarantees no merge commits and no rewriting of local work; when
    the fast-forward refuses, we print why and proceed on local state exactly
    as before this existed."""
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "fetch", "--quiet", "origin"],
                           capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            out["git_sync"] = f"fetch_failed: {(r.stderr or '').strip()[:160]}"
            print(f"  (git fetch failed — reconciling on local state: "
                  f"{(r.stderr or '').strip()[:120]})")
            return
        r = subprocess.run(["git", "-C", str(ROOT), "merge", "--ff-only", "@{u}"],
                           capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            out["git_sync"] = f"ff_refused: {(r.stderr or '').strip()[:160]}"
            print(f"  (fast-forward refused — reconciling on local state: "
                  f"{(r.stderr or '').strip()[:120]})")
        else:
            out["git_sync"] = "ok"
    except Exception as e:
        out["git_sync"] = f"error: {str(e)[:160]}"
        print(f"  (git sync skipped: {e})")


def _broker_maintenance(out: dict) -> None:
    """Absorb broker-side fills and keep the resting stops armed.

    This tick is the only thing that runs while the market is open and a trading
    cycle is not, so it is where a stop that fired overnight or over a weekend
    finally gets noticed — and where a sub-one-share position's session-only DAY
    stop gets re-armed (Alpaca will not carry a fractional stop GTC, so that leg
    genuinely expires every session).

    Strictly risk-reducing: it records exits that already happened and arms
    protective sell orders. Nothing here can increase exposure. Fail-soft — a
    broker hiccup must never stop the stop check that follows."""
    if broker.backend_name() != "alpaca_paper":
        return  # simulation has no broker-side lifecycle
    # See fills the OTHER node already applied and pushed before reconciling,
    # so the per-checkout dedupe layers actually cover them (F3 mitigation).
    _sync_repo_ff(out)
    try:
        ingested = run_reconcile()
        if ingested:
            out["ingested_fills"] = [
                f"{f.get('ticker')} {f.get('quantity')} @ {f.get('fill_price')}"
                for f in ingested]
    except Exception as e:
        out["ingest_error"] = str(e)
    try:
        from execution import alpaca_broker
        for pos in (alpaca_broker.get_portfolio().get("positions") or []):
            alpaca_broker.ensure_protective_stop(
                str(pos.get("ticker", "")).upper(), reason="stop_watch_tick")
        naked = [str(p.get("ticker")) for p in alpaca_broker.unprotected_positions()]
        if naked:
            out["unprotected"] = naked
            print(f"  UNPROTECTED POSITIONS (no resting stop): {', '.join(naked)}")
    except Exception as e:
        out["stop_arm_error"] = str(e)


def run(dry_run: bool = False) -> dict:
    """One stop-enforcement pass. Never raises; returns a structured result."""
    started = datetime.now(timezone.utc).isoformat()
    out = {"status": "ok", "started_at": started, "checked": 0,
           "forced": [], "fills": [], "note": None}
    cfg = _cfg()

    if cfg.get("mode", {}).get("trading_mode") == "dry_run":
        out.update(status="skipped", note="trading_mode=dry_run")
        return out

    # Runs BEFORE the book is read (and even on a flat book): a fired stop is
    # exactly what makes the book flat, and that fill still needs recording.
    # UNDER THE RUN LOCK (fixed 2026-08-05): maintenance WRITES the ledger —
    # run_reconcile ingests broker fills into state/portfolio.json and
    # ensure_protective_stop stamps protective_stop records — while a trading
    # cycle's own read-modify-write (apply_corporate_actions loads the state,
    # mutates, saves ~140 lines later) could load BEFORE this wrote and save
    # AFTER, silently reverting a tick-ingested fill or a fresh stop record.
    # The docstring claimed the lock was taken; the code did not take it here.
    # If a cycle holds the lock, defer: the next tick is five minutes away.
    if not dry_run:
        maint_id = (f"stopwatch-maint-"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
                    f"{uuid.uuid4().hex[:4]}")
        if preflight.acquire_run_lock(maint_id):
            try:
                _broker_maintenance(out)
            finally:
                preflight.release_run_lock(maint_id)
        else:
            out["maintenance"] = "deferred_run_lock_held"

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

    atr_map = _load_atr_map()

    forced = exit_guard.check_forced_exits(portfolio, prices, atr_map)
    out["forced"] = forced
    if not forced:
        return out

    if dry_run:
        out.update(status="dry_run", note=f"{len(forced)} exit(s) would fire")
        return out

    # Second, separate lock acquisition: the price read and breach check above
    # are lock-free so a tick rarely blocks a scheduled trading cycle from
    # starting — only the two ledger-WRITING phases (maintenance above,
    # execution here) ever hold the lock, each briefly.
    run_id = f"stopwatch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"
    if not preflight.acquire_run_lock(run_id):
        out.update(status="deferred",
                   note="a trading cycle holds the run lock; it enforces stops itself")
        return out
    try:
        out["fills"] = exit_guard.execute_forced_exits(forced, prices, run_id, atr_map)
        out["run_id"] = run_id
    finally:
        # Ownership-checked release (pass the run id): an unconditional unlink
        # here could delete a lock another process legitimately reclaimed.
        preflight.release_run_lock(run_id)
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
