"""Checks for the 2026-08-05 orchestrator decomposition. Pure Python:

    python3 tests/test_orchestrator_phases.py

Covers the phase helpers extracted from the old ~900-line main() (dict-fixture
unit tests), the STALE_BUNDLE_AGE_HOURS single-source constant, the
_partial_quality_note dedup, and runlib.preflight.tests_gate (exercised only
against THROWAWAY temp directories — never against this repo's own suite, so
there is no recursion and no conftest state swap here).

Hermetic by construction: no network (the _build_market_context fixture carries
its own market cap so the yfinance fallback is never reached), no writes under
the repo (journal is stubbed where a helper would journal; pytest cache is
disabled in the gate's own command), runnable with or without conftest.py.
"""

import inspect
import json
import os
import shutil
import subprocess  # noqa: F401  (documents what tests_gate shells out to)
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Never let an import construct a live broker adapter when run standalone
# (conftest pins this too, but this file must not depend on conftest).
os.environ.setdefault("EE_BROKER", "simulation")

import orchestrator  # noqa: E402
from runlib.depths import depth_allows_new_buys  # noqa: E402
from runlib.preflight import tests_gate  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)
    # Unlike the older check() files, this also asserts so a failure is loud
    # under pytest too (the __main__ runner below catches per-test and keeps
    # going, preserving the run-everything report style).
    assert cond, f"{name}" + (f" - {detail}" if detail else "")


def _ctx(age_hours=0.5, scanned=100, requested=100, macro_ok=True,
         top_setups=("AAA",), feeds_ok=True):
    """Bundle fixture, same shape as tests/test_data_quality_reaches_validator."""
    gathered = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    feed = ({"status": "ok", "tickers": {"AAA": {"status": "ok"}}} if feeds_ok
            else {"status": "ok", "tickers": {"AAA": {"error": "blocked"}}})
    return {
        "run_date": gathered.isoformat(),
        "universe_scan": {"top_setups": [{"ticker": t} for t in top_setups],
                          "status": "ok", "scanned": scanned,
                          "requested": requested},
        "macro_regime": {"status": "ok" if macro_ok else "error"},
        "news_and_catalysts": feed, "sec_filings": feed, "insider_activity": feed,
    }


# ---------------------------------------------------------------------------
# Item 2: one staleness constant, one note function
# ---------------------------------------------------------------------------

def test_staleness_threshold_is_single_sourced():
    print("staleness threshold single-sourced:")
    check("constant is 4.0 hours", orchestrator.STALE_BUNDLE_AGE_HOURS == 4.0)
    default = inspect.signature(orchestrator.label_data_quality) \
        .parameters["max_age_hours"].default
    check("label_data_quality default IS the constant",
          default is orchestrator.STALE_BUNDLE_AGE_HOURS, str(default))
    src = inspect.getsource(orchestrator._run_gather_only)
    check("gather-only relay branch uses the constant (both sites)",
          src.count("STALE_BUNDLE_AGE_HOURS") >= 2)
    check("no bare 4-hour literal left in the relay branch",
          "age_h < 4 " not in src and "age_h >= 4," not in src)


def test_partial_quality_note_is_the_one_source():
    print("_partial_quality_note dedup:")
    low = orchestrator._partial_quality_note(
        {"low_coverage": True, "scanned": 40, "requested": 100, "dead_feeds": []})
    check("low-coverage wording", "40/100" in low and "unscanned" in low, low)
    dead = orchestrator._partial_quality_note(
        {"low_coverage": False, "dead_feeds": ["sec_filings"]})
    check("dead-feeds wording names the feed", "sec_filings" in dead, dead)
    # label_data_quality must route through it (feeds dead -> live_partial).
    ctx = _ctx(feeds_ok=False)
    label = orchestrator.label_data_quality(ctx, "full")
    health = orchestrator.assess_bundle_health(ctx, "full")
    check("label_data_quality partial note comes from the shared function",
          label.get("source") == "live_partial"
          and label.get("note") == orchestrator._partial_quality_note(health),
          str(label))
    # The gather-only branch must call it too, not carry its own copy.
    gsrc = inspect.getsource(orchestrator._run_gather_only)
    check("gather-only branch calls _partial_quality_note",
          "_partial_quality_note(" in gsrc)
    check("gather-only drifted copy is gone",
          "one or more research feeds" not in gsrc)


def test_label_data_quality_age_behavior_unchanged():
    print("label_data_quality ages a bundle exactly as before:")
    label = orchestrator.label_data_quality(_ctx(age_hours=5), "full")
    check("5h-old healthy bundle is stale", label.get("stale") is True
          and abs(label.get("age_hours") - 5) < 0.1, str(label))
    label = orchestrator.label_data_quality(_ctx(age_hours=0.5), "full")
    check("fresh healthy bundle is live, not stale",
          label.get("source") == "live" and not label.get("stale"), str(label))


