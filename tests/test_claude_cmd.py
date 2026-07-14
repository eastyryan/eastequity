"""Tests for the pinned claude CLI invocation (_claude_cmd). Pure Python:

    python3 tests/test_claude_cmd.py

Regression context: every claude -p call used to inherit the CLI's ambient
default model and tool grants, so the public track record's behavior (and
cost) silently depended on whatever the host machine was configured with.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator
import validator

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_full_pinning():
    print("model + allowedTools pinned:")
    cmd = orchestrator._claude_cmd("do the thing", "claude-opus-4-8",
                                   "Read Glob Grep WebSearch WebFetch")
    check("starts with claude -p <prompt>", cmd[:3] == ["claude", "-p", "do the thing"])
    check("--model present", "--model" in cmd and
          cmd[cmd.index("--model") + 1] == "claude-opus-4-8", str(cmd))
    check("--allowedTools present", "--allowedTools" in cmd and
          cmd[cmd.index("--allowedTools") + 1] == "Read Glob Grep WebSearch WebFetch",
          str(cmd))
    check("no write-capable tools granted",
          all(t not in cmd[cmd.index("--allowedTools") + 1].split()
              for t in ("Write", "Edit", "Bash")))


def test_backward_compat_omission():
    print("missing config -> flags omitted (CLI defaults, old behavior):")
    cmd = orchestrator._claude_cmd("prompt", None, None)
    check("bare invocation", cmd == ["claude", "-p", "prompt"], str(cmd))
    cmd = orchestrator._claude_cmd("prompt", "claude-opus-4-8", None)
    check("model without tools", "--model" in cmd and "--allowedTools" not in cmd)


def test_config_block_present():
    print("autonomy_config.json llm block wired:")
    llm = validator.load_config().get("llm") or {}
    check("brain_model set", bool(llm.get("brain_model")), str(llm))
    check("risk_desk_model set", bool(llm.get("risk_desk_model")))
    check("allowed_tools excludes writers",
          llm.get("allowed_tools") and
          all(t not in llm["allowed_tools"].split() for t in ("Write", "Edit", "Bash")))
    check("_llm_settings reads it", orchestrator._llm_settings().get("brain_model")
          == llm.get("brain_model"))


if __name__ == "__main__":
    for fn in (test_full_pinning, test_backward_compat_omission, test_config_block_present):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All claude-cmd checks passed.")
