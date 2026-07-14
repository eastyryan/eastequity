"""Regression tests for the 2026-07-14 audit fixes (pytest-style, offline).

Covers:
  * plan integrity: journal-plan joins use FILLED proposals only, and
    backfill_position_plans self-heals a plan that a rejected proposal
    overwrote (the HPE incident);
  * fundamental_screen keep-last-good on a degraded refresh;
  * freshness_audit never persists an all-error artifact over a good one;
  * discovery_screen never persists an empty sweep over a real one;
  * corporate-actions splits scale the persisted plan's price levels
    (no phantom "stop_loss_breached" after a split);
  * guidance ledger per-metric consecutive_beats.
"""

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# plan integrity (C3)
# ---------------------------------------------------------------------------
FILLED_PLAN = {"stop_loss": 43.75, "target_price": 58.0,
               "holding_horizon_days": 40, "entry_price_max": 49.75,
               "confidence": 0.64}
REJECTED_PLAN = {"stop_loss": 44.75, "target_price": 56.5,
                 "holding_horizon_days": 40, "entry_price_max": 49.25,
                 "confidence": 0.67}


def _seed_plan_repo(tmp_path, corrupted_plan=True, adds_count=0):
    """Fake repo root: a filled BUY (run-filled) followed by a REJECTED BUY
    (run-rej) on the same ticker, with the position's persisted plan set to the
    rejected proposal's numbers (the pre-fix corruption)."""
    (tmp_path / "journal" / "proposals").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "autonomy_config.json").write_text(json.dumps(
        {"mode": {"broker": "simulation", "trading_mode": "paper"}}))

    recs = [
        {"ts": "2026-07-10T19:14:00+00:00", "run_id": "run-filled",
         "proposal": {"action": "BUY", "ticker": "HPE", "thesis": "filled thesis",
                      **FILLED_PLAN}},
        {"ts": "2026-07-10T19:15:00+00:00", "run_id": "run-rej",
         "proposal": {"action": "BUY", "ticker": "HPE", "thesis": "rejected thesis",
                      **REJECTED_PLAN}},
    ]
    (tmp_path / "journal" / "proposals" / "2026-07-10.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n")

    pos = {"ticker": "HPE", "quantity": 16.3771, "avg_cost": 48.8488,
           "market_value_usd": 800.0, "opened_at": "2026-07-10T19:14:48+00:00",
           "proposal_id": "run-filled"}
    if adds_count:
        pos["adds_count"] = adds_count
    if corrupted_plan:
        pos["plan"] = dict(REJECTED_PLAN)
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps({
        "cash_usd": 8200.0, "total_equity_usd": 10000.0, "positions": [pos],
        "pending_orders": {},
        "history": [{"ticker": "HPE", "action": "BUY", "status": "filled",
                     "proposal_id": "run-filled", "fill_price": 48.8488,
                     "quantity": 16.3771,
                     "filled_at": "2026-07-10T19:14:48+00:00"}],
    }))
    return tmp_path


@pytest.fixture
def plan_repo(tmp_path, monkeypatch):
    import tools.portfolio_state as PS
    from execution import simulated_broker as SB
    root = _seed_plan_repo(tmp_path)
    monkeypatch.setattr(PS, "ROOT", root)
    monkeypatch.setattr(SB, "STATE_FILE", root / "state" / "portfolio.json")
    return root


def test_journal_plans_use_filled_proposal_only(plan_repo):
    import tools.portfolio_state as PS
    plans = PS._journal_plans()
    assert plans["HPE"]["stop_loss"] == FILLED_PLAN["stop_loss"]
    assert plans["HPE"]["confidence"] == FILLED_PLAN["confidence"]
    assert plans["HPE"]["thesis"] == "filled thesis"  # rejected never a source


def test_backfill_repairs_rejected_plan(plan_repo):
    import tools.portfolio_state as PS
    changed = PS.backfill_position_plans()
    assert changed == 1
    state = json.loads((plan_repo / "state" / "portfolio.json").read_text())
    assert state["positions"][0]["plan"] == FILLED_PLAN
    # idempotent: second run changes nothing
    assert PS.backfill_position_plans() == 0


def test_backfill_never_touches_scaled_in_positions(tmp_path, monkeypatch):
    import tools.portfolio_state as PS
    from execution import simulated_broker as SB
    root = _seed_plan_repo(tmp_path, corrupted_plan=True, adds_count=1)
    monkeypatch.setattr(PS, "ROOT", root)
    monkeypatch.setattr(SB, "STATE_FILE", root / "state" / "portfolio.json")
    assert PS.backfill_position_plans() == 0  # an add's plan legitimately replaces


# ---------------------------------------------------------------------------
# fundamental_screen keep-last-good (H2)
# ---------------------------------------------------------------------------
def test_screen_refresh_keeps_last_good_on_degraded_feed(tmp_path, monkeypatch):
    import tools.fundamental_screen as FS
    monkeypatch.setattr(FS, "CACHE", tmp_path / "fundamental_screen.json")
    monkeypatch.setattr(FS, "_fetch_row",
                        lambda t: {"ticker": t, "eps_estimate_current_yr": 1.0})
    good = FS.refresh_screen(["A", "B", "C", "D"])
    assert len(good["rows"]) == 4

    monkeypatch.setattr(FS, "_fetch_row", lambda t: None)  # feed dies
    kept = FS.refresh_screen(["A", "B", "C", "D"])
    assert kept["rows"] == good["rows"], "empty refresh must serve last-good"
    on_disk = json.loads((tmp_path / "fundamental_screen.json").read_text())
    assert on_disk["current"]["rows"] == good["rows"], "file must not be clobbered"