# ---------------------------------------------------------------------------
# Item 1: extracted phase helpers, driven with dict fixtures
# ---------------------------------------------------------------------------

def test_parse_args_cli_surface():
    print("_parse_args keeps the CLI surface:")
    a = orchestrator._parse_args([])
    check("all-default invocation",
          not a.research_only and not a.gather_only and not a.manual
          and a.depth is None and a.act_on is None and a.note is None)
    check("--depth passes through",
          orchestrator._parse_args(["--depth", "full"]).depth == "full")
    a = orchestrator._parse_args(["--act-on", "r.json", "--context", "c.json"])
    check("cloud act-on pair", a.act_on == "r.json" and a.context == "c.json")
    a = orchestrator._parse_args(["--gather-only", "--auto-depth"])
    check("gather flags", a.gather_only and a.auto_depth)
    exited = None
    try:
        orchestrator._parse_args(["--no-such-flag"])
    except SystemExit as e:
        exited = e.code
    check("unknown flag exits 2 (argparse behavior preserved)", exited == 2)


def test_depth_gates_matrix():
    print("_depth_gates matrix:")
    ns = lambda news_only=False: types.SimpleNamespace(news_only=news_only)  # noqa: E731
    check("full trades", orchestrator._depth_gates(ns(), "full") == (False, True))
    check("holdings_watchlist trades",
          orchestrator._depth_gates(ns(), "holdings_watchlist") == (False, True))
    check("light: no new BUYs, safety on",
          orchestrator._depth_gates(ns(), "light") == (True, True))
    check("weekly_market: no orders, safety still on",
          orchestrator._depth_gates(ns(), "weekly_market") == (True, True))
    check("evening_review: no orders, no safety",
          orchestrator._depth_gates(ns(), "evening_review") == (True, False))
    check("news-only forces both off",
          orchestrator._depth_gates(ns(True), "full") == (True, False))
    # Internal consistency with the depth module the gates derive from.
    for d in ("full", "holdings_watchlist", "light"):
        nb, _ = orchestrator._depth_gates(ns(), d)
        check(f"{d} agrees with depth_allows_new_buys",
              nb == (not depth_allows_new_buys(d)))


def test_process_gate_inputs_assembly():
    print("_process_gate_inputs from a dict fixture:")
    ctx = {
        "universe_scan": {"top_setups": [{"ticker": f"T{i}"} for i in range(10)]},
        "portfolio": {"positions": [{"ticker": "dell"}, {"ticker": None}]},
        "factor_map": {"requires_factor_response": True,
                       "concentration_level": "high"},
        "momentum_health": {"status": "softening"},
        "watchlist_trigger_alerts": {"alerts": [{"ticker": "anet"}, "junk",
                                                {"no_ticker": 1}]},
        "engagement": {"requires_commitment": True},
    }
    cfg = {"swing_rules": {"max_unbought_trigger_hits": 2}}
    gi = orchestrator._process_gate_inputs(ctx, cfg, "holdings_watchlist")
    check("top-8 scan tickers", gi["top_scan_tickers"] == [f"T{i}" for i in range(8)])
    check("held tickers uppercased, Nones dropped", gi["held_tickers"] == ["DELL"])
    check("seat reviews required on a trading depth with a book",
          gi["require_seat_reviews"] is True)
    check("factor response required", gi["require_factor_response"] is True)
    check("concentration level forwarded",
          gi["factor_concentration_level"] == "high")
    check("momentum status forwarded", gi["momentum_status"] == "softening")
    check("fired triggers filtered + uppercased", gi["fired_triggers"] == ["ANET"])
    check("unbought hits is a dict", isinstance(gi["unbought_hits"], dict))
    check("commitment flag forwarded", gi["requires_commitment"] is True)
    check("max unbought hits from config", gi["max_unbought_hits"] == 2)
    gi2 = orchestrator._process_gate_inputs(ctx, {}, "light")
    check("light depth never requires seat reviews",
          gi2["require_seat_reviews"] is False)
    check("max unbought hits defaults to 3", gi2["max_unbought_hits"] == 3)


class _JournalStub:
    """Records journal calls in memory so no test writes the REAL journal/."""

    def __init__(self):
        self.rejections, self.improvements = [], []

    def log_rejection(self, p, reasons, run_id):
        self.rejections.append((p.get("ticker"), list(reasons)))

    def log_improvement(self, note, run_id):
        self.improvements.append(note)


