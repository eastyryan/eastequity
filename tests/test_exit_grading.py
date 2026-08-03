"""Offline tests for exit grading and the post-exit runner backfill.

    ./.venv311/bin/python -m pytest tests/test_exit_grading.py -q

THREE DEFECTS THESE TESTS PIN (all audited 2026-08-03, all on the live record)

  1. data/post_exit_runners.json read `{"tracking": [], "completed": []}` against
     two closed trades, for nineteen days. Seeding was close-time-only, so every
     way the store could lose a record — a dedupe collision with the fabricated
     HPE record of 2026-07-16, a stale copy hand-committed in f02af98, the
     2026-07-19 cleanup that emptied the file, an exit that took the queued path
     — was permanent and silent. The quarantine loop added afterwards only ever
     REMOVED records, so the study could only converge on zero.

  2. The HPE close carries `stop_loss: None` and `forced_exit_reason: None` on
     its ledger row, so nothing could say what plan it was judged against or
     whether a rule or a judgement closed it — while the DELL row two entries
     above carries both.

  3. DELL's plan stop was 408.00 and it filled at 389.7542: 4.47% of the stop
     level, 0.549 ATR through it, a 1%-risk position realizing -13.5%.
     `execution_costs.stop_gap_atr_fraction` (0.5) models exactly this and
     book_risk spends heat budget on it, and no exit had ever been compared
     against it.

Everything here is offline. Real numbers from the real ledger are used as
fixtures, because a grader tested only on round numbers is a grader tested on
nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import exit_grade as EG            # noqa: E402
import tools.post_exit_runners as PR          # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures — the two real closes, verbatim from state/portfolio.json history
# --------------------------------------------------------------------------- #
DELL_EXIT = {
    "ticker": "DELL", "action": "SELL_TO_CLOSE", "reference_price": 400.52,
    "proposal_id": "20260715-e2c95f", "forced_exit_reason": "stop_loss_breached",
    "stop_loss": 408.0, "atr_pct": 8.3, "order_id": "SIM-afbba17462",
    "status": "filled", "fill_price": 389.7542, "quantity": 0.22189,
    "avg_cost": 450.6702, "sell_fraction": 1.0,
    "position_opened_at": "2026-07-09T21:41:38.155448+00:00",
    "entry_plan": {"stop_loss": 408.0, "target_price": 525.0,
                   "holding_horizon_days": 45, "entry_price_max": 458.0,
                   "confidence": 0.66},
    "realized_pnl_usd": -13.52, "gap_modeled": True, "gap_usd": 18.2458,
    "fees_usd": {"commission": 0.0, "sec_fee": 0.0024, "taf": 4e-05,
                 "slippage_bps": 10.0, "effective_slippage_pct": 0.415},
    "filled_at": "2026-07-15T16:32:49.841872+00:00",
}

# The defect: no top-level stop, no forced_exit_reason, plan: null.
HPE_EXIT = {
    "ticker": "HPE", "action": "SELL_TO_CLOSE", "position_size_usd": None,
    "reference_price": 46.22, "proposal_id": "20260715-e2c95f",
    "sell_fraction": 1.0, "atr_pct": 5.97, "plan": None, "demand_driver": None,
    "order_id": "SIM-d02ec3368c", "status": "filled", "fill_price": 46.082,
    "quantity": 1.63771, "avg_cost": 48.8488,
    "position_opened_at": "2026-07-10T19:14:48.307774+00:00",
    "entry_plan": {"stop_loss": 43.75, "target_price": 58.0,
                   "holding_horizon_days": 40, "entry_price_max": 49.75,
                   "confidence": 0.64},
    "realized_pnl_usd": -4.53,
    "fees_usd": {"commission": 0.0, "sec_fee": 0.0021, "taf": 0.00027,
                 "slippage_bps": 10.0, "effective_slippage_pct": 0.2985},
    "filled_at": "2026-07-15T16:32:49.853725+00:00",
}

HPE_AUTOPSY = {
    "ts": "2026-07-15T16:32:49.854049+00:00", "ticker": "HPE",
    "action": "SELL_TO_CLOSE", "fill_price": 46.082, "quantity": 1.63771,
    "days_held": 4, "forced": False, "forced_reason": None,
    "entry_plan": HPE_EXIT["entry_plan"], "realized_pnl_usd": -4.53,
    "avg_cost": 48.8488, "filled_at": "2026-07-15T16:32:49.853725+00:00",
    "brain_exit_thesis": ("Closing ahead of the stop rather than waiting for it. "
                          "HPE shares DELL's exact hyperscaler_server_capex driver"),
    "brain_grade": {"process": "process_fail",
                    "tags": ["discretionary_exit", "discretionary_loss"]},
}

MODEL = {"model_stop_gaps": True, "stop_gap_atr_fraction": 0.5,
         "slippage_bps": 10.0, "slippage_atr_fraction": 0.05}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated runner/grade store. conftest's _no_writes_to_real_learning_stores
    guard makes this mandatory, and the two incidents it cites are why."""
    f = tmp_path / "post_exit_runners.json"
    monkeypatch.setattr(PR, "RUNNERS_FILE", f)
    monkeypatch.setattr(PR, "ROOT", tmp_path)
    import tools.concept_memory as CM
    monkeypatch.setattr(CM, "MEM_DIR", tmp_path / "concept_memory")
    monkeypatch.setattr(CM, "ROOT", tmp_path)
    return f


