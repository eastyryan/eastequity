"""East Equity Agent — append-only JSONL journaling.

Everything auditable lands here: proposals, validations, rejections, fills,
run summaries, improvement notes. Files are date-partitioned JSONL under journal/.
The dashboard generator reads these files; nothing else writes to them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOURNAL = ROOT / "journal"


def _write(subdir: str, record: dict) -> Path:
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    day = record["ts"][:10]
    path = JOURNAL / subdir / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return path


def log_proposal(proposal: dict, run_id: str) -> None:
    _write("proposals", {"run_id": run_id, "proposal": proposal})


def log_rejection(proposal: dict, reasons: list[str], run_id: str) -> None:
    _write("rejected", {"run_id": run_id, "proposal": proposal, "reasons": reasons})


def log_trade(order: dict, fill: dict, run_id: str) -> None:
    _write("trades", {"run_id": run_id, "order": order, "fill": fill})


def log_intent(order: dict, status: str, run_id: str) -> None:
    """An order handed to the async executor (cloud->Actions) or resting at the
    broker — committed but not yet filled. The executor logs the real trade."""
    _write("intents", {"run_id": run_id, "status": status, "order": order})


def log_run_summary(summary: dict, run_id: str) -> None:
    _write("runs", {"run_id": run_id, **summary})


def log_improvement(note: str, run_id: str) -> None:
    _write("improvements", {"run_id": run_id, "note": note})


def log_brain_call(record: dict, run_id: str) -> None:
    """One LLM invocation: model, outcome, latency, tokens, cost.

    Added 2026-07-19. Before this, nothing anywhere recorded what a brain call
    cost or how long it took — the system ran 15-30 Opus sessions a day with no
    spend signal, and a run that hung for 30 minutes was indistinguishable from
    one that answered instantly. The 12-runs/day cap was the only cost control
    and it cannot see the difference between a 4k-token run and a 400k one.
    """
    _write("brain_calls", {"run_id": run_id, **record})
