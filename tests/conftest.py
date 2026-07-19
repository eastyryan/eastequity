"""Shared pytest fixtures. The test files remain runnable as plain scripts
(`python3 tests/test_x.py`); everything here loads only under pytest.

Regression context (2026-07-14 audit): test_execution_costs patched
simulated_broker module attributes (STATE_FILE, _execution_costs) and never
restored them, so test_mark_to_market / test_position_plan_persistence failed
in full-suite order while passing in isolation - and worse, the script-style
files whose seeding runs only under __main__ were hitting (and MUTATING) the
REAL state/portfolio.json when collected by pytest. The autouse snapshot below
plus per-module seeding fixtures in those files close both holes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator as _validator  # noqa: E402
from execution import simulated_broker as _sb  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_kill_switch(tmp_path, monkeypatch):
    """The suite must not read the OPERATOR's live state/KILL_SWITCH.

    validate_proposals short-circuits the entire batch to KILL_SWITCH_ACTIVE, so
    engaging the switch — a legitimate production action, and exactly what you do
    while reworking risk code — turned 47 tests red and made every safety assertion
    unverifiable at the precise moment you most want to verify it. Halted is when
    the suite matters MOST, so it must be hermetic with respect to that file.

    Redirects the CONFIG path rather than stubbing kill_switch_active(), so the real
    predicate still runs and tests can exercise it by touching the temp file.
    """
    real_load_config = _validator.load_config
    switch = tmp_path / "KILL_SWITCH"

    def _test_config():
        cfg = real_load_config()
        cfg.setdefault("risk_controls", {})["kill_switch_file"] = str(switch)
        return cfg

    monkeypatch.setattr(_validator, "load_config", _test_config)
    return switch


@pytest.fixture
def kill_switch(_isolate_kill_switch):
    """Engage the isolated kill switch for a test that wants it active."""
    _isolate_kill_switch.write_text("engaged by test")
    return _isolate_kill_switch


@pytest.fixture(autouse=True)
def _pin_simulation_backend(monkeypatch):
    """Tests must NEVER dispatch to a real broker API, regardless of what
    autonomy_config.json's mode.broker says (post-Alpaca-cutover it says
    alpaca_paper). EE_BROKER overrides the router; test_alpaca_broker.py
    exercises the adapter explicitly with a faked transport."""
    monkeypatch.setenv("EE_BROKER", "simulation")


@pytest.fixture(autouse=True)
def _restore_simulated_broker():
    """Snapshot broker module attributes before each test, restore after -
    a test that patches them can never leak into the next test."""
    state_file = _sb.STATE_FILE
    exec_costs = _sb._execution_costs
    yield
    _sb.STATE_FILE = state_file
    _sb._execution_costs = exec_costs


@pytest.fixture
def tmp_ledger(tmp_path):
    """Point the guidance ledger at a temp file for the duration of a test."""
    import tools.guidance_ledger as G
    orig = G.LEDGER_FILE
    G.LEDGER_FILE = tmp_path / "guidance_ledger.json"
    try:
        yield G.LEDGER_FILE
    finally:
        G.LEDGER_FILE = orig


def pytest_runtest_teardown(item):
    """Make the legacy print-based check() pattern actually FAIL under pytest.

    Most suites here use `check(name, cond)` helpers that print PASS/FAIL and
    append to a module-level FAILURES list, exiting non-zero only in script
    mode. CI runs pytest, where a failed check counted as a passing test -
    safety-rule regressions sailed through green (2026-07-15 review finding).
    Drain each module's FAILURES after every test and fail the test if
    anything accumulated, so both invocation styles now agree."""
    fails = getattr(getattr(item, "module", None), "FAILURES", None)
    if fails:
        pending = list(fails)
        del fails[:]  # drain so later tests in the module report their own
        raise AssertionError(f"{len(pending)} failed check(s): {pending}")
