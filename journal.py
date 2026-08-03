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
    _reconcile_brain_call(summary, run_id)


def log_improvement(note: str, run_id: str) -> None:
    _write("improvements", {"run_id": run_id, "note": note})


def log_brain_call(record: dict, run_id: str) -> None:
    """One LLM invocation: model, outcome, latency, tokens, notional quota cost.

    Added 2026-07-19. Before this, nothing anywhere recorded what a brain call
    cost or how long it took — the system ran 15-30 Opus sessions a day with no
    spend signal, and a run that hung for 30 minutes was indistinguishable from
    one that answered instantly. The 12-runs/day cap was the only cost control
    and it cannot see the difference between a 4k-token run and a 400k one.

    `node` added 2026-08-03 for the same reason log_run_summary carries it: the
    fleet is a cloud `vm` plus a Mac, and until the audit below nobody could tell
    which of them was consuming quota. Note `total_cost_usd` in these records is
    the CLI's NOTIONAL API-equivalent figure — the brain authenticates with a
    subscription (OAuth, no ANTHROPIC_API_KEY exists anywhere in this system), so
    it measures quota consumption and the relative weight of one call type against
    another. It is not money billed.
    """
    _write("brain_calls", {"run_id": run_id, "node": node_id(), **record})


def log_brain_call_start(record: dict, run_id: str) -> None:
    """A 'this model call began' breadcrumb, written BEFORE the CLI is launched.

    Same argument as log_run_start, one level down. log_brain_call only ever fires
    once subprocess.run RETURNS, so a call that is killed mid-flight — the
    documented context-death of the heavy evening review, a sandbox teardown, a Mac
    that slept — leaves no record whatsoever and is byte-for-byte indistinguishable
    from a call that was never made. That is the same "silence reads as healthy"
    failure the run_starts breadcrumb was added for on 2026-07-20.

    Kept on a SEPARATE function rather than folded into log_brain_call so a started
    record can never be mistaken for an outcome by anything counting calls, and so
    the two write paths fail independently. tools/llm_usage pairs them on `call_uid`
    and reports an unpaired start as `died_mid_call`.
    """
    _write("brain_calls", {"run_id": run_id, "node": node_id(),
                           "stage": "started", **record})


def brain_calls_for_run(run_id: str) -> list[dict]:
    """Every brain-call record already journaled for `run_id`.

    Reads TODAY and YESTERDAY's partitions: _write partitions by wall-clock UTC
    date, so a run that starts at 23:5x writes its calls under one date and its
    summary under the next. A single-file read would miss them and the
    reconciliation below would fabricate a duplicate for a run that was in fact
    fully instrumented.
    """
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for day in (today, today - timedelta(days=1)):
        path = JOURNAL / "brain_calls" / f"{day.isoformat()}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line from a crash mid-append is not a reason to fail
            if rec.get("run_id") == run_id:
                out.append(rec)
    return out


def _reconcile_brain_call(summary: dict, run_id: str) -> None:
    """Record the CLOUD ROUTINE's own reasoning, which no other path can see.

    THE 82% GAP THIS CLOSES (measured 2026-08-03). journal/runs held 165 run
    records against 29 brain_calls records, and the correlation was perfect: every
    single `brain:*` telemetry record belonged to a run on `MacBook-Pro-2.local`
    (4 on 07-21, 1 on 07-31, 1 pre-dating the `node` field). Not one of the 82
    cloud `vm` runs ever logged the brain call it was built around.

    The reason is architectural, not a bug in the logger. In cloud mode the routine
    IS the brain: orchestrator runs `--gather-only`, writes the bundle and EXITS;
    the routine reasons in its own session; orchestrator is re-entered with
    `--act-on <file>` and simply does `Path(args.act_on).read_text()`. There is no
    subprocess, so run_claude — and therefore every line of telemetry hanging off
    it — is never reached. This is exactly why the risk desk and the daily study
    ARE logged on `vm` while the brain call in the same run is not: those two run
    as Python subprocesses inside the act-on step and go through run_claude.

    Reconciling here rather than in the orchestrator is deliberate: log_run_summary
    is the ONE call that fires on every completed run, on every node, on every
    transport, and it already holds both the run_id and the depth. Any future path
    that invokes a model outside run_claude is covered the day it is written.

    HONESTY CONSTRAINTS, because a fabricated audit record is worse than a missing
    one (the 2026-07-16 and 07-19 learning-store incidents):
      * `instrumented: false` and `transport: cloud_routine` are stamped on the
        record so nothing downstream reads it as a measured call.
      * NO token counts and NO cost. The routine's own usage is not exposed to the
        subprocess, and inventing a number here would corrupt the very rollup this
        exists to feed. It contributes to the CALL COUNT only.
      * Halted runs are skipped: preflight returns before the brain is woken, so a
        halt summary is a run where no model was invoked at all.
      * Skipped entirely when a `brain:*` record already exists for the run — a
        local run that went through run_claude is already measured properly.
    """
    try:
        if summary.get("halted"):
            return
        if any(str(rec.get("call", "")).startswith("brain")
               for rec in brain_calls_for_run(run_id)):
            return
        log_brain_call({
            "call": f"brain:{summary.get('run_depth') or 'unknown'}",
            "transport": "cloud_routine",
            "instrumented": False,
            "ok": True,
            "attempt": 1,
            "note": "reasoned by the scheduled routine's own session, not via the "
                    "claude CLI subprocess — the call happened, its token and quota "
                    "usage is not observable from here",
        }, run_id)
    except Exception as e:  # pragma: no cover - telemetry is never load-bearing
        print(f"  (brain-call reconciliation skipped: {e})")
