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


def node_id() -> str:
    """Best-effort stable id for this execution node (cloud sandbox vs a Mac).

    Mirrors runlib.preflight._node_id, deliberately duplicated rather than imported:
    journal.py is a leaf module that everything writes through, and importing preflight
    (which imports validator) would put a cycle under every journal write.
    """
    import os
    import socket
    if os.environ.get("EE_NODE"):
        return os.environ["EE_NODE"]
    try:
        return socket.gethostname() or "unknown-node"
    except Exception:
        return "unknown-node"


def log_run_start(run_id: str, slot: str | None = None, stage: str = "start",
                  extra: dict | None = None) -> "Path":
    """A durable 'a run began' breadcrumb, written and (by the caller) pushed BEFORE
    the heavy work.

    THE PROBLEM THIS SOLVES (2026-07-20). A run's first durable record was
    log_run_summary at the very END, after gather + brain + validate + execute. So a
    session that died mid-run — the documented context-death of the heavy evening
    review, a pip/gather failure, any error before the final push — left ZERO trace:
    no log, no commit, nothing. On 2026-07-20 three slots (10am, 2pm, 5:30) vanished
    that way and were byte-for-byte indistinguishable from fires that never happened.
    You cannot read why a run died when the run recorded nothing until it succeeded.

    A start marker makes a death VISIBLE and DIAGNOSABLE: a start with no matching
    summary in its slot window is a run that fired and died, which the heartbeat can
    now report and the watchdog can re-run. `stage` lets a caller advance the marker
    (start -> gathered -> brain_done) so we also learn HOW FAR it got.
    """
    return _write("run_starts", {"run_id": run_id, "node": node_id(),
                                 "slot": slot, "stage": stage, **(extra or {})})


def log_run_summary(summary: dict, run_id: str) -> None:
    """One completed run.

    `node` added 2026-07-20. Run records carried no execution identity, so the runs
    heartbeat could only ask "did ANYONE run this slot?" — and a live cloud trader
    masked a completely dead local one for eight days. The count looked low; nothing
    said half the fleet was gone.
    """
    _write("runs", {"run_id": run_id, "node": node_id(), **summary})


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