def _write_ledger(root: Path, rows: list) -> Path:
    p = root / "state" / "portfolio.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cash_usd": 1000.0, "positions": [],
                             "pending_orders": {}, "history": rows}))
    return p


# --------------------------------------------------------------------------- #
# 1. The DELL stop gap — the measurement autonomy_config assumed and never took
# --------------------------------------------------------------------------- #
def test_dell_stop_gap_is_measured_in_dollars_percent_and_atr():
    g = EG.grade_exit(DELL_EXIT, model=MODEL)
    s = g["stop_slippage"]
    assert s["applicable"] is True
    assert s["stop_level"] == 408.0 and s["fill_price"] == 389.7542
    assert s["gap_usd_per_share"] == pytest.approx(18.2458, abs=1e-4)
    assert s["gap_pct_of_stop"] == pytest.approx(4.4720, abs=1e-3)
    # ATR is applied to the reference price, the same base _sell_fill_price uses:
    # 8.3% of 400.52 = 33.243/share -> 18.2458 / 33.243 = 0.549 ATR.
    assert s["atr_usd"] == pytest.approx(33.2432, abs=1e-3)
    assert s["gap_in_atr"] == pytest.approx(0.549, abs=1e-3)
    assert s["modelled_gap_atr_fraction"] == 0.5
    # Total position cost of the gap, not just per share.
    assert s["gap_usd_total"] == pytest.approx(18.2458 * 0.22189, abs=1e-3)


def test_a_simulated_gap_is_labelled_modelled_and_never_counts_as_evidence():
    """The fill came OUT of the gap model, so grading it against the model is
    arithmetic. Conflating that with market evidence is the 2026-07-16 failure
    class: a loop reporting healthy while studying fiction."""
    g = EG.grade_exit(DELL_EXIT, model=MODEL)
    s = g["stop_slippage"]
    assert s["slippage_source"] == "modelled"
    assert "disclosure" in s and "produced by" in s["disclosure"].lower()
    summ = EG.summarize_exit_grades([g])
    assert summ["n_stop_gaps_modelled"] == 1
    assert summ["n_stop_gaps_realized"] == 0
    assert summ["mean_gap_in_atr_realized"] is None   # never averaged together


def test_a_real_broker_gap_worse_than_the_model_is_flagged():
    real = {**DELL_EXIT, "gap_modeled": False, "fill_price": 370.0,
            "order_id": "ALPACA-1"}
    g = EG.grade_exit(real, model=MODEL)
    s = g["stop_slippage"]
    assert s["slippage_source"] == "realized"
    assert s["verdict"] == "gap_worse_than_model"
    assert s["excess_vs_model_ratio"] > EG.GAP_MODEL_TOLERANCE
    assert "heat is" in s["note"] and "stop_gap_atr_fraction" in s["note"]
    summ = EG.summarize_exit_grades([g])
    assert summ["n_worse_than_model"] == 1 and summ["n_stop_gaps_realized"] == 1


def test_a_fill_at_or_above_the_stop_says_the_stop_held():
    g = EG.grade_exit({**DELL_EXIT, "fill_price": 408.5, "gap_modeled": False},
                      model=MODEL)
    s = g["stop_slippage"]
    assert s["verdict"] == "filled_at_or_above_stop"
    assert s["gap_usd_per_share"] < 0


