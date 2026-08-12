"""No model invocation may happen without a record — including the ones that fail.

THE GAP THIS CLOSES (measured 2026-08-03)
-----------------------------------------
journal/runs held 165 run records against 29 journal/brain_calls records: 82% of
this system's model consumption was invisible. The cause was NOT a flaky logger,
and that mattered — the correlation was perfect. Every single `brain:*` record
belonged to a run on `MacBook-Pro-2.local` (4 on 07-21, 1 on 07-31, 1 pre-dating
the `node` field). Not one of the 82 cloud `vm` runs recorded the brain call it
was built around, while the risk desk and the daily study on those SAME cloud runs
logged perfectly.

The reason is architectural. In cloud mode the scheduled routine IS the brain:
orchestrator runs `--gather-only`, writes the bundle and exits; the routine
reasons in its own session; orchestrator is re-entered with `--act-on <file>` and
does `Path(args.act_on).read_text()`. No subprocess is spawned, so run_claude —
and every line of telemetry hanging off it — is never reached. The desk and the
study are logged on the same node precisely because they DO go through run_claude.

Three holes are closed here, and each gets a test below:
  1. The cloud-routine brain call, reconciled at log_run_summary — the one call
     that fires on every completed run, on every node, on every transport.
  2. Launch failures. Only subprocess.TimeoutExpired was caught, so a missing
     `claude` binary (FileNotFoundError) escaped with zero telemetry into a
     caller's bare `except Exception: print(...)` — silent AND unrecorded.
  3. Calls killed mid-flight, which left nothing at all and were indistinguishable
     from a call that never fired. A start breadcrumb makes them visible.

RULE 1, WHICH OUTRANKS ALL OF THEM: telemetry never breaks a trading run.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

import journal
from runlib import brain_io


def _records(subdir: str) -> list[dict]:
    """Everything written into the ISOLATED journal (see conftest _isolate_journal)."""
    out = []
    for path in sorted((journal.JOURNAL / subdir).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            out.append(json.loads(line))
    return out


def _outcomes() -> list[dict]:
    return [r for r in _records("brain_calls") if r.get("stage") != "started"]


def _starts() -> list[dict]:
    return [r for r in _records("brain_calls") if r.get("stage") == "started"]


# ---------------------------------------------------------------------------
# 1. The cloud routine's own reasoning is now recorded.
# ---------------------------------------------------------------------------

def test_cloud_run_without_a_cli_call_is_reconciled():
    """THE 82% BUG. A completed run whose brain reasoned outside run_claude."""
    journal.log_run_summary(
        {"run_depth": "holdings_watchlist", "proposals": 1, "fills": 0}, "RID-CLOUD")

    calls = _outcomes()
    assert len(calls) == 1, "a cloud run must not vanish from the quota record"
    rec = calls[0]
    assert rec["call"] == "brain:holdings_watchlist", "the depth must be preserved"
    assert rec["run_id"] == "RID-CLOUD"
    assert rec["transport"] == "cloud_routine"
    assert rec["instrumented"] is False, (
        "an inferred record must never masquerade as a measured one")
    assert rec.get("node"), "the fleet must be attributable (vm vs the Mac)"


def test_reconciled_record_claims_no_usage_it_cannot_measure():
    """A fabricated number in an audit trail is worse than a missing one.

    The routine's token and quota usage is not observable from the subprocess, so
    the record contributes to the CALL COUNT and nothing else. Inventing a cost
    here would corrupt the very rollup it exists to feed."""
    journal.log_run_summary({"run_depth": "full"}, "RID-NOFAKE")
    rec = _outcomes()[0]
    for invented in ("total_cost_usd", "input_tokens", "output_tokens",
                     "elapsed_s", "model"):
        assert invented not in rec, f"reconciliation invented {invented}"


def test_instrumented_run_is_not_double_counted():
    """A local run that went through run_claude is already measured properly."""
    journal.log_brain_call({"call": "brain:full", "ok": True,
                            "total_cost_usd": 4.89}, "RID-LOCAL")
    journal.log_run_summary({"run_depth": "full"}, "RID-LOCAL")

    brain = [r for r in _outcomes() if str(r["call"]).startswith("brain")]
    assert len(brain) == 1, "the real call must not gain a phantom twin"
    assert brain[0]["total_cost_usd"] == 4.89


def test_a_non_brain_call_does_not_satisfy_the_reconciler():
    """The precise shape of the cloud gap: risk_desk logged, brain call not.

    On 2026-07-22 the vm logged three risk_desk calls and ZERO brain calls in the
    same runs. If any call record counted, that day would have looked fully
    instrumented while the most expensive invocation of each run stayed invisible.
    """
    journal.log_brain_call({"call": "risk_desk", "ok": True}, "RID-DESK")
    journal.log_run_summary({"run_depth": "holdings_watchlist"}, "RID-DESK")

    assert any(str(r["call"]).startswith("brain") for r in _outcomes()), (
        "a risk-desk record must not stand in for the brain call")


def test_halted_run_fabricates_nothing():
    """preflight returns BEFORE the brain is woken, so a halt invoked no model."""
    journal.log_run_summary(
        {"halted": "KILL_SWITCH_ACTIVE", "run_depth": "full"}, "RID-HALT")
    assert _outcomes() == [], "a halted run must not book quota it never spent"
    assert len(_records("runs")) == 1, "the run summary itself must still be written"


def test_reconciliation_failure_never_breaks_the_run(monkeypatch, capsys):
    """RULE 1. The run summary is load-bearing; the quota record is not."""
    def _explode(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(journal, "brain_calls_for_run", _explode)
    journal.log_run_summary({"run_depth": "full"}, "RID-BOOM")

    assert len(_records("runs")) == 1, "the run summary must survive telemetry failure"
    assert "reconciliation skipped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2. Launch failures are recorded, not swallowed.
# ---------------------------------------------------------------------------

@pytest.fixture
def _no_sleep(monkeypatch):
    """The retry sleeps 90s between attempts; tests must not."""
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(brain_io, "llm_settings",
                        lambda: {"brain_model": "grok-4.6"})


def test_missing_cli_binary_is_journaled_then_raised(monkeypatch, _no_sleep):
    """THE BUG: only TimeoutExpired was caught.

    ask_claude, reviews.self_review / daily_study / universe_review and
    publish._brain_trade_memo all call run_claude WITHOUT the shutil.which guard
    adversarial_review has, and each wraps it in a bare `except Exception` that
    prints and returns. On a node without the CLI the system therefore failed
    silently and recorded nothing about why."""
    def _no_binary(*a, **k):
        raise FileNotFoundError(2, "No such file or directory: 'grok'")

    monkeypatch.setattr(subprocess, "run", _no_binary)
    with pytest.raises(RuntimeError) as e:
        brain_io.run_claude("p", call="brain:full", run_id="RID-NOCLI")

    assert "FileNotFoundError" in str(e.value), "the cause must reach the caller"
    failures = [r for r in _outcomes() if r["ok"] is False]
    assert len(failures) == 2, "both attempts must be journaled, not just the last"
    assert all("launch_failed" in r["error"] for r in failures)
    assert [r["attempt"] for r in failures] == [1, 2]


def test_launch_failure_then_success_recovers_and_both_are_recorded(
        monkeypatch, _no_sleep):
    """A fork/pipe failure is usually transient; the retry must still work."""
    state = {"n": 0}

    def _flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("Resource temporarily unavailable")
        return subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"result": "recovered",
                                      "total_cost_usd": 1.5}), stderr="")

    monkeypatch.setattr(subprocess, "run", _flaky)
    assert brain_io.run_claude("p", call="risk_desk", run_id="RID-FLAKY") == "recovered"
    assert [r["ok"] for r in _outcomes()] == [False, True]
    assert _outcomes()[1]["total_cost_usd"] == 1.5


# ---------------------------------------------------------------------------
# 3. A call killed mid-flight leaves a trace.
# ---------------------------------------------------------------------------

def test_start_breadcrumb_precedes_the_call_and_pairs_with_its_outcome(
        monkeypatch, _no_sleep):
    """Same argument as log_run_start, one level down: log_brain_call only fires
    once subprocess.run RETURNS, so a SIGKILL or sandbox teardown mid-call left
    nothing and read as a call that never happened."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(
                            [], 0, stdout=json.dumps({"result": "ok"}), stderr=""))
    brain_io.run_claude("p", call="brain:full", run_id="RID-PAIR")

    starts, outcomes = _starts(), _outcomes()
    assert len(starts) == 1 and len(outcomes) == 1
    assert starts[0]["call_uid"] == outcomes[0]["call_uid"], (
        "an unpaired start must mean a dead call, never a bookkeeping mismatch")
    assert starts[0]["stage"] == "started", (
        "a breadcrumb must never be countable as an invocation outcome")