def test_apply_process_audit_blocks_buys_only():
    print("_apply_process_audit teeth:")
    saved = orchestrator.journal
    orchestrator.journal = stub = _JournalStub()
    try:
        audit = {"process_ok": False, "full_run_no_trade_ok": None,
                 "issues": ["seat_reviews_missing"],
                 "blocking_issues": ["seat_reviews_missing"],
                 "blocks_new_buys": True, "watchlist": [], "rejected_ideas": [],
                 "seat_reviews": [], "factor_response": None,
                 "signal_discipline": {}, "engagement": {}}
        parsed = {}
        proposals = [{"ticker": "NVDA", "action": "BUY"},
                     {"ticker": "DELL", "action": "SELL_TO_CLOSE"}]
        out, ntr = orchestrator._apply_process_audit(
            audit, parsed, proposals, None, [], "holdings_watchlist", "TESTRUN")
        check("BUY dropped, SELL kept",
              [p["action"] for p in out] == ["SELL_TO_CLOSE"], str(out))
        check("parsed.proposals mirrors the drop", parsed["proposals"] == out)
        check("rejection journaled with the gate reason",
              stub.rejections and stub.rejections[0][0] == "NVDA"
              and "process_gate_blocked" in stub.rejections[0][1][0],
              str(stub.rejections))
        check("issues journaled as an improvement note",
              stub.improvements and "seat_reviews_missing" in stub.improvements[0])
        check("process_audit folded into parsed",
              parsed["process_audit"]["blocks_new_buys"] is True)
    finally:
        orchestrator.journal = saved


def test_apply_process_audit_tags_weak_full_run_no_trade():
    print("_apply_process_audit full-run no-trade tag:")
    saved = orchestrator.journal
    orchestrator.journal = _JournalStub()
    try:
        audit = {"process_ok": True, "full_run_no_trade_ok": False, "issues": [],
                 "blocking_issues": [], "blocks_new_buys": False,
                 "watchlist": [], "rejected_ideas": [], "seat_reviews": [],
                 "factor_response": None, "signal_discipline": {},
                 "engagement": {}}
        parsed = {}
        out, ntr = orchestrator._apply_process_audit(
            audit, parsed, [], "quiet tape", [], "full", "TESTRUN")
        check("no-trade reason tagged",
              "full-run no-trade needs rejected_ideas" in str(ntr), str(ntr))
        check("tag mirrored into parsed", parsed.get("no_trade_reason") == ntr)
    finally:
        orchestrator.journal = saved


def test_equity_risk_halts_always_returns_a_list():
    print("_equity_risk_halts shape (fail-closed contract):")
    cfg = {"risk_controls": {"max_daily_loss_pct_halt": 0.03,
                             "max_drawdown_pct_halt": 0.15}}
    out = orchestrator._equity_risk_halts({"total_equity_usd": None}, cfg)
    check("returns a list", isinstance(out, list), str(out))
    out = orchestrator._equity_risk_halts({}, cfg)
    check("empty portfolio dict still a list", isinstance(out, list), str(out))


def test_build_market_context_reserved_feeds():
    print("_build_market_context from a dict fixture (no network):")
    ctx = {
        "universe_scan": {"benchmark_trend": {"spy_above_200dma": False}},
        "momentum_health": {"status": "unwind", "momentum_unwind": True},
        "data_quality": {"source": "aged_bundle", "stale": True,
                         "age_hours": 9.0},
        "fundamentals_freshness": {"stale_tickers": ["msft"]},
        "portfolio_risk": {"status": "ok", "portfolio_beta": 1.2,
                           "avg_pairwise_holding_corr": 0.8,
                           "shared_left_tail": True,
                           "holdings": {"DELL": {"beta": 1.4}},
                           "candidates": {}},
        # market cap present here so the yfinance network fallback never runs
        "deep_fundamentals": {"NVDA": {"quality_ratios": {"ratios": {
            "market_cap_usd": {"value": 4.4e12}}}}},
    }
    proposals = [{"ticker": "NVDA", "action": "BUY"},
                 {"ticker": "DELL", "action": "SELL_TO_CLOSE"}]
    mc = orchestrator._build_market_context(ctx, proposals)
    check("_regime: SPY below 200dma derived",
          mc["_regime"]["spy_below_200dma"] is True, str(mc.get("_regime")))
    check("_regime: unwind flagged", mc["_regime"]["momentum_unwind"] is True)
    check("_data_quality: stale + tickers uppercased",
          mc["_data_quality"]["stale"] is True
          and mc["_data_quality"]["stale_tickers"] == ["MSFT"],
          str(mc.get("_data_quality")))
    check("_book: betas and shared left tail",
          mc["_book"]["betas"] == {"DELL": 1.4}
          and mc["_book"]["shared_left_tail"] is True, str(mc.get("_book")))
    check("BUY market cap injected from deep_fundamentals",
          mc.get("NVDA", {}).get("market_cap_usd") == 4.4e12)
    check("non-BUY gets no market-cap row", "market_cap_usd" not in mc.get("DELL", {}))
    check("_bundle rides along (identity, not copy)", mc["_bundle"] is ctx)


