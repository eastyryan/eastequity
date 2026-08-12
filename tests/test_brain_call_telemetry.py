"""Every brain call must leave a record, and telemetry must never break a run.

THE GAP THIS CLOSES (audited 2026-07-19)
----------------------------------------
Nothing anywhere recorded what a brain call cost, how long it took, which model
actually ran, or whether it succeeded on the first attempt. The system made 15-30
Opus calls a day with no spend signal, and the only cost control — a 12-runs/day
cap — cannot tell a 4k-token run from a 400k one.

Worse, `subprocess.TimeoutExpired` is not a `CalledProcessError` and was not caught
by the returncode branch, so a hung CLI propagated out of main(): no run summary,
no journal line, no dashboard update. A 30-minute hang burned the slot and left no
trace of itself but a traceback in cron.log.

TWO RULES, IN PRIORITY ORDER
    1. Telemetry NEVER breaks a run. An unparseable envelope costs metrics, not
       the trade. parse_proposals is load-bearing; this is not.
    2. Subject to (1), every invocation is journaled — success, failure, timeout.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from runlib import brain_io


# ---------------------------------------------------------------------------
# grok_cmd (exported as claude_cmd) pins model + tools.
# ---------------------------------------------------------------------------

def test_output_format_is_opt_in():
    """No format flag unless asked; prompt is last so flags cannot be swallowed."""
    cmd = brain_io.claude_cmd("p", None, None)
    assert cmd[0] == "grok"
    assert cmd[-2:] == ["-p", "p"]
    assert "--output-format" not in cmd


def test_output_format_appended_when_asked():
    cmd = brain_io.claude_cmd("p", "grok-4.6", "Read Grep", output_format="json")
    assert cmd[cmd.index("--output-format") + 1] == "json"
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert not {"Write", "Edit", "Bash", "run_terminal_cmd", "search_replace"} & set(tools)


# ---------------------------------------------------------------------------
# Envelope parsing — fail-safe by contract.
# ---------------------------------------------------------------------------

def test_envelope_yields_text_and_usage():
    env = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 41234, "num_turns": 7, "total_cost_usd": 0.42,
        "session_id": "abc-123",
        "result": "```json\n{\"proposals\": []}\n```\nImprovement note: x",
        "usage": {"input_tokens": 120000, "output_tokens": 3100,
                  "cache_read_input_tokens": 90000},
    })
    text, meta = brain_io.unwrap_cli_json(env)
    assert text.startswith("```json"), "the response text must be handed on untouched"
    assert meta["input_tokens"] == 120000
    assert meta["output_tokens"] == 3100
    assert meta["cache_read_input_tokens"] == 90000
    assert meta["total_cost_usd"] == 0.42
    assert meta["num_turns"] == 7
    assert meta["session_id"] == "abc-123"


@pytest.mark.parametrize("stdout", [
    "plain text response with no envelope",
    "```json\n{\"proposals\": []}\n```",          # a fenced block, not an envelope
    "{ this is not valid json at all",
    '{"no_result_key": true}',                    # JSON, but not our shape
    '{"result": {"not": "a string"}}',            # result present, wrong type
    "",
])
def test_unrecognised_output_passes_through_unchanged(stdout):
    """A CLI upgrade that changes this shape must cost metrics, never the run."""
    text, meta = brain_io.unwrap_cli_json(stdout)
    assert text == stdout, "response text must survive an unparseable envelope"
    assert meta == {}


# ---------------------------------------------------------------------------
# run_claude journals every outcome.
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, record, run_id):
        self.calls.append({**record, "run_id": run_id})


@pytest.fixture
def recorded(monkeypatch):
    rec = _Recorder()
    import journal
    monkeypatch.setattr(journal, "log_brain_call", rec)
    monkeypatch.setattr(brain_io, "llm_settings",
                        lambda: {"brain_model": "grok-4.6",
                                 "allowed_tools": "read_file,grep"})
    monkeypatch.setattr(brain_io.time if hasattr(brain_io, "time") else __import__("time"),
                        "sleep", lambda *_: None, raising=False)
    return rec


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_successful_call_is_journaled_with_usage(monkeypatch, recorded):
    env = json.dumps({"result": "the answer", "num_turns": 3,
                      "total_cost_usd": 0.11,
                      "usage": {"input_tokens": 900, "output_tokens": 50}})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed(0, env))
    out = brain_io.run_claude("prompt", call="brain:full", run_id="RID")

    assert out == "the answer", "the parsed text, not the envelope, reaches the caller"
    assert len(recorded.calls) == 1
    rec = recorded.calls[0]
    assert rec["ok"] is True and rec["run_id"] == "RID"
    assert rec["call"] == "brain:full" and rec["model"] == "grok-4.6"
    assert rec["input_tokens"] == 900 and rec["total_cost_usd"] == 0.11
    assert rec["attempt"] == 1 and rec["elapsed_s"] >= 0


def test_timeout_is_handled_retried_and_journaled(monkeypatch, recorded):
    """THE BUG: TimeoutExpired escaped every handler and crashed the run."""
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="grok", timeout=1800)
    monkeypatch.setattr(subprocess, "run", _boom)

    with pytest.raises(RuntimeError) as e:
        brain_io.run_claude("prompt", call="brain:full", run_id="RID")

    assert "timeout" in str(e.value), (
        "a hung CLI must surface as a handled RuntimeError, not TimeoutExpired")
    assert len(recorded.calls) == 2, "both attempts must be journaled"
    assert all(c["ok"] is False for c in recorded.calls)
    assert all("timeout" in c["error"] for c in recorded.calls)


def test_timeout_then_success_recovers(monkeypatch, recorded):
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="grok", timeout=1800)
        return _completed(0, json.dumps({"result": "recovered"}))

    monkeypatch.setattr(subprocess, "run", _flaky)
    assert brain_io.run_claude("p", run_id="RID") == "recovered"
    assert [c["ok"] for c in recorded.calls] == [False, True]
    assert recorded.calls[1]["attempt"] == 2


def test_nonzero_exit_is_journaled_then_raises(monkeypatch, recorded):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _completed(1, "", "boom"))
    with pytest.raises(RuntimeError):
        brain_io.run_claude("p", run_id="RID")
    assert len(recorded.calls) == 2
    assert all(not c["ok"] and "exit=1" in c["error"] for c in recorded.calls)


def test_journal_failure_never_breaks_the_run(monkeypatch):
    """Rule 1. If telemetry can take down a trading run it is worse than nothing."""
    import journal

    def _explode(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(journal, "log_brain_call", _explode)
    monkeypatch.setattr(brain_io, "llm_settings", lambda: {})
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _completed(0, json.dumps({"result": "ok"})))
    assert brain_io.run_claude("p", run_id="RID") == "ok"


def test_raw_text_response_still_returned_when_envelope_absent(monkeypatch, recorded):
    """Belt and braces: a CLI ignoring --output-format must not break parsing."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _completed(0, "```json\n{\"proposals\": []}\n```"))
    out = brain_io.run_claude("p", run_id="RID")
    assert out.startswith("```json")
    assert recorded.calls[0]["ok"] is True
    assert "input_tokens" not in recorded.calls[0], "no usage claimed when none was given"