def test_unknown_atr_is_unverified_not_within_model():
    """THE BUG SHAPE THIS AVOIDS. exit_autopsy graded every stopped-out loss a
    process_win because the only escape was having no stop at all — an unrunnable
    check resolving in the graded party's favour. An unmeasurable gap must not
    resolve to 'within model'."""
    g = EG.grade_exit({**DELL_EXIT, "atr_pct": None}, model=MODEL)
    s = g["stop_slippage"]
    assert s["verdict"] == "gap_unmodelled"
    assert s["gap_in_atr"] is None
    assert "NOT within-model" in s["note"]
    assert "atr_pct" in g["missing"]


def test_a_trailing_stop_exit_is_graded_against_the_level_that_actually_fired():
    """Grading a trail exit against the plan stop would report a gap that never
    happened — the trail is the line the position was actually protected at."""
    trail = {**DELL_EXIT, "forced_exit_reason": "trailing_stop_breached",
             "stop_loss": 470.0, "trailing_stop": 470.0, "fill_price": 462.0,
             "gap_modeled": False, "reference_price": 468.0}
    g = EG.grade_exit(trail, model=MODEL)
    assert g["exit_reason_kind"] == "forced_trailing_stop"
    assert g["binding_stop_loss"] == 470.0
    assert g["plan_stop_loss"] == 408.0          # both are kept
    assert g["stop_slippage"]["stop_level"] == 470.0


# --------------------------------------------------------------------------- #
# 2. Realized vs claimed R — grading the number that cleared the 2:1 gate
# --------------------------------------------------------------------------- #
def test_dell_realized_r_against_the_claim_that_cleared_the_gate():
    g = EG.grade_exit(DELL_EXIT, model=MODEL)
    r = g["r_grade"]
    assert r["gradeable"] is True
    assert r["risk_unit_usd"] == pytest.approx(42.6702, abs=1e-3)
    assert r["claimed_rr"] == pytest.approx(1.74, abs=0.01)
    assert r["realized_r"] == pytest.approx(-1.43, abs=0.01)
    assert r["r_gap"] == pytest.approx(3.17, abs=0.02)


def test_hpe_discretionary_exit_is_gradeable_and_names_its_reason():
    """THE DEFECT: this row has no top-level stop and no forced_exit_reason. The
    narrative was never gone — it sat in the autopsy as brain_exit_thesis — but
    nothing joined the two, so the close read as unattributable."""
    g = EG.grade_exit(HPE_EXIT, autopsy=HPE_AUTOPSY, model=MODEL)
    assert g["exit_reason_kind"] == "discretionary"
    assert g["forced"] is False
    assert "Closing ahead of the stop" in (g["exit_reason"] or "")
    assert g["plan_stop_loss"] == 43.75
    assert g["entry_plan"]["target_price"] == 58.0
    assert g["horizon_days"] == 40
    assert g["plan_source"] == "fill_entry_plan"
    r = g["r_grade"]
    assert r["claimed_rr"] == pytest.approx(1.79, abs=0.01)
    assert r["realized_r"] == pytest.approx(-0.54, abs=0.01)
    # Closed at just over half the loss it had underwritten — a defensible
    # decision, and a measurable habit that could not previously be stated.
    assert r["risk_used_fraction"] == pytest.approx(0.543, abs=0.005)
    assert g["stop_slippage"]["applicable"] is False


def test_the_grade_line_says_the_whole_thing_in_one_sentence():
    line = EG.grade_exit(DELL_EXIT, model=MODEL)["headline"]
    assert "DELL" in line and "forced_stop" in line
    assert "-1.43R" in line and "+1.74R" in line
    assert "408.0" in line and "389.7542" in line and "ATR" in line
    assert len(line) <= 400


# --------------------------------------------------------------------------- #
# 3. Old and broken records stay tolerable
# --------------------------------------------------------------------------- #
def test_a_record_with_no_plan_is_ungradeable_out_loud_not_silently():
    g = EG.grade_exit({"ticker": "OLD", "action": "SELL_TO_CLOSE",
                       "fill_price": 10.0, "quantity": 1.0, "order_id": "X",
                       "filled_at": "2026-01-01T00:00:00+00:00"}, model=MODEL)
    assert g["r_grade"]["gradeable"] is False
    assert "missing_stop" in g["r_grade"]["ungradeable_reason"]
    assert "plan_stop_loss" in g["missing"] and "plan_target_price" in g["missing"]
    assert "UNGRADEABLE" in g["headline"]