def test_module_surface_preserved():
    print("module import surface other tests/tools rely on:")
    for name in ("feed_is_alive", "assess_bundle_health", "bundle_age_hours",
                 "label_data_quality", "_should_mark_run_start",
                 "expected_slots", "compute_closed_trades",
                 "write_tiered_context", "gather_context", "main",
                 "_claude_cmd", "_llm_settings", "_sector_map",
                 "_sector_exposure"):
        check(f"orchestrator.{name} reachable",
              callable(getattr(orchestrator, name, None)))


def test_pinned_call_sites_still_inline_in_main():
    """Mirror of the EXTERNAL pins so a future refactor fails fast here too:
    runlib/capabilities._probe_process_gate_flags and tests/test_process_gates
    source-inspect main() for the process-gate call's literal kwargs, and
    tests/test_capability_audit pins the capability-audit call in main()."""
    print("pinned call sites remain inline in main():")
    src = inspect.getsource(orchestrator.main)
    check("capability audit call in main", "audit_capabilities()" in src)
    check("capability halt reason in main", "capability_dead" in src)
    check("tests gate wired in main", "tests_gate(" in src
          and "tests_red:" in src)
    i = src.find("audit_brain_process(")
    check("process-gate call in main", i >= 0)
    call = src[i:]
    call = call[:call.index(")\n") + 1]
    for kw in ("held_tickers", "require_seat_reviews", "require_factor_response",
               "factor_concentration_level"):
        check(f"call passes {kw}", kw in call)


# ---------------------------------------------------------------------------
# Item 4: tests_gate — exercised against throwaway suites only
# ---------------------------------------------------------------------------

def _gate_case(test_body, **kw):
    """Run tests_gate against a scratch repo containing one test file."""
    root = tempfile.mkdtemp(prefix="ee_tests_gate_")
    try:
        tdir = Path(root) / "tests"
        tdir.mkdir()
        (tdir / "test_case.py").write_text(test_body)
        return tests_gate(repo_root=root, **kw)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tests_gate_disabled_and_missing_pytest():
    print("tests_gate off-switch + fail-open when it cannot run:")
    r = tests_gate(enabled=False)
    check("disabled: status", r["status"] == "disabled", str(r))
    check("disabled: never blocks", r["blocks_new_buys"] is False)
    import importlib.util
    if importlib.util.find_spec("pytest") is None:
        r = tests_gate()
        check("no pytest: fails OPEN",
              r["status"] == "no_pytest" and r["blocks_new_buys"] is False,
              str(r))


def test_tests_gate_verdicts():
    import importlib.util
    if importlib.util.find_spec("pytest") is None:
        print("tests_gate verdicts: SKIPPED (pytest not importable here)")
        return
    print("tests_gate verdicts against throwaway suites:")
    r = _gate_case("def test_ok():\n    assert True\n", timeout_s=120)
    check("green suite: status green", r["status"] == "green", str(r))
    check("green suite: never blocks", r["blocks_new_buys"] is False)
    check("green suite: exit code 0", r["returncode"] == 0)

    r = _gate_case("def test_fails():\n    assert False, 'deliberate'\n",
                   timeout_s=120)
    check("red suite: status red", r["status"] == "red", str(r))
    check("red suite: BLOCKS new BUYs", r["blocks_new_buys"] is True)
    check("red suite: names the first failing test",
          r["first_failing_test"] is not None
          and "test_case.py::test_fails" in r["first_failing_test"], str(r))

    r = _gate_case("", timeout_s=120)  # nothing collected -> pytest exit 5
    check("nothing collected: inconclusive, fails OPEN",
          r["status"] == "error" and r["blocks_new_buys"] is False, str(r))

    r = _gate_case("import time\n\ndef test_slow():\n    time.sleep(30)\n",
                   timeout_s=5)
    check("wall-clock timeout: fails OPEN",
          r["status"] == "timeout" and r["blocks_new_buys"] is False, str(r))


_TESTS = [
    test_staleness_threshold_is_single_sourced,
    test_partial_quality_note_is_the_one_source,
    test_label_data_quality_age_behavior_unchanged,
    test_parse_args_cli_surface,
    test_depth_gates_matrix,
    test_process_gate_inputs_assembly,
    test_apply_process_audit_blocks_buys_only,
    test_apply_process_audit_tags_weak_full_run_no_trade,
    test_equity_risk_halts_always_returns_a_list,
    test_build_market_context_reserved_feeds,
    test_module_surface_preserved,
    test_pinned_call_sites_still_inline_in_main,
    test_tests_gate_disabled_and_missing_pytest,
    test_tests_gate_verdicts,
]

if __name__ == "__main__":
    for fn in _TESTS:
        try:
            fn()
        except AssertionError:
            pass  # already recorded in FAILURES; keep reporting the rest
        print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All orchestrator-phase checks passed.")
