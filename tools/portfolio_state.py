"""Portfolio State Reader — broker-agnostic view of current positions.

Reads from whichever backend autonomy_config.json selects. For each open
position it attaches the original proposal's thesis/stop/target/horizon so the
brain reviews every holding against its own stated plan. Numeric plan fields
persisted on the position at fill time (position["plan"]) take precedence;
the proposals journal supplies the narrative (thesis/catalysts/risk_map) and
acts as a fallback for positions that predate plan persistence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Numeric fields persisted onto positions at fill time (see simulated_broker.readback).
PLAN_NUMERIC_FIELDS = ("stop_loss", "target_price", "holding_horizon_days",
                       "entry_price_max", "confidence")


def _journal_plans() -> dict:
    """Latest BUY proposal per ticker from the proposals journal (narrative + numbers)."""
    plans: dict = {}
    for f in sorted((ROOT / "journal" / "proposals").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("proposal", {})
            if p.get("action") == "BUY":
                plans[p.get("ticker", "").upper()] = {
                    "thesis": p.get("thesis"), "stop_loss": p.get("stop_loss"),
                    "target_price": p.get("target_price"),
                    "entry_price_max": p.get("entry_price_max"),
                    "holding_horizon_days": p.get("holding_horizon_days"),
                    "confidence": p.get("confidence"),
                    "risk_reward_ratio": p.get("risk_reward_ratio"),
                    "catalysts": p.get("catalysts"),
                    "risk_map": p.get("risk_map"),
                    "macro_context": p.get("macro_context"),
                    "proposed_at": rec.get("ts"),
                }
    return plans


def _merge_plan(persisted: dict | None, journal_plan: dict | None) -> dict | None:
    """Persisted numeric fields win; the journal supplies narrative + fallback.
    Returns None when neither source has anything (exit_guard then skips the
    position rather than force-exiting blind)."""
    merged = dict(journal_plan or {})
    for k, v in (persisted or {}).items():
        if v is not None:
            merged[k] = v
    return merged or None


def get_portfolio_state() -> dict:
    cfg = json.loads((ROOT / "autonomy_config.json").read_text())
    broker = cfg["mode"]["broker"]
    if broker == "simulation":
        from execution import simulated_broker
        state = simulated_broker.get_portfolio()
    else:
        raise NotImplementedError(f"broker backend '{broker}' not wired yet")

    plans = _journal_plans()
    for pos in state.get("positions", []):
        pos["original_plan"] = _merge_plan(pos.get("plan"), plans.get(pos["ticker"]))
        opened = pos.get("opened_at")
        if opened:
            days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(opened)).days
            pos["days_held"] = days
    return {"status": "ok", "broker": broker, "mode": cfg["mode"]["trading_mode"], **state}


def backfill_position_plans() -> int:
    """Idempotent one-time migration: copy journal-derived numeric plans onto
    positions opened before plan persistence existed, so stop/horizon enforcement
    no longer depends on journal retention. Returns positions backfilled."""
    cfg = json.loads((ROOT / "autonomy_config.json").read_text())
    if cfg["mode"]["broker"] != "simulation":
        return 0
    from execution import simulated_broker
    state = simulated_broker._load()
    plans = _journal_plans()
    changed = 0
    for pos in state.get("positions", []):
        if pos.get("plan"):
            continue
        jp = plans.get(str(pos.get("ticker", "")).upper())
        if not jp:
            continue
        numeric = {k: jp.get(k) for k in PLAN_NUMERIC_FIELDS if jp.get(k) is not None}
        if not numeric:
            continue
        pos["plan"] = numeric
        changed += 1
    if changed:
        simulated_broker._save(state)
    return changed


if __name__ == "__main__":
    print(json.dumps(get_portfolio_state(), indent=2, default=str))
