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


def filled_buy_proposal_ids() -> dict:
    """{TICKER: {proposal_id, ...}} of FILLED BUYs from the broker ledger.

    Journal-plan joins must be restricted to proposals that actually filled: the
    proposals journal logs rejected/unfilled proposals too, and the old "latest
    BUY per ticker" join let a later REJECTED proposal overwrite the real plan
    (the HPE 2026-07-10 incident: the enforced stop/confidence came from a
    size-cap-rejected proposal, not the $800 fill)."""
    out: dict[str, set] = {}
    try:
        history = json.loads((ROOT / "state" / "portfolio.json").read_text()).get("history", [])
    except Exception:
        return out
    for fill in history:
        if not isinstance(fill, dict) or fill.get("status") != "filled":
            continue
        if str(fill.get("action", "")).upper() != "BUY":
            continue
        pid = fill.get("proposal_id")
        if pid:
            out.setdefault(str(fill.get("ticker", "")).upper(), set()).add(pid)
    return out


def _journal_plans() -> dict:
    """Latest FILLED BUY proposal per ticker from the proposals journal
    (narrative + numbers). Rejected/unfilled proposals are never a plan source."""
    filled = filled_buy_proposal_ids()
    plans: dict = {}
    for f in sorted((ROOT / "journal" / "proposals").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("proposal", {})
            if p.get("action") == "BUY":
                tk = p.get("ticker", "").upper()
                if rec.get("run_id") not in filled.get(tk, ()):
                    continue  # rejected or never filled - not this position's plan
                plans[tk] = {
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
    from execution import broker as broker_router
    broker = broker_router.backend_name()
    state = broker_router.get_portfolio()

    plans = _journal_plans()
    for pos in state.get("positions", []):
        pos["original_plan"] = _merge_plan(pos.get("plan"), plans.get(pos["ticker"]))
        opened = pos.get("opened_at")
        if opened:
            days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(opened)).days
            pos["days_held"] = days
    return {"status": "ok", "broker": broker, "mode": cfg["mode"]["trading_mode"], **state}


def _proposal_numeric_plan(ticker: str, proposal_id: str | None) -> dict | None:
    """Numeric plan from the exact journal proposal a fill's proposal_id points
    at - the authoritative record of what was actually approved and filled."""
    if not proposal_id:
        return None
    best = None
    for f in sorted((ROOT / "journal" / "proposals").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("proposal", {})
            if rec.get("run_id") == proposal_id and p.get("action") == "BUY" \
                    and str(p.get("ticker", "")).upper() == ticker:
                numeric = {k: p.get(k) for k in PLAN_NUMERIC_FIELDS
                           if p.get(k) is not None}
                if numeric:
                    best = numeric
    return best


def backfill_position_plans() -> int:
    """Idempotent migration + self-heal, run at the start of every cycle.

    (1) Positions with no persisted plan get one from the journal (filled
    proposals only). (2) Positions WITH a plan are reconciled against the
    proposal their own proposal_id points at: if they differ and the position
    was never scaled into (an add legitimately replaces the plan), the filled
    proposal's plan wins. This repairs plans the old unfiltered join populated
    from REJECTED proposals (HPE 2026-07-10: stop 44.75 from a rejected
    proposal instead of the filled 43.75). Returns positions changed."""
    # Plans live in the mirror ledger for BOTH backends (the Alpaca adapter
    # writes the same file/shape), so the backfill is backend-agnostic.
    from execution import simulated_broker
    state = simulated_broker._load()
    plans = _journal_plans()
    changed = 0
    for pos in state.get("positions", []):
        tk = str(pos.get("ticker", "")).upper()
        authoritative = _proposal_numeric_plan(tk, pos.get("proposal_id"))
        if pos.get("plan"):
            if authoritative and int(pos.get("adds_count", 0) or 0) == 0 \
                    and pos["plan"] != authoritative:
                pos["plan"] = authoritative
                changed += 1
            continue
        jp = authoritative or plans.get(tk)
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
