"""Tests for the pinned grok CLI invocation (_claude_cmd / grok_cmd). Pure Python:

    python3 tests/test_claude_cmd.py

Regression context: every brain call used to inherit the CLI's ambient default
model and tool grants, so the public track record silently depended on whatever
the host was configured with. The binary is grok; the export name is stable.
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
    print("model + tools pinned:")
    cmd = orchestrator._claude_cmd(
        "do the thing", "grok-4.6", "read_file,grep,list_dir,web_search,web_fetch")
    check("binary is grok", cmd[0] == "grok", str(cmd))
    check("-p last with prompt", cmd[-2:] == ["-p", "do the thing"], str(cmd))
    check("--yolo present (CI, no TTY)", "--yolo" in cmd, str(cmd))
    check("--model present", "--model" in cmd and
          cmd[cmd.index("--model") + 1] == "grok-4.6", str(cmd))
    check("--tools present", "--tools" in cmd, str(cmd))
    tools = cmd[cmd.index("--tools") + 1]
    check("no write-capable tools granted",
          all(t not in tools.split(",")
              for t in ("run_terminal_cmd", "search_replace", "Write", "Edit", "Bash")),
          tools)
    check("web search granted", "web_search" in tools, tools)


def test_claude_tool_names_are_mapped():
    print("legacy Claude allowlist maps to grok tool ids:")
    cmd = orchestrator._claude_cmd(
        "p", "grok-4.6", "Read Glob Grep WebSearch WebFetch")
    tools = cmd[cmd.index("--tools") + 1]
    check("Read→read_file", "read_file" in tools, tools)
    check("WebSearch→web_search", "web_search" in tools, tools)
    check("no raw Claude names left", "Read" not in tools.split(","), tools)


def test_backward_compat_omission():
    print("missing config -> flags omitted (CLI defaults, old behavior):")
    cmd = orchestrator._claude_cmd("prompt", None, None)
    check("bare still grok -p", cmd[0] == "grok" and cmd[-2:] == ["-p", "prompt"], str(cmd))
    check("no --tools when unset", "--tools" not in cmd, str(cmd))
    cmd = orchestrator._claude_cmd("prompt", "grok-4.6", None)
    check("model without tools", "--model" in cmd and "--tools" not in cmd)


def test_config_block_present():
    print("autonomy_config.json llm block wired:")
    llm = validator.load_config().get("llm") or {}
    check("brain_model set", bool(llm.get("brain_model")), str(llm))
    check("brain is grok", str(llm.get("brain_model", "")).startswith("grok"),
          str(llm.get("brain_model")))
    check("risk_desk_model set", bool(llm.get("risk_desk_model")))
    check("risk desk is a different grok",
          llm.get("risk_desk_model") != llm.get("brain_model")
          and str(llm.get("risk_desk_model", "")).startswith("grok"),
          f"{llm.get('brain_model')} vs {llm.get('risk_desk_model')}")
    tools = (llm.get("allowed_tools") or "").replace(",", " ").split()
    check("allowed_tools excludes writers",
          tools and all(t not in tools for t in (
              "Write", "Edit", "Bash", "run_terminal_cmd", "search_replace")))
    check("_llm_settings reads it", orchestrator._llm_settings().get("brain_model")
          == llm.get("brain_model"))


def test_unwrap_accepts_grok_envelope():
    print("grok --output-format json envelope:")
    text, meta = orchestrator._run_claude.__globals__["unwrap_cli_json"](
        '{"text":"hello","stopReason":"end_turn","sessionId":"abc",'
        '"usage":{"input_tokens":1},"total_cost_usd":0.01}'
    )
    check("text extracted", text == "hello")
    check("sessionId mapped", meta.get("session_id") == "abc")
    check("stopReason mapped", meta.get("stop_reason") == "end_turn")
    # Claude-shaped envelope still works (tests + any leftover fixtures).
    text2, _ = orchestrator._run_claude.__globals__["unwrap_cli_json"](
        '{"result":"old"}')
    check("legacy result still works", text2 == "old")


if __name__ == "__main__":
    for fn in (test_full_pinning, test_claude_tool_names_are_mapped,
               test_backward_compat_omission, test_config_block_present,
               test_unwrap_accepts_grok_envelope):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All grok-cmd checks passed.")