def test_an_unclassifiable_exit_says_unknown_rather_than_defaulting():
    """Defaulting to `discretionary` would quietly move every forced exit whose
    reason was lost into the bucket that grades hardest."""
    out = EG.classify_exit_reason({"ticker": "X", "fill_price": 1.0}, None)
    assert out["exit_reason_kind"] == "unknown"
    assert out["forced"] is False and out["is_stop_exit"] is False


def test_grade_exit_never_raises_on_garbage():
    for bad in (None, {}, {"ticker": None}, {"fill_price": "abc", "avg_cost": []},
                {"entry_plan": "not-a-dict", "ticker": "Z"}):
        g = EG.grade_exit(bad, model=MODEL)
        assert isinstance(g, dict) and "exit_key" in g


def test_a_stop_at_or_above_entry_is_refused_not_reported_as_a_huge_r():
    g = EG.grade_exit({**DELL_EXIT,
                       "entry_plan": {**DELL_EXIT["entry_plan"], "stop_loss": 999.0}},
                      model=MODEL)
    assert g["r_grade"]["gradeable"] is False
    assert "non_positive_risk_unit" in g["r_grade"]["ungradeable_reason"]


def test_exit_key_is_stable_and_distinguishes_partials():
    assert EG.exit_key(DELL_EXIT) == EG.exit_key(dict(DELL_EXIT))
    assert EG.exit_key(DELL_EXIT) != EG.exit_key(HPE_EXIT)
    half = {**DELL_EXIT, "order_id": "SIM-second-half", "sell_fraction": 0.5}
    assert EG.exit_key(half) != EG.exit_key(DELL_EXIT)


def test_gap_model_is_read_from_the_live_config_not_hard_coded():
    """The number graded against must be the number the simulator and book_risk
    actually use; a private copy here would drift and grade the wrong assumption."""
    m = EG.load_gap_model()
    assert m["stop_gap_atr_fraction"] is not None
    injected = EG.load_gap_model({"execution_costs": {"stop_gap_atr_fraction": 0.9}})
    assert injected["stop_gap_atr_fraction"] == 0.9


# --------------------------------------------------------------------------- #
# 4. THE REGRESSION — a study that could only lose observations
# --------------------------------------------------------------------------- #
def test_an_empty_store_recovers_every_closed_exit_from_the_ledger(store, tmp_path):
    """This is the nineteen-day bug, reproduced and fixed: an empty store against
    two real closes must not stay empty."""
    _write_ledger(tmp_path, [DELL_EXIT, HPE_EXIT])
    assert PR._load()["tracking"] == []

    out = PR.backfill_from_ledger(fetch_bars=False)

    assert out["seeded"] == 2
    tracked = {t["ticker"] for t in PR._load()["tracking"]}
    assert tracked == {"DELL", "HPE"}


def test_backfill_is_idempotent(store, tmp_path):
    _write_ledger(tmp_path, [DELL_EXIT, HPE_EXIT])
    PR.backfill_from_ledger(fetch_bars=False)
    again = PR.backfill_from_ledger(fetch_bars=False)
    assert again["seeded"] == 0 and again["already_tracked"] == 2
    assert len(PR._load()["tracking"]) == 2


def test_backfill_takes_realized_pnl_from_the_LEDGER_not_the_autopsy(store, tmp_path):
    """The original DELL seed carried -135.19 against a real -13.52 and would have
    failed reconciles_with_ledger on its next mark. Sourcing the number from the
    thing it is reconciled against makes that impossible."""
    _write_ledger(tmp_path, [DELL_EXIT])
    wrong = {**HPE_AUTOPSY, "ticker": "DELL", "fill_price": 389.7542,
             "realized_pnl_usd": -135.19,
             "filled_at": DELL_EXIT["filled_at"]}
    PR.backfill_from_ledger(fetch_bars=False, autopsies=[wrong])
    rec = PR._load()["tracking"][0]
    assert rec["realized_pnl_usd"] == -13.52
    assert PR.reconciles_with_ledger(rec, closed_trades=[DELL_EXIT]) is True