# ---------------------------------------------------------------------------
# freshness_audit artifact guard (H1)
# ---------------------------------------------------------------------------
def test_all_error_audit_never_persisted(tmp_path, monkeypatch):
    import tools.freshness_audit as FA
    artifact = tmp_path / "freshness_audit.json"
    artifact.write_text(json.dumps({"audited": 10, "fresh": 10,
                                    "audited_at_et": "2026-07-12"}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "universe.json").write_text(json.dumps(
        {"sectors": {"x": ["A", "B", "C"]}}))
    monkeypatch.setattr(FA, "ARTIFACT", artifact)
    monkeypatch.setattr(FA, "ROOT", tmp_path)
    monkeypatch.setattr(FA, "audit_freshness", lambda *a, **k: {
        "status": "ok", "audited": 3, "fresh": 0, "stale_tickers": [],
        "error_tickers": ["A", "B", "C"], "foreign_annual_filers": [],
        "value_mismatch_vs_yfinance": [], "results": []})
    out = FA.audit_universe(cross_check=False, progress=False)
    assert "artifact_error" in out
    assert json.loads(artifact.read_text())["fresh"] == 10, "last-good destroyed"


# ---------------------------------------------------------------------------
# discovery_screen keep-last-good (M10)
# ---------------------------------------------------------------------------
def test_empty_discovery_sweep_keeps_last_good(tmp_path, monkeypatch):
    import tools.discovery_screen as DS
    outp = tmp_path / "discovery_screen.json"
    outp.write_text(json.dumps({"as_of": "yday",
                                "top_candidates": [{"ticker": "X"}]}))
    monkeypatch.setattr(DS, "OUTPUT_PATH", outp)

    empty = {"as_of": "now", "top_candidates": []}
    DS._write_output(empty)
    assert "write_skipped" in empty
    assert json.loads(outp.read_text())["top_candidates"] == [{"ticker": "X"}]

    full = {"as_of": "now", "top_candidates": [{"ticker": "Y"}]}
    DS._write_output(full)
    assert json.loads(outp.read_text())["top_candidates"] == [{"ticker": "Y"}]


# ---------------------------------------------------------------------------
# splits scale the persisted plan (H4)
# ---------------------------------------------------------------------------
class _Ts:
    def __init__(self, dt):
        self._dt = dt

    def to_pydatetime(self):
        return self._dt


def test_split_scales_plan_price_levels(tmp_path, monkeypatch):
    from execution import simulated_broker as SB
    from execution import corporate_actions as CA

    state_file = tmp_path / "portfolio.json"
    state_file.write_text(json.dumps({
        "cash_usd": 1000.0, "total_equity_usd": 2000.0,
        "positions": [{"ticker": "SPLT", "quantity": 2.0, "avg_cost": 500.0,
                       "market_value_usd": 1000.0, "last_price": 500.0,
                       "opened_at": "2026-07-01T00:00:00+00:00",
                       "plan": {"stop_loss": 450.0, "target_price": 600.0,
                                "entry_price_max": 510.0,
                                "holding_horizon_days": 40, "confidence": 0.65}}],
        "pending_orders": {}, "history": [],
    }))
    monkeypatch.setattr(SB, "STATE_FILE", state_file)

    split_dt = datetime(2026, 7, 10, tzinfo=timezone.utc)
    fake_yf = types.ModuleType("yfinance")

    class _FakeTicker:
        def __init__(self, _):
            self.dividends = {}
            self.splits = {_Ts(split_dt): 10.0}

    fake_yf.Ticker = _FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    summary = CA.apply_corporate_actions()
    assert summary["splits_applied"] == 1

    pos = json.loads(state_file.read_text())["positions"][0]
    assert pos["quantity"] == 20.0
    assert pos["avg_cost"] == 50.0
    # the whole point: plan levels scale too, or the exit guard force-closes
    # a healthy position as stop_loss_breached at 1/10th prices
    assert pos["plan"]["stop_loss"] == 45.0
    assert pos["plan"]["target_price"] == 60.0
    assert pos["plan"]["entry_price_max"] == 51.0
    assert pos["plan"]["holding_horizon_days"] == 40  # non-price fields untouched


# ---------------------------------------------------------------------------
# guidance ledger: per-metric consecutive_beats (M6)
# ---------------------------------------------------------------------------
def test_consecutive_beats_per_metric(tmp_ledger):
    import tools.guidance_ledger as G
    G.record_guidance([
        {"ticker": "ZZZ", "metric": "revenue", "period": "FY2026Q1",
         "guide_low": 10.0, "guide_high": 11.0, "unit": "usd_billions"},
        {"ticker": "ZZZ", "metric": "revenue", "period": "FY2026Q2",
         "guide_low": 11.0, "guide_high": 12.0, "unit": "usd_billions"},
        {"ticker": "ZZZ", "metric": "eps", "period": "FY2026Q2",
         "guide_low": 1.00, "guide_high": 1.10, "unit": "usd_per_share"},
    ])
    G.grade_guidance("ZZZ", {
        "revenue": {"FY2026Q1": 11.5e9, "FY2026Q2": 12.5e9},  # two beats
        "eps": {"FY2026Q2": 0.80},                            # a miss
    })
    s = G.get_ledger_summary(["ZZZ"])["ZZZ"]
    assert s["consecutive_beats"]["revenue"] == 2, s
    assert s["consecutive_beats"]["eps"] == 0, s
    # the old mixed-metric walk would have truncated the revenue streak to 0
    # whenever the interleaved EPS miss sorted last
