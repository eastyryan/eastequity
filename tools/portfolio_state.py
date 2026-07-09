"""Portfolio State Reader — broker-agnostic view of current positions.

Reads from whichever backend autonomy_config.json selects. For each open
position it attaches the original proposal's thesis/stop/target/horizon (from
the journal) so the brain reviews every holding against its own stated plan.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get_portfolio_state() -> dict:
    cfg = json.loads((ROOT / "autonomy_config.json").read_text())
    broker = cfg["mode"]["broker"]
    if broker == "simulation":
        from execution import simulated_broker
        state = simulated_broker.get_portfolio()
    else:
        raise NotImplementedError(f"broker backend '{broker}' not wired yet")

    # Attach original trade plans from the proposals journal.
    plans = {}
    for f in sorted((ROOT / "journal" / "proposals").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            p = rec.get("proposal", {})
            if p.get("action") == "BUY":
                plans[p.get("ticker", "").upper()] = {
                    "thesis": p.get("thesis"), "stop_loss": p.get("stop_loss"),
                    "target_price": p.get("target_price"),
                    "holding_horizon_days": p.get("holding_horizon_days"),
                    "proposed_at": rec.get("ts"),
                }
    for pos in state.get("positions", []):
        pos["original_plan"] = plans.get(pos["ticker"])
        opened = pos.get("opened_at")
        if opened:
            days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(opened)).days
            pos["days_held"] = days
    return {"status": "ok", "broker": broker, "mode": cfg["mode"]["trading_mode"], **state}


if __name__ == "__main__":
    print(json.dumps(get_portfolio_state(), indent=2, default=str))