def test_a_completed_track_is_not_reseeded(store, tmp_path):
    _write_ledger(tmp_path, [DELL_EXIT])
    PR._save({"tracking": [], "completed": [
        {"ticker": "DELL", "exit_date": "2026-07-15", "exit_price": 389.7542,
         "status": "completed"}]})
    out = PR.backfill_from_ledger(fetch_bars=False)
    assert out["seeded"] == 0
    assert PR._load()["tracking"] == []


def test_a_quarantined_record_is_never_resurrected_by_the_backfill(store, tmp_path):
    """Quarantine is the loop that REMOVES fictions. A recovery loop that puts
    them back would make the 2026-07-16 incident unfixable."""
    _write_ledger(tmp_path, [DELL_EXIT])
    PR._save({"tracking": [], "completed": [], "quarantined": [
        {"ticker": "DELL", "exit_date": "2026-07-15", "unreconciled": "fiction"}]})
    out = PR.backfill_from_ledger(fetch_bars=False)
    assert out["seeded"] == 0
    assert PR._load()["tracking"] == []


def test_backfill_ignores_unfilled_and_non_exit_rows(store, tmp_path):
    _write_ledger(tmp_path, [
        {"ticker": "UNH", "action": "BUY", "status": "rejected_insufficient_cash"},
        {"ticker": "BKR", "action": "BUY", "status": "filled", "fill_price": 58.2},
        {"ticker": "X", "action": "SELL_TO_CLOSE", "status": "pending",
         "fill_price": 1.0},
        DELL_EXIT,
    ])
    out = PR.backfill_from_ledger(fetch_bars=False)
    assert out["seeded"] == 1
    assert PR._load()["tracking"][0]["ticker"] == "DELL"


def test_backfill_survives_a_missing_or_corrupt_ledger(store, tmp_path):
    assert PR.backfill_from_ledger(fetch_bars=False)["seeded"] == 0
    p = tmp_path / "state" / "portfolio.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert PR.ledger_exits() == []
    assert PR.backfill_from_ledger(fetch_bars=False)["seeded"] == 0


# --------------------------------------------------------------------------- #
# 5. Marks — reconstructed from real bars, or admitted as missed. Never invented.
# --------------------------------------------------------------------------- #
def _bars(start_day: str, n: int, close: float, step: float = 0.0) -> dict:
    from datetime import date, timedelta
    d0 = date.fromisoformat(start_day)
    out = {}
    for i in range(n):
        px = close + step * i
        out[(d0 + timedelta(days=i)).isoformat()] = {
            "high": round(px * 1.02, 4), "low": round(px * 0.98, 4),
            "close": round(px, 4)}
    return out


def test_a_late_recovered_track_replays_its_marks_from_real_bars(store, tmp_path):
    """A track recovered nineteen days after the exit cannot honestly mark its
    15-day window with today's close. Daily bars make the window observable for
    real; the mark is tagged so nothing mistakes it for a live one."""
    from datetime import date, timedelta
    exit_day = (date.today() - timedelta(days=20)).isoformat()
    row = {**DELL_EXIT, "filled_at": f"{exit_day}T16:00:00+00:00"}
    _write_ledger(tmp_path, [row])
    bars = _bars(exit_day, 21, 389.7542, step=2.0)

    PR.backfill_from_ledger(fetch_bars=False, bars_by_ticker={"DELL": bars})

    rec = PR._load()["tracking"][0]
    m15 = rec["marks"]["15"]
    assert m15["mark_source"] == "daily_bars_backfill"
    assert m15["mark_date"] == (date.fromisoformat(exit_day)
                                + timedelta(days=15)).isoformat()
    assert m15["leftover_pct"] > 0
    assert "30" not in rec["marks"] and "60" not in rec["marks"]  # not due yet
    assert rec["high_after_exit"] > rec["exit_price"]
    assert rec["bars_backfilled"] is True


def test_a_window_that_closed_before_the_track_existed_is_MISSED_not_invented(store, tmp_path):
    """Fabricating an observation is worse than admitting a hole: every consumer
    treats a mark and a real mark identically, which is exactly how the
    fabricated HPE record fed the loop for three days."""
    from datetime import date, timedelta
    exit_day = (date.today() - timedelta(days=25)).isoformat()
    _write_ledger(tmp_path, [{**DELL_EXIT, "filled_at": f"{exit_day}T16:00:00+00:00"}])
    PR.backfill_from_ledger(fetch_bars=False)          # no bars available

    PR.mark_post_exit_runners({"DELL": 430.0}, cache_news_on_marks=False,
                              backfill=False)

    rec = PR._load()["tracking"][0]
    m15 = rec["marks"]["15"]
    assert m15.get("status") == "missed"
    assert "leftover_pct" not in m15
    assert "closed" in m15["reason"]
    # ...and the record still tracks forward normally from here.
    assert rec["current_leftover_pct"] == pytest.approx(
        (430.0 / 389.7542 - 1) * 100, abs=0.1)