def test_repeated_call_type_in_one_run_gets_distinct_uids(monkeypatch, _no_sleep):
    """adversarial_review calls the desk TWICE when a repairable veto goes through
    the repair round (2026-08-03). Keying on run_id+call+attempt alone would
    collide and silently mis-pair the second desk call with the first's start."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(
                            [], 0, stdout=json.dumps({"result": "ok"}), stderr=""))
    brain_io.run_claude("p", call="risk_desk", run_id="RID-TWICE")
    brain_io.run_claude("p", call="risk_desk", run_id="RID-TWICE")

    uids = [r["call_uid"] for r in _outcomes()]
    assert len(set(uids)) == 2, f"uids collided: {uids}"


def test_breadcrumb_failure_never_breaks_the_run(monkeypatch, _no_sleep, capsys):
    """RULE 1 again, on the newest write path."""
    def _explode(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(journal, "log_brain_call_start", _explode)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(
                            [], 0, stdout=json.dumps({"result": "ok"}), stderr=""))
    assert brain_io.run_claude("p", run_id="RID-CRUMB") == "ok"
    assert "breadcrumb skipped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The repair round must be separable from the desk call it repeats.
# ---------------------------------------------------------------------------

def test_repair_round_is_three_separately_labelled_calls(monkeypatch, tmp_path,
                                                         _no_sleep):
    """A repairable veto costs THREE model calls, and the rollup must see three.

    The repair round added 2026-08-03 runs desk -> risk_desk_repair -> desk again.
    If the re-review reused the `risk_desk` label, triple spend on one run would
    read as one desk call that happened to be expensive, and the loop could not be
    judged on what it recovers versus what it burns.
    """
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/grok")

    ctx = tmp_path / "context.json"
    ctx.write_text(json.dumps({"universe_scan": {}}))

    seen: list[str] = []
    replies = [
        '```json\n{"reviews": [{"ticker": "BKR", "verdict": "veto", '
        '"objection": "Stifel PT misattributed", "repairable": true, '
        '"repair_instruction": "delete the Stifel claim"}]}\n```',
        '```json\n{"repairs": [{"ticker": "BKR", "withdraw": false, '
        '"thesis": "corrected thesis without the disputed citation"}]}\n```',
        '```json\n{"reviews": [{"ticker": "BKR", "verdict": "approve", '
        '"objection": "stands without the false claim", '
        '"confidence_adjustment": 0.0}]}\n```',
    ]

    def _fake(prompt, model=None, *, call="brain", run_id=""):
        seen.append(call)
        return replies[len(seen) - 1]

    monkeypatch.setattr(brain_io, "run_claude", _fake)

    proposals = [{"ticker": "BKR", "action": "BUY", "confidence": 0.7}]
    kept = brain_io.adversarial_review(proposals, str(ctx), "RID-REPAIR")

    assert seen == ["risk_desk", "risk_desk_repair", "risk_desk_rereview"], seen
    assert [p["ticker"] for p in kept] == ["BKR"], (
        "the repaired proposal must survive the re-review — behaviour unchanged")
    assert kept[0]["thesis"].startswith("corrected"), "the repair must be applied"


def test_external_recorder_is_available_and_marks_itself_uninstrumented():
    """The public seam for any transport run_claude cannot see."""
    brain_io.record_brain_call("brain:full", "RID-EXT", slot="09:00")
    rec = _outcomes()[0]
    assert rec["transport"] == "external" and rec["instrumented"] is False
    assert rec["slot"] == "09:00" and rec["run_id"] == "RID-EXT"
