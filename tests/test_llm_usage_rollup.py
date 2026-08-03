"""The quota rollup must be readable AND honest about what it cannot measure.

Closing the 82% telemetry gap (see tests/test_llm_call_coverage.py) only helps if
somebody can read the result. Nobody was going to `jq` 30 JSONL files a day, which
is why the 07-28 run that made five risk-desk calls looked identical to a quiet
day in a directory listing.

Two properties are load-bearing here and both are easy to get subtly wrong:
  * `stage: "started"` breadcrumbs must NOT be counted as calls. Double-counting
    every invocation would make the rollup wrong in the flattering direction.
  * An inferred cloud-routine record must never contribute tokens or quota it does
    not have. It counts as one call and nothing else.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import llm_usage


TODAY = datetime.now(timezone.utc).date().isoformat()
YESTERDAY = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


@pytest.fixture
def journal_dir(tmp_path, monkeypatch):
    """Point the rollup at a synthetic journal. It must never read the real one."""
    monkeypatch.setattr(llm_usage, "JOURNAL", tmp_path / "journal")
    return tmp_path / "journal"


def _write(journal_dir, subdir: str, day: str, records: list[dict]) -> None:
    path = journal_dir / subdir / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _call(call: str, uid: str, **kw) -> dict:
    return {"run_id": kw.pop("run_id", "R1"), "call": call, "call_uid": uid,
            "ok": True, "attempt": 1, "elapsed_s": 100.0,
            "total_cost_usd": 1.5, "input_tokens": 1000, "output_tokens": 500,
            **kw}


def test_started_breadcrumbs_are_not_counted_as_calls(journal_dir):
    """Every real call writes TWO records. Counting both doubles the whole rollup."""
    _write(journal_dir, "brain_calls", TODAY, [
        {"call": "brain:full", "call_uid": "u1", "stage": "started"},
        _call("brain:full", "u1", total_cost_usd=5.0),
    ])
    data = llm_usage.rollup(days=2)
    assert data["totals"]["calls"] == 1
    assert data["totals"]["notional_quota_usd"] == 5.0
    assert data["totals"]["died_mid_call"] == 0


def test_unpaired_start_is_reported_as_a_dead_call(journal_dir):
    """Quota was spent and no answer came back. That used to leave NOTHING —
    indistinguishable from a call that never fired (the run_starts lesson, one
    level down)."""
    _write(journal_dir, "brain_calls", TODAY, [
        {"call": "brain:full", "call_uid": "u1", "stage": "started"},
        {"call": "risk_desk", "call_uid": "u2", "stage": "started"},
        _call("risk_desk", "u2"),
    ])
    data = llm_usage.rollup(days=2)
    assert data["totals"]["calls"] == 1, "the dead call has no outcome to aggregate"
    assert data["totals"]["died_mid_call"] == 1
    assert "DIED MID-CALL" in llm_usage.render(data)


def test_inferred_cloud_record_counts_as_a_call_and_nothing_more(journal_dir):
    """It carries no tokens and no cost because the routine's usage is not
    observable from the subprocess. Inventing either would corrupt the rollup."""
    _write(journal_dir, "brain_calls", TODAY, [
        {"run_id": "R1", "call": "brain:full", "ok": True, "attempt": 1,
         "transport": "cloud_routine", "instrumented": False},
        _call("risk_desk", "u2", total_cost_usd=1.6, input_tokens=900),
    ])
    t = llm_usage.rollup(days=2)["totals"]
    assert t["calls"] == 2
    assert t["inferred"] == 1 and t["instrumented"] == 1
    assert t["notional_quota_usd"] == 1.6, "the inferred call must add no quota"
    assert t["input_tokens"] == 900, "the inferred call must add no tokens"


def test_retries_are_surfaced_as_repeated_spend(journal_dir):
    """attempt 2 is quota spent twice on the same question — waste worth seeing,
    not a detail to bury inside a call count."""
    _write(journal_dir, "brain_calls", TODAY, [
        _call("brain:full", "u1:1", ok=False, attempt=1, total_cost_usd=0.0,
              error="timeout after 1800s"),
        _call("brain:full", "u1:2", attempt=2, total_cost_usd=5.0),
    ])
    t = llm_usage.rollup(days=2)["totals"]
    assert t["calls"] == 2 and t["ok"] == 1 and t["failed"] == 1
    assert t["retries"] == 1


def test_per_call_type_ranks_the_biggest_consumer_first(journal_dir):
    """daily_study is the largest recurring consumer (11 of the 29 instrumented
    calls to 07-31) and it fires on a schedule whether or not the book trades.
    That is the single fact this table exists to make obvious."""
    _write(journal_dir, "brain_calls", TODAY, [
        _call("daily_study", "u1", total_cost_usd=1.6),
        _call("daily_study", "u2", total_cost_usd=1.7),
        _call("risk_desk", "u3", total_cost_usd=1.5),
        _call("brain:full", "u4", total_cost_usd=1.0),
    ])
    by_call = llm_usage.rollup(days=2)["by_call"]
    assert list(by_call) == ["daily_study", "risk_desk", "brain:full"]
    assert by_call["daily_study"]["calls"] == 2
    assert round(by_call["daily_study"]["notional_quota_usd"], 2) == 3.3


def test_coverage_counts_runs_missing_a_brain_call(journal_dir):
    """The question the 2026-08-03 audit could not answer. A non-zero value here
    means a NEW uninstrumented path exists, not that the brain sat idle."""
    _write(journal_dir, "runs", TODAY, [
        {"run_id": "A", "run_depth": "full"},
        {"run_id": "B", "run_depth": "holdings_watchlist"},
        {"run_id": "C", "halted": "KILL_SWITCH_ACTIVE"},
    ])
    _write(journal_dir, "brain_calls", TODAY, [
        _call("brain:full", "u1", run_id="A"),
        _call("risk_desk", "u2", run_id="B"),  # a desk call is NOT a brain call
    ])
    c = llm_usage.rollup(days=2)["coverage"]
    assert c["runs"] == 2, "a halted run invoked no model and is not a gap"
    assert c["runs_with_brain_call"] == 1
    assert c["uninstrumented_run_ids"] == ["B"]
    assert c["pct"] == 50.0


def test_pre_telemetry_runs_are_excluded_not_counted_as_gaps(journal_dir,
                                                             monkeypatch):
    """run_claude recorded nothing before 2026-07-19 and it cannot be backfilled.
    Folding those 59 runs into the ratio would pin the number at a failure that is
    already fixed."""
    monkeypatch.setattr(llm_usage, "TELEMETRY_EPOCH", TODAY)
    _write(journal_dir, "runs", YESTERDAY, [{"run_id": "OLD", "run_depth": "full"}])
    _write(journal_dir, "runs", TODAY, [{"run_id": "NEW", "run_depth": "full"}])
    _write(journal_dir, "brain_calls", TODAY, [_call("brain:full", "u1",
                                                     run_id="NEW")])
    c = llm_usage.rollup(days=2)["coverage"]
    assert c["runs"] == 1 and c["pct"] == 100.0
    assert c["pre_telemetry_runs_excluded"] == 1


def test_torn_line_does_not_break_the_report(journal_dir):
    """A crash mid-append must cost a record, never the whole rollup — the same
    guard execute() already applies to the trade journal."""
    path = journal_dir / "brain_calls" / f"{TODAY}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_call("risk_desk", "u1")) + "\n{\"call\": \"brain")
    assert llm_usage.rollup(days=2)["totals"]["calls"] == 1


def test_empty_journal_renders_without_dividing_by_zero(journal_dir):
    out = llm_usage.render(llm_usage.rollup(days=3))
    assert "LLM QUOTA ROLLUP" in out
    assert "NOT money billed" in out, (
        "the subscription framing must never be droppable from the output")


def test_output_never_calls_it_money_billed(journal_dir):
    """Framing is a correctness property here: there is no ANTHROPIC_API_KEY in
    this system, so every figure is API-EQUIVALENT quota, not a charge."""
    _write(journal_dir, "brain_calls", TODAY, [_call("brain:full", "u1")])
    data = llm_usage.rollup(days=2)
    assert data["cost_basis"] == "notional_api_equivalent_not_billed"
    assert "not money billed" in data["cost_note"].lower()
    assert "notional" in llm_usage.render(data).lower()