def test_a_missed_mark_never_reaches_the_scorer_as_a_number(store):
    scored = PR._score_runner({
        "ticker": "DELL", "exit_price": 100.0, "high_after_exit": 101.0,
        "low_after_exit": 99.0, "track_kind": "winner", "gain_at_exit_pct": 5.0,
        "marks": {"15": {"window_days": 15, "status": "missed"}},
    })
    assert scored["latest_leftover_pct"] is None
    assert scored["runner_verdict"] == "mixed"


def test_a_window_still_inside_the_grace_is_marked_normally(store, tmp_path):
    from datetime import date, timedelta
    exit_day = (date.today() - timedelta(days=16)).isoformat()
    _write_ledger(tmp_path, [{**DELL_EXIT, "filled_at": f"{exit_day}T16:00:00+00:00"}])
    PR.backfill_from_ledger(fetch_bars=False)
    PR.mark_post_exit_runners({"DELL": 430.0}, cache_news_on_marks=False,
                              backfill=False)
    m15 = PR._load()["tracking"][0]["marks"]["15"]
    assert m15.get("status") != "missed"
    assert m15["leftover_pct"] == pytest.approx((430.0 / 389.7542 - 1) * 100, abs=0.1)


# --------------------------------------------------------------------------- #
# 6. End to end — the mark cycle heals a wiped store
# --------------------------------------------------------------------------- #
def test_the_mark_cycle_recovers_a_wiped_store(store, tmp_path, monkeypatch):
    """commit f02af98 (2026-07-16) committed a stale copy of this file and the
    real DELL track vanished with it; commit 80f2e63 emptied it entirely on
    2026-07-19. Neither was survivable before, because the only seeding path ran
    at close time and both closes had already happened."""
    _write_ledger(tmp_path, [DELL_EXIT, HPE_EXIT])
    PR._save({"version": 1, "tracking": [], "completed": []})   # the wiped file
    monkeypatch.setattr(PR, "_fetch_bars", lambda *a, **k: {})

    out = PR.mark_post_exit_runners({"DELL": 430.0, "HPE": 50.0},
                                    cache_news_on_marks=False)

    assert out["backfill"]["seeded"] == 2
    state = PR._load()
    assert {t["ticker"] for t in state["tracking"]} == {"DELL", "HPE"}
    assert all(t.get("current_leftover_pct") is not None for t in state["tracking"])
    # ...and the exits got graded on the same pass.
    assert out["exit_grades"]["graded"] == 2


def test_study_health_names_the_exits_nobody_is_watching(store, tmp_path):
    """'Warming up' is what the runner block said for nineteen days while the
    store was empty. A thin study and a broken one must never read the same."""
    _write_ledger(tmp_path, [DELL_EXIT, HPE_EXIT])
    PR._save({"version": 1, "tracking": [], "completed": []})

    h = PR.study_health()
    assert h["healthy"] is False
    assert h["ledger_closed_exits"] == 2 and h["n_unseeded"] == 2
    assert {u["ticker"] for u in h["unseeded_exits"]} == {"DELL", "HPE"}
    assert "under-observing" in h["note"]

    face = PR.brain_facing_runner_learning()
    assert "WARNING" in face["note"]
    assert face["study_health"]["healthy"] is False

    PR.backfill_from_ledger(fetch_bars=False)
    h2 = PR.study_health()
    assert h2["healthy"] is True and h2["n_unseeded"] == 0
    assert "WARNING" not in PR.brain_facing_runner_learning()["note"]


# --------------------------------------------------------------------------- #
# 7. Grade persistence, the ledger sweep, and reaching the brain
# --------------------------------------------------------------------------- #
def test_grades_persist_and_dedupe_on_the_broker_order(store):
    g = EG.grade_exit(DELL_EXIT, model=MODEL)
    assert PR.record_exit_grade(g) is True
    assert PR.record_exit_grade(EG.grade_exit(DELL_EXIT, model=MODEL)) is True
    rows = PR.load_exit_grades()
    assert len(rows) == 1 and rows[0]["ticker"] == "DELL"
    assert PR.record_exit_grade({"no": "key"}) is False


