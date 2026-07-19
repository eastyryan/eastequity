"""Runtime proof that every load-bearing guard is actually WIRED and FED.

WHY THIS EXISTS. Over three days of hardening, every single defect found had one
shape — the code existed, its unit tests passed, and production either never invoked
it or invoked it with data that was always empty:

  * alpaca_broker's live entry-price guard read order["entry_price_max"]; brain_io
    only ever nested that value inside `plan`. The guard evaluated None and no-opped
    on EVERY buy since it was written.
  * scripts/stop_watch.py read data/universe_scan.json, a file nothing in the repo
    writes, behind a bare `except: pass`. The ATR map was {} on every tick, so the
    chandelier trail never ratcheted between cycles.
  * factor_map / portfolio_competition / ownership_flow — 1,445 lines, fully tested,
    with their bundle wiring never committed. factor_map.requires_factor_response is
    the TRIGGER for the gate that blocks new BUYs on unanswered concentration, so
    that gate could never fire.
  * orchestrator called audit_brain_process without held_tickers or the require_*
    flags, so both process gates defaulted OFF in production.
  * validator.py contained zero references to stale/data_quality/freshness while
    CLAUDE.md called fundamentals freshness a "HARD RULE".

Unit tests structurally cannot catch this class. They call a function directly with
good inputs, which is precisely what production does not do. The gap is never in the
function — it is in the wiring between functions, and in whether the data arriving is
real.

So this module probes the PRODUCTION PATH with the REAL artifacts, every run. A
capability that is claimed but dead blocks new BUYs, journals loudly, and fails the
heartbeat. Exits, holds and stop enforcement are NEVER blocked — refusing to reduce
risk is always the wrong direction.

Contract for a probe:
  * Offline and cheap. This runs every cycle; it may read files, never the network.
  * It must be able to FAIL. A probe that cannot return False is decoration, and
    decoration is the thing this module exists to eliminate.
  * It asserts DATA, not existence. "The function is importable" proves nothing;
    "the map it depends on has entries" proves something.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _newest_context() -> dict:
    """The most recent bundle by MTIME, or {} when none exists.

    Genuinely newest, not relay-first. An earlier version preferred
    data/cloud_context.json unconditionally and therefore probed a bundle produced
    before the wiring it was checking — reporting factor_map dead minutes after it
    had been verified present in a live run. A staleness bug inside the staleness
    detector is the same failure this module exists to prevent.
    """
    candidates = list((ROOT / "state").glob("context_2*.json"))
    relay = ROOT / "data" / "cloud_context.json"
    if relay.exists():
        candidates.append(relay)
    for path in sorted(candidates, key=lambda f: f.stat().st_mtime, reverse=True)[:3]:
        try:
            return json.loads(path.read_text())
        except Exception:
            continue
    return {}


def _source_of(module_path: str, func_name: str) -> str:
    """Source text of one function, for probes that must inspect a CALL SITE.

    Reading source is the right tool for exactly one question: does production pass
    this argument. That is a wiring fact no behavioural test can observe, because the
    test supplies the argument itself.
    """
    try:
        import importlib
        import inspect
        mod = importlib.import_module(module_path)
        return inspect.getsource(getattr(mod, func_name))
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Probes. Each returns (alive: bool, detail: str).
# --------------------------------------------------------------------------- #
def _probe_entry_ceiling() -> tuple[bool, str]:
    src = _source_of("runlib.brain_io", "execute")
    if not src:
        return False, "cannot read brain_io.execute"
    try:
        call = src[src.index("broker.place_order({"):]
        call = call[:call.index("})")]
    except ValueError:
        return False, "place_order call site not found in execute()"
    if '"entry_price_max"' not in call:
        return False, ("brain_io does not pass entry_price_max to the broker — the "
                       "live last-trade ceiling is unreachable and every BUY is "
                       "capped only by a bundle price up to 30 min stale")
    return True, "entry ceiling reaches the broker"


def _probe_atr_map() -> tuple[bool, str]:
    try:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        from stop_watch import _load_atr_map
        atr = _load_atr_map()
    except Exception as e:
        return False, f"stop_watch ATR loader unavailable: {e}"
    if not atr:
        return False, ("ATR map is EMPTY — the chandelier trail cannot ratchet "
                       "between cycles and the simulated stop gap-through is off")
    return True, f"{len(atr)} tickers"


def _probe_book_risk_blocks() -> tuple[bool, str]:
    """factor_map must be IN the bundle, because it triggers the concentration gate."""
    ctx = _newest_context()
    if not ctx:
        return True, "no bundle yet (fresh checkout) — not asserted"
    missing = [k for k in ("factor_map", "portfolio_competition", "ownership_flow")
               if not isinstance(ctx.get(k), dict)]
    if missing:
        return False, (f"absent from the bundle: {missing} — factor_map drives "
                       f"requires_factor_response, so the gate that blocks BUYs on "
                       f"unanswered concentration cannot fire")
    return True, "factor_map / competition / ownership present"


def _probe_process_gate_flags() -> tuple[bool, str]:
    src = _source_of("orchestrator", "main")
    if not src:
        return False, "cannot read orchestrator.main"
    if "audit_brain_process(" not in src:
        return False, "orchestrator never calls audit_brain_process"
    call = src[src.index("audit_brain_process("):]
    call = call[:call.index(")\n") + 1]
    missing = [a for a in ("held_tickers", "require_seat_reviews",
                           "require_factor_response") if a not in call]
    if missing:
        return False, (f"orchestrator does not pass {missing} — both process gates "
                       f"default OFF and their teeth have nothing to bite")
    return True, "gate flags passed"


def _probe_driver_crosscheck() -> tuple[bool, str]:
    """The relabelling exploit guard, proved by attacking it."""
    try:
        from tools.demand_drivers import driver_for_ticker, driver_mismatch
    except Exception as e:
        return False, f"demand_drivers unavailable: {e}"
    canonical = driver_for_ticker("DELL")
    if canonical != "hyperscaler_server_capex":
        return True, f"DELL mapping changed ({canonical}) — not asserted"
    if driver_mismatch("DELL", "ai_supplier_other") is None:
        return False, ("an adjacent canonical relabel is NOT rejected — a same-theme "
                       "bet can escape both theme caps")
    if driver_mismatch("DELL", canonical) is not None:
        return False, "the canonical driver is being rejected — caps are inverted"
    return True, "adjacent relabel rejected, canonical accepted"


def _probe_data_quality_gate() -> tuple[bool, str]:
    src = _source_of("validator", "validate_proposals")
    if not src:
        return False, "cannot read validate_proposals"
    if "_check_data_quality" not in src:
        return False, ("the validator does not run the data-quality gate — "
                       "CLAUDE.md's fundamentals-freshness HARD RULE is enforced by "
                       "nothing")
    return True, "data-quality gate wired"


def _probe_feed_health_predicate() -> tuple[bool, str]:
    """feed_is_alive must be able to call a dead feed dead."""
    try:
        from orchestrator import feed_is_alive
    except Exception as e:
        return False, f"feed_is_alive unavailable: {e}"
    dead = {"status": "ok", "tickers": {"A": {"error": "x"}, "B": {"error": "x"}}}
    if feed_is_alive(dead):
        return False, ("feed_is_alive calls an all-failed feed healthy — the data "
                       "health check is dead code again")
    if not feed_is_alive({"status": "ok", "tickers": {"A": {"headlines": [1]}}}):
        return False, "feed_is_alive calls a healthy feed dead"
    return True, "detects a dead feed and a healthy one"


def _probe_protective_stops() -> tuple[bool, str]:
    """Every OPEN position must be protected, or say why not."""
    try:
        from execution import simulated_broker
        positions = (simulated_broker._load().get("positions") or [])
    except Exception as e:
        return False, f"cannot read the ledger: {e}"
    if not positions:
        return True, "no open positions"
    naked = []
    for pos in positions:
        ps = pos.get("protective_stop") or {}
        if str(ps.get("status") or "") not in ("resting", "session_only"):
            naked.append(f"{pos.get('ticker')}({ps.get('status') or 'none'})")
    if naked:
        return False, f"UNPROTECTED positions: {', '.join(naked)}"
    return True, f"{len(positions)} position(s) protected"


PROBES = {
    "entry_price_ceiling": _probe_entry_ceiling,
    "atr_map_for_trail": _probe_atr_map,
    "book_risk_bundle_blocks": _probe_book_risk_blocks,
    "process_gate_flags": _probe_process_gate_flags,
    "demand_driver_crosscheck": _probe_driver_crosscheck,
    "data_quality_gate": _probe_data_quality_gate,
    "feed_health_predicate": _probe_feed_health_predicate,
    "protective_stops_armed": _probe_protective_stops,
}


def audit_capabilities() -> dict:
    """Probe every load-bearing capability. Never raises.

    Returns {"ok", "dead", "results", "blocks_new_buys"}. A probe that itself throws
    counts as DEAD: an unverifiable guard and an absent one are the same thing to the
    position it was supposed to protect.
    """
    results, dead = {}, []
    for name, probe in PROBES.items():
        try:
            alive, detail = probe()
        except Exception as e:
            alive, detail = False, f"probe raised: {type(e).__name__}: {e}"
        results[name] = {"alive": bool(alive), "detail": detail}
        if not alive:
            dead.append(name)
    return {
        "ok": not dead,
        "dead": dead,
        "results": results,
        "blocks_new_buys": bool(dead),
        "note": ("Each probe asserts a guard is WIRED AND FED on the production path, "
                 "not merely that its function exists. A dead capability blocks new "
                 "BUYs; exits, holds and stop enforcement are never blocked."),
    }


if __name__ == "__main__":
    out = audit_capabilities()
    for name, r in out["results"].items():
        print(f"  [{'OK ' if r['alive'] else 'DEAD'}] {name:26} {r['detail']}")
    print(f"\nok={out['ok']} blocks_new_buys={out['blocks_new_buys']}")
    raise SystemExit(0 if out["ok"] else 1)
