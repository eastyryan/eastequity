"""Pytest bridge for the standalone exec-audit regression scripts.

The test_exec_audit_*.py scripts do process-global surgery at import time —
redirecting simulated_broker.STATE_FILE / alpaca_broker.INTENTS_FILE /
journal.JOURNAL to tempdirs and stubbing time.sleep — which is exactly right
for their designed standalone mode (one pristine interpreter per script) and
exactly wrong for in-process pytest collection: with several of them imported
into one session their module-level redirections overlapped and broke pytest's
setup/teardown state for unrelated tests (measured 2026-08-05: a
"previous item was not torn down properly" cascade). conftest.py therefore
collect-ignores them, and THIS file runs each one in its own interpreter so CI
still executes every check.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(HERE.glob("test_exec_audit_*.py"))


def test_scripts_were_found():
    assert len(SCRIPTS) >= 6, f"expected the exec-audit scripts next to this file, got {SCRIPTS}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_exec_audit_script(script):
    env = dict(os.environ, EE_BROKER="simulation")
    r = subprocess.run([sys.executable, str(script)], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, (
        f"{script.name} failed (exit {r.returncode}):\n"
        f"--- stdout ---\n{r.stdout[-3000:]}\n--- stderr ---\n{r.stderr[-2000:]}")