def test_the_sweep_grades_every_exit_in_the_ledger_and_is_idempotent(store, tmp_path):
    """Runs off the LEDGER rather than a call site: forced exits are placed by
    exit_guard, discretionary ones by runlib/brain_io, queued ones completed by
    reconcile_runner. Wiring call sites leaves a fourth ungraded the day someone
    adds one."""
    _write_ledger(tmp_path, [DELL_EXIT, HPE_EXIT])
    out = PR.sweep_exit_grades(autopsies=[HPE_AUTOPSY])
    assert out["graded"] == 2
    assert PR.sweep_exit_grades(autopsies=[HPE_AUTOPSY])["graded"] == 0

    by_ticker = {g["ticker"]: g for g in PR.load_exit_grades()}
    assert by_ticker["DELL"]["exit_reason_kind"] == "forced_stop"
    assert by_ticker["HPE"]["exit_reason_kind"] == "discretionary"
    assert by_ticker["HPE"]["r_grade"]["realized_r"] == pytest.approx(-0.54, abs=0.01)


def test_the_grade_reaches_the_block_the_brain_actually_reads(store, tmp_path):
    """runlib/context_tiers.compact_learning_pack builds the brain's `runners`
    block from a hard-coded ALLOWLIST and drops everything else silently — the
    same class of bug as parse_proposals dropping seat_reviews. Both keys were
    registered there on 2026-08-03, replacing the digest that used to ride on
    `stats`, so the FULL blocks now reach the brain. A grade the brain cannot see
    is the failure this repairs, so assert the real path, not a proxy."""
    _write_ledger(tmp_path, [DELL_EXIT, HPE_EXIT])
    PR.sweep_exit_grades(autopsies=[HPE_AUTOPSY])
    PR.backfill_from_ledger(fetch_bars=False)

    face = PR.brain_facing_runner_learning()
    assert "exit_execution_grades" in face
    assert len(face["exit_execution_grades"]["recent"]) == 2
    assert face["study_health"]["healthy"] is True
    heads = face["exit_execution_grades"]["headlines"]
    assert any("DELL" in h and "through" in h for h in heads)
    assert face["exit_execution_grades"]["stats"]["n_r_gradeable"] == 2

    # The workaround is gone: stats no longer carries a shadow copy.
    assert "exit_execution" not in face["stats"]

    # Drive the REAL compactor rather than simulating its allowlist, so this test
    # fails if that list is ever trimmed again.
    from runlib.context_tiers import compact_learning_pack
    pack = compact_learning_pack({"runner_learning": face})
    runners = pack["runners"]
    assert runners["study_health"]["healthy"] is True
    assert runners["exit_execution_grades"]["headlines"], (
        "the exit grade must survive compact_learning_pack's allowlist")


def test_brain_facing_grades_survive_an_empty_store(store):
    out = PR.brain_facing_exit_grades()
    assert out["recent"] == [] and out["stats"]["n_exits"] == 0


# --------------------------------------------------------------------------- #
# 8. exit_guard — the plan is persisted, and grading can never block an exit
# --------------------------------------------------------------------------- #
@pytest.fixture
def book(tmp_path, monkeypatch, store):
    """Temp ledger + isolated autopsy/lesson stores for a live forced exit."""
    from execution import simulated_broker as SB
    import tools.exit_autopsy as EA
    state = tmp_path / "book.json"
    state.write_text(json.dumps({"cash_usd": 1000.0, "total_equity_usd": 1000.0,
                                 "positions": [], "pending_orders": {},
                                 "history": []}))
    monkeypatch.setattr(SB, "STATE_FILE", state)
    monkeypatch.setenv("EE_BROKER", "simulation")
    monkeypatch.setattr(EA, "AUTOPSY_DIR", tmp_path / "autopsies")
    monkeypatch.setattr(EA, "BINDING_FILE", tmp_path / "binding.json")
    return state


def _position(state_file: Path):
    st = json.loads(state_file.read_text())
    st["positions"].append({
        "ticker": "NVDA", "quantity": 10.0, "avg_cost": 100.0,
        "market_value_usd": 900.0, "high_water": 100.0,
        "opened_at": "2026-07-01T14:00:00+00:00",
        "plan": {"stop_loss": 90.0, "target_price": 130.0,
                 "holding_horizon_days": 30, "entry_price_max": 101.0,
                 "confidence": 0.7},
    })
    state_file.write_text(json.dumps(st))


