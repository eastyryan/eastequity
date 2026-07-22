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


# The account size every sizing test was written against. Pinned here so the
# assertions below ($1250 clamp, $2000 ceiling, $625 halved budget, $200 floor)
# describe the MATH rather than whatever capital the operator currently runs.
_TEST_BOOK = {
    "starting_capital_usd": 10000,
    "max_position_usd": 2000,
    "min_clamped_position_usd": 200,
    "conviction_max_position_usd": 3000,
}


@pytest.fixture(autouse=True)
def _isolate_kill_switch(tmp_path, monkeypatch):
    """The suite must not read the OPERATOR's live state/KILL_SWITCH or their
    live account size.

    validate_proposals short-circuits the entire batch to KILL_SWITCH_ACTIVE, so
    engaging the switch — a legitimate production action, and exactly what you do
    while reworking risk code — turned 47 tests red and made every safety assertion
    unverifiable at the precise moment you most want to verify it. Halted is when
    the suite matters MOST, so it must be hermetic with respect to that file.

    Redirects the CONFIG path rather than stubbing kill_switch_active(), so the real
    predicate still runs and tests can exercise it by touching the temp file.

    POSITION SIZING is pinned for the same reason (2026-07-21). The sizing suites
    hardcode a $10,000 fixture book (`pf()` in test_risk_sizing.py) but read the
    LIVE config for the notional ceilings, so they were asserting a $10k book
    against whatever cap production happened to carry. Restating the book from a
    $10,000 start to a $1,000 start turned 12 tests red and errored 20 more —
    every one of them a false alarm about arithmetic that had not changed. The
    operator's capital is a deployment fact; the clamp math is what these tests
    exist to protect, so they now own their own book.
    """
    real_load_config = _validator.load_config
    switch = tmp_path / "KILL_SWITCH"

    def _test_config():
        cfg = real_load_config()
        cfg.setdefault("risk_controls", {})["kill_switch_file"] = str(switch)
        ps = cfg.setdefault("position_sizing", {})
        ps["starting_capital_usd"] = _TEST_BOOK["starting_capital_usd"]
        ps["max_position_usd"] = _TEST_BOOK["max_position_usd"]
        ps.setdefault("risk_based_sizing", {})["min_clamped_position_usd"] = (
            _TEST_BOOK["min_clamped_position_usd"])
        ps.setdefault("conviction_tier", {})["max_position_usd"] = (
            _TEST_BOOK["conviction_max_position_usd"])
        return cfg

    monkeypatch.setattr(_validator, "load_config", _test_config)
    return switch


@pytest.fixture
def kill_switch(_isolate_kill_switch):
    """Engage the isolated kill switch for a test that wants it active."""
    _isolate_kill_switch.write_text("engaged by test")
    return _isolate_kill_switch


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    """No test may append to the REAL journal/.

    Found 2026-07-21. Running the suite wrote genuine-looking RISK EVENTS into
    journal/rejected/: every broker-sync test that exercises the circuit breaker called
    journal.log_rejection() against the live path, producing

        {"run_id": "broker-sync", "reasons": ["RISK_EVENT_broker_sync_suspect",
         "zero-equity read ($0.00) over a funded mirror ($10,000.00)"]}

    — the $10,000.00 is test_resting_stops' monkeypatched _starting_capital over an
    empty history, and the $0.00 is the deliberately bad read the test feeds in. 94 of
    these accumulated across 07-19 to 07-22 and were indistinguishable from a real
    broker outage. They were read as evidence the trader had been locked out for days,
    which sent an investigation down the wrong path entirely.

    This is the same defect class as _no_writes_to_real_learning_stores below, and the
    same lesson: fabricated records in an audit trail are worse than missing ones,
    because the loops and the operator both read them as fact. journal/ is an audit
    trail, so it gets the same treatment.
    """
    import journal as _j
    monkeypatch.setattr(_j, "JOURNAL", tmp_path / "journal")


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


# ---------------------------------------------------------------------------
# No test may write into the REAL learning stores.
# ---------------------------------------------------------------------------
# This has now happened twice, both times through the same gap: a fixture that
# isolates SOME learning modules and misses one, letting a synthetic stub chain
# through into production data.
#
#   2026-07-16  test_exit_grade's fixture omitted post_exit_runners, so a
#               fabricated HPE record (exit 40.00, pnl -50.00, days_held 20 vs a
#               reality of 46.08 / -45.33 / 4) was committed and re-marked on
#               every run for three days, while the runner loop computed
#               leftover-gain against a trade that never happened.
#   2026-07-19  test_runner_quarantine's fixture isolated the runner file but not
#               concept_memory, so a synthetic DELL track (100 -> 130) wrote
#               "price recovered +30.0% above exit" into the real
#               data/concept_memory/DELL.json.
#
# Per-fixture discipline has failed twice, so this is enforced centrally instead.
# Fabricated learning inputs are worse than missing ones: the loops report healthy
# while studying fiction, and nothing downstream can tell the difference.
_GUARDED_LEARNING_PATHS = (
    "data/concept_memory",
    "data/post_exit_runners.json",
    "data/knowledge_base.json",
    "data/adopted_lessons.json",
    "data/shadow_portfolio.json",
    "data/binding_exit_lessons.json",
    "journal/exit_autopsies",
)


def _learning_store_fingerprint() -> dict:
    """mtime+size of every real learning store, cheaply."""
    root = Path(__file__).resolve().parent.parent
    out: dict = {}
    for rel in _GUARDED_LEARNING_PATHS:
        p = root / rel
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    st = f.stat()
                    out[str(f)] = (st.st_mtime_ns, st.st_size)
        elif p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


@pytest.fixture(autouse=True)
def _no_writes_to_real_learning_stores():
    before = _learning_store_fingerprint()
    yield
    after = _learning_store_fingerprint()
    touched = sorted(
        {k for k in set(before) | set(after) if before.get(k) != after.get(k)})
    if touched:
        rels = [t.split("east-equity-agent/")[-1] for t in touched][:5]
        raise AssertionError(
            "test wrote into the REAL learning stores: " + ", ".join(rels)
            + ". Isolate them in the fixture (see tmp_learning in "
              "tests/test_learning_systems.py — patch MEM_DIR/ROOT for every "
              "module the code under test chains into, not just the obvious one). "
              "A synthetic record in production learning data is the 2026-07-16 "
              "and 2026-07-19 incidents.")


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
