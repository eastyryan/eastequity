"""The 2pm-slot-shows-pending bug (2026-07-21).

WHAT WENT WRONG. At the end of a run the orchestrator called refresh_dashboard()
BEFORE journal.log_run_summary(). refresh_dashboard embeds build_health() ->
slot_report(), which grades each scheduled slot by reading the run journal. Because the
current run had not logged its summary yet, slot_report found no completed run in the
current slot's window and — with the clock still inside that slot's grace window —
published it as "pending". Every run therefore shipped a dashboard that showed its OWN
slot as not-yet-run. It was most visible for the 2pm slot: the dashboard read "14:00
pending" from 2:15pm until the 4pm run republished, so the 2pm cycle looked like it
never fired even though it had.

THE FIX. Log the summary before building the dashboard, so the run counts itself.

Two guards below: a behavioural one on the mechanism (a run inside its window reads
"hit" only once its summary is journaled) and a source-order one so the two calls in the
publish tail cannot silently be swapped back.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import runlib.analytics as A  # noqa: E402


def _write_runs(tmp_path, hours):
    """Write completed-run journal records at the given ET hours (ET is UTC-4 in July)."""
    runs = tmp_path / "journal" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, h in enumerate(hours):
        utc_h = h + 4
        lines.append(json.dumps({
            "ts": f"2026-07-21T{int(utc_h):02d}:{int(round((utc_h % 1) * 60)):02d}:00+00:00",
            "run_id": f"20260721-{i:04d}", "node": "vm", "manual": False}))
    (runs / "2026-07-21.jsonl").write_text(("\n".join(lines) + "\n") if lines else "")


def _slot_status(monkeypatch, tmp_path, hours, now_h, label):
    _write_runs(tmp_path, hours)
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "et_date", lambda: "2026-07-21")
    monkeypatch.setattr(A, "to_et_date", lambda ts: "2026-07-21")
    rep = A.slot_report(now_h=now_h, weekday=True)
    return {s["label"]: s["status"] for s in rep["slots"]}[label]


def test_current_slot_reads_pending_until_its_summary_is_logged(monkeypatch, tmp_path):
    """The bug state: 2pm run publishes at 14:15, summary NOT yet in the journal.

    Only the four earlier slots are logged. Inside the 14:00 grace window this is the
    exact snapshot the buggy ordering shipped: the current slot as 'pending'.
    """
    status = _slot_status(monkeypatch, tmp_path,
                          [6.0, 9.0, 10.0, 12.0], now_h=14.25, label="14:00")
    assert status == "pending"


def test_current_slot_reads_hit_once_its_summary_is_logged(monkeypatch, tmp_path):
    """The fixed state: log the summary first, then build health. With the 14:00 run in
    the journal, the dashboard this run publishes shows its own slot as 'hit'."""
    status = _slot_status(monkeypatch, tmp_path,
                          [6.0, 9.0, 10.0, 12.0, 14.25], now_h=14.25, label="14:00")
    assert status == "hit"


def test_orchestrator_logs_summary_before_refreshing_dashboard():
    """Source-order guard. In the publish tail, journal.log_run_summary( must precede
    refresh_dashboard( — otherwise build_health() cannot see the current run and the
    slot-shows-pending regression returns."""
    src = (ROOT / "orchestrator.py").read_text()
    # Anchor on the final publish section so the halted-run log_run_summary calls
    # earlier in the file don't confuse the ordering.
    marker = src.index("[5/5] Journaling")
    tail = src[marker:]
    summary_at = re.search(r"journal\.log_run_summary\(", tail)
    dashboard_at = re.search(r"refresh_dashboard\(", tail)
    assert summary_at and dashboard_at, "publish calls not found — test anchor is stale"
    assert summary_at.start() < dashboard_at.start(), (
        "log_run_summary must run before refresh_dashboard so the run's own slot is "
        "counted in the health block it publishes (2pm-slot-shows-pending bug)")