def test_the_forced_exit_carries_the_plan_it_was_judged_against():
    """THE DEFECT: HPE's ledger row has stop_loss None and forced_exit_reason
    None, so nothing on it can say what it was measured against."""
    from execution import exit_guard
    portfolio = {"positions": [{
        "ticker": "NVDA", "quantity": 10.0, "avg_cost": 100.0, "days_held": 5,
        "original_plan": {"stop_loss": 90.0, "target_price": 130.0,
                          "holding_horizon_days": 30, "entry_price_max": 101.0,
                          "confidence": 0.7}}]}
    ex = exit_guard.check_forced_exits(portfolio, {"NVDA": 89.0}, {"NVDA": 4.0})[0]
    assert ex["plan_stop_loss"] == 90.0
    assert ex["plan_target_price"] == 130.0
    assert ex["plan_holding_horizon_days"] == 30
    assert ex["entry_plan_snapshot"]["confidence"] == 0.7
    assert ex["avg_cost"] == 100.0


def test_the_plan_lands_on_the_ledger_row_and_the_exit_is_graded(book, tmp_path):
    from execution import exit_guard
    _position(book)
    exits = [{"ticker": "NVDA", "reason": "stop_loss_breached", "last_price": 89.0,
              "stop_loss": 90.0, "plan_stop_loss": 90.0, "plan_target_price": 130.0,
              "plan_holding_horizon_days": 30, "trailing_stop": None,
              "atr_pct": 4.0, "days_held": 5, "horizon": 30,
              "entry_plan_snapshot": {"stop_loss": 90.0, "target_price": 130.0,
                                      "holding_horizon_days": 30,
                                      "entry_price_max": 101.0, "confidence": 0.7}}]

    fills = exit_guard.execute_forced_exits(exits, {"NVDA": 89.0}, "run-1")

    assert len(fills) == 1
    row = json.loads(book.read_text())["history"][-1]
    assert row["forced_exit_reason"] == "stop_loss_breached"
    assert row["plan_stop_loss"] == 90.0 and row["plan_target_price"] == 130.0
    assert row["plan_holding_horizon_days"] == 30

    g = PR.load_exit_grades()[0]
    assert g["ticker"] == "NVDA" and g["exit_reason_kind"] == "forced_stop"
    assert g["r_grade"]["claimed_rr"] == pytest.approx(3.0, abs=0.01)
    assert g["stop_slippage"]["applicable"] is True
    assert g["stop_slippage"]["gap_usd_per_share"] > 0     # gap-through modelled


def test_a_grading_failure_can_never_block_an_exit(book, monkeypatch, capsys):
    """EXITS REDUCE RISK AND ARE SACRED. Measurement does not get to interfere."""
    from execution import exit_guard
    import tools.exit_grade as EGmod

    def boom(*_a, **_k):
        raise RuntimeError("grader exploded")

    monkeypatch.setattr(EGmod, "grade_exit", boom)
    _position(book)
    exits = [{"ticker": "NVDA", "reason": "stop_loss_breached", "last_price": 89.0,
              "stop_loss": 90.0, "atr_pct": 4.0, "days_held": 5}]

    fills = exit_guard.execute_forced_exits(exits, {"NVDA": 89.0}, "run-2")

    assert len(fills) == 1 and fills[0]["status"] == "filled"
    assert json.loads(book.read_text())["positions"] == []   # position IS closed
    assert "exit grade skipped" in capsys.readouterr().out


def test_an_autopsy_failure_can_never_block_an_exit(book, monkeypatch):
    from execution import exit_guard
    import tools.exit_autopsy as EA
    monkeypatch.setattr(EA, "grade_and_persist_autopsy",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    _position(book)
    fills = exit_guard.execute_forced_exits(
        [{"ticker": "NVDA", "reason": "horizon_expired", "last_price": 105.0,
          "stop_loss": None, "atr_pct": 4.0, "days_held": 31}],
        {"NVDA": 105.0}, "run-3")
    assert len(fills) == 1
    # The exit is still graded even though the autopsy blew up.
    assert PR.load_exit_grades()[0]["exit_reason_kind"] == "forced_horizon"
