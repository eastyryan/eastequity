"""The learning layer must produce evidence, and must refuse to fake it.

Four dead or dishonest loops are pinned here, each with the live symptom it had.

1. KNOWLEDGE BASE had no store. data/knowledge_base.json did not exist while its
   derived view dashboard/data/learning_journal.json did and carried lesson
   KB-2388376284. Every grading API is fail-soft, so record_citations and
   link_lessons_to_trade iterated an empty list and returned 0 — silently,
   forever. The lesson could be cited and never graded.

2. ADOPT PIPELINE emitted nothing, ever, with 26 improvement notes on disk and 6
   of them auto-adoptable. Proposal ids came from `abs(hash(fp))`, which Python
   salts per process, so the `adopted_ids` dedupe could never match across runs.

3. SHADOW BOOK could not close. The gate demanded `age >= 30` before ANY close
   even when the stop was already touched, and the touch flags themselves came
   from SAMPLED last prices rather than daily bars, so they under-detected.
   Live state was 49 open / 0 closed; `binding` needs 8.

4. PERFORMANCE BREAKDOWN had no sample-size floor and published
   win_rate_pct: 0.0 off two trades to a public dashboard, into a gate
   (calibration_gate) with the power to block trades.

Plus the new capability: target attainment. The 2:1 risk_reward_ratio gate is
computed from target_price, which exit_guard never reads — so the claim was
unfalsifiable. It is now graded, and at n=2 it must bind NOTHING.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.benchmark as bm  # noqa: E402
import tools.calibration_gate as cg  # noqa: E402
import tools.exit_autopsy as ea  # noqa: E402
import tools.knowledge_base as kb  # noqa: E402
import tools.learning_adopt as la  # noqa: E402
import tools.performance_breakdown as pb  # noqa: E402
import tools.shadow_portfolio as sp  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Knowledge base: bootstrap + self-heal
# --------------------------------------------------------------------------- #
@pytest.fixture
def kb_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "KB_JSON", tmp_path / "knowledge_base.json")
    monkeypatch.setattr(kb, "KB_MD", tmp_path / "knowledge_base.md")
    monkeypatch.setattr(kb, "JOURNAL_JSON", tmp_path / "learning_journal.json")
    return tmp_path


def _publish(tmp_path, *ids):
    (tmp_path / "learning_journal.json").write_text(json.dumps({
        "updated_at": "2026-07-17T05:18:15+00:00",
        "entries": [{"id": i, "discipline": "technical_analysis",
                     "topic": f"topic {i}", "summary": "s" * 100,
                     "how_to_apply": "a" * 80, "learned_at": "2026-07-17"}
                    for i in ids],
    }))


def test_ensure_store_rebuilds_the_lost_store_from_the_published_journal(kb_paths):
    """THE incident: the store vanished with the runner; the journal survived."""
    _publish(kb_paths, "KB-2388376284")
    assert not kb.KB_JSON.exists()

    out = kb.ensure_store()
    assert out["status"] == "rebuilt_from_published"
    assert out["ids"] == ["KB-2388376284"]
    assert kb.KB_JSON.exists()
    assert kb.store_health()["ok"] is True


def test_ensure_store_is_idempotent(kb_paths):
    _publish(kb_paths, "KB-1111111111")
    kb.ensure_store()
    again = kb.ensure_store()
    assert again["status"] == "present" and again["n_entries"] == 1


def test_ensure_store_does_not_invent_a_store_from_nothing(kb_paths):
    """No published lessons is a quiet curriculum, not a loss — and must not be
    reported as a rebuild."""
    assert kb.ensure_store()["status"] == "empty"


def test_citations_self_heal_instead_of_returning_a_silent_zero(kb_paths):
    """record_citations returned 0 against a missing store. 0 is also what it
    returns for "no lessons matched", which is how this stayed invisible."""
    _publish(kb_paths, "KB-2388376284")
    assert kb.record_citations("per KB-2388376284, waiting for the retest") == 1
    stored = json.loads(kb.KB_JSON.read_text())["entries"][0]
    assert stored["times_cited"] == 1


def test_links_survive_a_lost_store_so_lessons_can_still_be_graded(kb_paths):
    _publish(kb_paths, "KB-2388376284")
    assert kb.link_lessons_to_trade("UNH", ["KB-2388376284"], "run1",
                                    linked_on="2026-07-17") == 1
    stored = json.loads(kb.KB_JSON.read_text())["entries"][0]
    assert stored["linked_trades"][0]["ticker"] == "UNH"


def test_store_health_diagnoses_without_healing(kb_paths):
    """A health check that repairs what it measures reports a permanent green
    over a subsystem that dies after every deploy."""
    _publish(kb_paths, "KB-2388376284")
    h = kb.store_health()
    assert h["ok"] is False
    assert h["status"] == "store_missing_but_lessons_published"
    assert h["recoverable"] is True
    assert not kb.KB_JSON.exists(), "store_health must not write the store"


def test_lesson_ids_are_deterministic_not_process_salted(kb_paths):
    """The id is the CITATION KEY the whole grading loop joins on; hash() is
    salted per process."""
    _publish(kb_paths)
    lesson = {"topic": "volume dry-up retests", "discipline": "technical_analysis",
              "summary": "s" * 120, "how_to_apply": "a" * 80}
    first = kb.add_lesson(lesson)["id"]
    # Wipe BOTH the store and the published view — a total loss, the way an
    # ephemeral runner loses them.
    kb.KB_JSON.unlink()
    kb.JOURNAL_JSON.unlink()
    second = kb.add_lesson(lesson)["id"]
    assert first == second, "the citation key must be a function of the content"


# --------------------------------------------------------------------------- #
# 2. Adopt pipeline
# --------------------------------------------------------------------------- #
@pytest.fixture
def adopt_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "PROPOSALS_FILE", tmp_path / "learning_proposals.json")
    monkeypatch.setattr(la, "ADOPTED_FILE", tmp_path / "adopted_lessons.md")
    monkeypatch.setattr(la, "ADOPTED_JSON", tmp_path / "adopted_lessons.json")
    return tmp_path


NOTE = ("The watchlist promote loop needs an explicit one-line reason per name; "
        "this run I held four names without saying why not BUY today.")


def test_proposal_ids_are_stable_across_processes(adopt_paths):
    """`abs(hash(fp))` is salted per process, so `adopted_ids` never matched and
    every Sunday would have re-adopted the entire backlog under fresh ids."""
    assert la._proposal_id("a note fingerprint") == la._proposal_id("a note fingerprint")
    assert la._proposal_id("a") != la._proposal_id("b")
    assert la._proposal_id("x").startswith("LP-")


def test_the_pipeline_adopts_once_and_only_once(adopt_paths):
    props = la.propose_adoptions([{"note": NOTE, "run_id": "r1"}])
    assert la.auto_adopt_soft(props)["auto_adopted"] == 1
    # Second week, same note still in the journal.
    props2 = la.propose_adoptions([{"note": NOTE, "run_id": "r1"}])
    assert la.auto_adopt_soft(props2)["auto_adopted"] == 0
    lessons = json.loads(la.ADOPTED_JSON.read_text())["lessons"]
    assert len(lessons) == 1


def test_pipeline_health_flags_adoptable_notes_that_were_never_adopted(adopt_paths, monkeypatch):
    """The live symptom: 6 auto-adoptable notes on disk, no adopted_lessons.json,
    and brain_facing_adopted_lessons() returning [] — indistinguishable from
    "nothing was worth adopting"."""
    monkeypatch.setattr(la, "harvest_improvement_notes",
                        lambda limit=40: [{"note": NOTE, "run_id": "r1"}])
    h = la.pipeline_health()
    assert h["ok"] is False
    assert h["status"] == "never_ran_with_adoptable_notes"
    assert h["n_pending_soft"] == 1

    la.run_weekly_adopt_pipeline()
    assert la.pipeline_health()["ok"] is True


def test_hard_proposals_stay_parked_for_a_human(adopt_paths):
    """DELIBERATE: an LLM must not rewrite its own risk limits. A note asking for
    a validator change is never auto-adoptable."""
    hard = "The validator should reject proposals whose stop sits inside the noise band."
    props = la.propose_adoptions([{"note": hard, "run_id": "r1"}])
    assert props[0]["auto_adoptable"] is False
    assert la.auto_adopt_soft(props)["hard_pending"] == 1


def test_the_pipeline_returns_its_errors_instead_of_raising(adopt_paths, monkeypatch):
    """runlib/reviews.py wraps this call in a bare except whose only output is a
    print, and stdout is not captured on the cloud runner — a throw here leaves
    no record anywhere. That is why the 2026-07-17 failure was undiagnosable."""
    monkeypatch.setattr(la, "harvest_improvement_notes",
                        lambda limit=40: (_ for _ in ()).throw(RuntimeError("boom")))
    out = la.run_weekly_adopt_pipeline()
    assert out["status"] == "error" and "boom" in out["reason"]


# --------------------------------------------------------------------------- #
# 3. Shadow book: first touch resolves, on real bars
# --------------------------------------------------------------------------- #
@pytest.fixture
def shadow(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "SHADOW_FILE", tmp_path / "shadow.json")
    return tmp_path / "shadow.json"


def _age(shadow_file, days):
    """Backdate every open shadow by `days`."""
    from datetime import datetime, timedelta, timezone
    state = json.loads(shadow_file.read_text())
    old = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    for p in state["positions"]:
        p["opened_at"] = old
    shadow_file.write_text(json.dumps(state))


def test_a_touched_stop_closes_the_shadow_immediately(shadow, monkeypatch):
    """THE bug: `age >= HORIZON_DAYS or age >= 30` gated EVERY close, so a shadow
    that gapped through its stop on day two sat open for a month. With binding at
    8 closed shadows the loop produced nothing for 30 days after every restart."""
    monkeypatch.setattr(sp, "_fetch_bars_for", lambda *a, **k: {})
    sp.open_shadow(ticker="AMD", entry_price=100.0, source="rejected_idea", reason="r")
    _age(shadow, 3)

    out = sp.mark_shadows({"AMD": 85.0})  # stop is 12% -> 88.0
    assert out["closed_now"] == 1
    closed = json.loads(shadow.read_text())["closed"][0]
    assert closed["verdict"] == "good_skip"
    assert closed["close_trigger"] == "level_touched"
    assert closed["age_days_at_close"] == 3


def test_a_touched_target_closes_the_shadow_immediately(shadow, monkeypatch):
    monkeypatch.setattr(sp, "_fetch_bars_for", lambda *a, **k: {})
    sp.open_shadow(ticker="NVDA", entry_price=100.0, source="rejected_idea", reason="r")
    _age(shadow, 2)
    sp.mark_shadows({"NVDA": 112.0})  # target is +10% -> 110.0
    closed = json.loads(shadow.read_text())["closed"][0]
    assert closed["verdict"] == "regret_miss"


def test_an_untouched_shadow_stays_open_until_the_horizon(shadow, monkeypatch):
    monkeypatch.setattr(sp, "_fetch_bars_for", lambda *a, **k: {})
    sp.open_shadow(ticker="ANET", entry_price=100.0, source="rejected_idea", reason="r")
    _age(shadow, 40)
    assert sp.mark_shadows({"ANET": 103.0})["closed_now"] == 0

    _age(shadow, sp.HORIZON_DAYS)
    out = sp.mark_shadows({"ANET": 103.0})
    assert out["closed_now"] == 1
    assert json.loads(shadow.read_text())["closed"][0]["close_trigger"] == "horizon_expired"


def test_daily_bars_catch_the_touch_that_sampled_marks_missed(shadow, monkeypatch):
    """high_water/low_water accumulate from whatever price happened to be passed
    at mark time. Sampling can only MISS an excursion, never invent one, so the
    old flags were a systematic under-count of touches."""
    monkeypatch.setattr(sp, "_fetch_bars_for", lambda *a, **k: {})
    sp.open_shadow(ticker="MU", entry_price=100.0, source="rejected_idea", reason="r")
    _age(shadow, 5)

    # Sampled marks alone see 100 -> 96: no touch of the 88.0 stop.
    assert sp.mark_shadows({"MU": 96.0})["closed_now"] == 0

    # The real tape printed an 85 low between our two samples.
    from datetime import datetime, timedelta, timezone
    day = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    bars = {"MU": {day: {"high": 99.0, "low": 85.0, "close": 96.0}}}
    out = sp.mark_shadows({"MU": 96.0}, bars=bars)
    assert out["closed_now"] == 1
    closed = json.loads(shadow.read_text())["closed"][0]
    assert closed["would_hit_stop"] is True
    assert closed["touch_source"] == "daily_bars"
    assert closed["verdict"] == "good_skip"


def test_touch_source_discloses_when_a_verdict_was_never_bar_verified(shadow, monkeypatch):
    """None and False are different claims. A shadow resolved on sampled marks
    alone must say so, or an unverified good_skip reads as evidence."""
    monkeypatch.setattr(sp, "_fetch_bars_for", lambda *a, **k: {})
    sp.open_shadow(ticker="HPE", entry_price=100.0, source="rejected_idea", reason="r")
    _age(shadow, 5)
    sp.mark_shadows({"HPE": 80.0})
    assert json.loads(shadow.read_text())["closed"][0]["touch_source"] == "sampled_only"


def test_a_same_day_shadow_never_triggers_a_bar_download(shadow):
    """Day zero has one bar — the one the entry price came from. Fetching for it
    would download history for every idea skipped this morning."""
    sp.open_shadow(ticker="AMD", entry_price=100.0, source="rejected_idea", reason="r")
    positions = json.loads(shadow.read_text())["positions"]
    assert sp._needs_bar_check(positions[0], 0) is False
    assert sp._needs_bar_check(positions[0], 1) is True


def test_a_sampled_touch_is_proof_and_needs_no_bars(shadow):
    """The asymmetry that keeps this cheap: sampled True is proof, sampled False
    is merely unproven."""
    sp.open_shadow(ticker="AMD", entry_price=100.0, source="rejected_idea", reason="r")
    p = json.loads(shadow.read_text())["positions"][0]
    p["low_water"] = 80.0
    assert sp._needs_bar_check(p, 30) is False


# --- beta attribution ------------------------------------------------------- #
def test_a_good_skip_in_a_market_wide_drawdown_is_charged_to_the_tape():
    """~38 open shadows showed would_hit_stop true from one market-wide drawdown.
    Resolved naively they read as 38 pieces of evidence that the bar is set right."""
    p = {"verdict": "good_skip", "max_drawdown_pct": -12.0,
         "benchmark_return_pct": -9.0}
    assert sp._verdict_attribution(p) == "market_wide"


def test_a_selloff_the_benchmark_does_not_explain_stays_idiosyncratic():
    """SPY -1.5% while the name fell 17% explains almost nothing — the live case."""
    p = {"verdict": "good_skip", "max_drawdown_pct": -17.0,
         "benchmark_return_pct": -1.5}
    assert sp._verdict_attribution(p) == "idiosyncratic"


def test_a_quiet_tape_cannot_explain_anything_however_good_the_ratio():
    """0.3% of a 0.5% excursion is a ratio, not an explanation."""
    p = {"verdict": "good_skip", "max_drawdown_pct": -0.5,
         "benchmark_return_pct": -0.4}
    assert sp._verdict_attribution(p) == "idiosyncratic"


def test_attribution_is_unknown_without_a_benchmark_window():
    assert sp._verdict_attribution(
        {"verdict": "good_skip", "max_drawdown_pct": -12.0,
         "benchmark_return_pct": None}) == "unknown"


def test_stats_publish_the_beta_stripped_rates_beside_the_raw_ones():
    closed = [
        {"verdict": "good_skip", "verdict_attribution": "market_wide"},
        {"verdict": "good_skip", "verdict_attribution": "market_wide"},
        {"verdict": "good_skip", "verdict_attribution": "idiosyncratic"},
        {"verdict": "regret_miss", "verdict_attribution": "idiosyncratic"},
    ]
    s = sp._shadow_stats(closed)
    assert s["good_skip_rate_pct"] == 75.0
    assert s["idiosyncratic_good_skip_rate_pct"] == 50.0
    assert s["n_market_wide"] == 2


# --------------------------------------------------------------------------- #
# 4. Performance breakdown: no statistics on noise
# --------------------------------------------------------------------------- #
def _trade(ticker, pnl, r=None, verdict="Thesis exit"):
    return {"ticker": ticker, "pnl_usd": pnl, "r_multiple": r, "days_held": 5,
            "verdict": verdict, "opened_at": "2026-07-09", "closed_at": "2026-07-15"}


def test_a_two_trade_bucket_reports_no_win_rate():
    """The live publish: by_sector servers_hardware trades:2 win_rate_pct: 0.0,
    on a PUBLIC dashboard, into a gate that can block trades."""
    b = pb.build_performance_breakdown([_trade("DELL", -135.19, -1.43),
                                        _trade("HPE", -45.33, -0.54)])
    assert b["total_trades"] == 2
    assert b["sufficient_sample"] is False
    assert b["sample_phase"] == "anecdote"
    for row in b["by_sector"] + b["by_confidence"] + b["by_verdict"]:
        assert row["win_rate_pct"] is None, row
        assert row["sufficient_sample"] is False
        assert row["min_trades_for_rate"] == pb.MIN_BUCKET_TRADES


def test_counts_are_facts_and_survive_the_suppression():
    """Suppressing the RATE must not suppress the evidence it was computed from."""
    b = pb.build_performance_breakdown([_trade("DELL", -135.19, -1.43),
                                        _trade("HPE", -45.33, -0.54)])
    sector = b["by_sector"][0]
    assert sector["trades"] == 2 and sector["wins"] == 0
    assert sector["total_pnl_usd"] == -180.52


def test_a_bucket_at_the_floor_reports_its_rate():
    trades = [_trade(f"T{i}", 100 if i < 3 else -50, 1.0) for i in range(5)]
    rows = pb._summarize({"networking": trades})
    assert rows[0]["sufficient_sample"] is True
    assert rows[0]["win_rate_pct"] == 60.0


def test_the_floor_matches_the_gate_that_reads_these_buckets():
    """calibration_gate demands an exception paragraph to trade a losing bucket;
    the two must agree about what counts as evidence."""
    assert pb.MIN_BUCKET_TRADES == cg.DEFAULTS["min_bucket_trades"]


def test_a_suppressed_bucket_cannot_trip_the_losing_sector_gate():
    """End to end: the n=2 servers_hardware bucket must not be able to demand a
    calibration_exception."""
    b = pb.build_performance_breakdown([_trade("DELL", -135.19, -1.43),
                                        _trade("HPE", -45.33, -0.54)])
    assert cg.losing_sector_buckets(b) == []


def test_markdown_renders_a_suppressed_rate_as_a_dash_not_a_zero():
    b = pb.build_performance_breakdown([_trade("DELL", -135.19, -1.43)])
    md = pb.render_breakdown_markdown(b)
    assert "0.0%" not in md
    assert "Sample phase: anecdote" in md


# --------------------------------------------------------------------------- #
# 5. Target attainment — making the 2:1 gate falsifiable
# --------------------------------------------------------------------------- #
def test_the_claim_and_the_outcome_are_measured_on_the_same_risk_unit():
    """claimed RR at entry vs REALIZED RR at exit. DELL's real numbers."""
    g = ea.grade_target_attainment(entry_price=450.67, exit_price=389.75,
                                   stop_loss=415.5, target_price=525.0,
                                   high_during_hold=463.48)
    assert g["claimed_rr"] == 2.11
    assert g["realized_rr"] == -1.73
    assert g["target_reached"] is False
    assert g["capture_fraction"] < 0
    assert g["gradeable"] is True


def test_a_target_actually_reached_is_scored_as_reached():
    g = ea.grade_target_attainment(entry_price=100.0, exit_price=118.0,
                                   stop_loss=90.0, target_price=120.0,
                                   high_during_hold=124.0)
    assert g["target_reached"] is True
    assert g["capture_fraction"] == 0.9
    assert g["near_target"] is True


def test_an_exit_above_the_target_proves_attainment_without_bars():
    g = ea.grade_target_attainment(entry_price=100.0, exit_price=125.0,
                                   stop_loss=90.0, target_price=120.0)
    assert g["target_reached"] is True


def test_an_unobservable_path_is_none_not_false():
    """None means we could not look; False means we looked and it did not happen.
    Averaging those together manufactures a hit rate out of missing data."""
    g = ea.grade_target_attainment(entry_price=100.0, exit_price=105.0,
                                   stop_loss=90.0, target_price=120.0)
    assert g["target_reached"] is None


def test_a_missing_plan_is_ungradeable_not_zero():
    g = ea.grade_target_attainment(entry_price=100.0, exit_price=105.0,
                                   stop_loss=None, target_price=None)
    assert g["gradeable"] is False
    assert g["claimed_rr"] is None and g["realized_rr"] is None


def test_a_stop_above_entry_is_not_a_risk_unit():
    """Dividing by a non-positive risk unit yields a confident nonsense RR."""
    g = ea.grade_target_attainment(entry_price=100.0, exit_price=105.0,
                                   stop_loss=105.0, target_price=120.0)
    assert g["claimed_rr"] is None and g["realized_rr"] is None


def test_at_n2_target_calibration_reports_anecdote_and_binds_nothing():
    """The whole point of grading a claim is that you do not act on the grade
    before you have one."""
    trades = [_trade("DELL", -135.19), _trade("HPE", -45.33)]
    tc = ea.build_target_calibration(trades, fetch_bars=False,
                                     min_trades_for_binding=15)
    assert tc["phase"] == "anecdote"
    assert tc["binding"] is False
    assert tc["sufficient_sample"] is False
    assert tc["rr_inflation"] is None, "must not publish a binding-grade number at n=2"


def test_target_inflation_binds_only_once_the_record_supports_it(monkeypatch):
    inflated = {"binding": True, "rr_inflation": 1.8, "mean_claimed_rr": 2.4,
                "mean_realized_rr": 0.6, "n_gradeable": 20}
    hit, why = cg.targets_inflated(inflated)
    assert hit is True and "2.4" in why

    not_yet = {**inflated, "binding": False, "phase": "anecdote", "n_gradeable": 2}
    hit2, why2 = cg.targets_inflated(not_yet)
    assert hit2 is False and "not binding" in why2


def test_the_confidence_cap_is_silent_until_binding(monkeypatch):
    monkeypatch.setattr(cg, "load_target_calibration",
                        lambda: {"binding": False, "phase": "anecdote",
                                 "n_gradeable": 2})
    assert cg.check_buy_target_calibration(
        {"action": "BUY", "confidence": 0.78}) == []


def test_the_confidence_cap_fires_on_a_binding_inflated_record(monkeypatch):
    monkeypatch.setattr(cg, "load_target_calibration",
                        lambda: {"binding": True, "rr_inflation": 1.9,
                                 "mean_claimed_rr": 2.5, "mean_realized_rr": 0.6,
                                 "n_gradeable": 20})
    reasons = cg.check_buy_target_calibration({"action": "BUY", "confidence": 0.78})
    assert len(reasons) == 1
    assert reasons[0].startswith("target_calibration_confidence_cap")
    # A proposal already inside the cap passes.
    assert cg.check_buy_target_calibration(
        {"action": "BUY", "confidence": 0.65}) == []
    # SELLs are never gated on entry calibration.
    assert cg.check_buy_target_calibration(
        {"action": "SELL_TO_CLOSE", "confidence": 0.9}) == []


def test_publish_writes_the_key_calibration_gate_reads_back():
    src = (Path(__file__).resolve().parent.parent / "runlib" / "publish.py").read_text()
    assert '"target_calibration"' in src, \
        "latest.json must carry target_calibration or the gate reads nothing"


def test_publish_materializes_the_knowledge_base_before_committing_it():
    """Listing knowledge_base.json in the git-add paths was decorative: the entry
    is gated on .exists() and the file had never existed."""
    src = (Path(__file__).resolve().parent.parent / "runlib" / "publish.py").read_text()
    assert "ensure_store" in src
    assert src.index("ensure_store") < src.index('"knowledge_base.json"')


# --------------------------------------------------------------------------- #
# 6. Bar helpers (pure)
# --------------------------------------------------------------------------- #
def test_window_extremes_respects_the_window():
    bars = {"2026-07-01": {"high": 10, "low": 5},
            "2026-07-05": {"high": 20, "low": 1},
            "2026-07-09": {"high": 15, "low": 8}}
    ext = bm.window_extremes(bars, "2026-07-02", "2026-07-08")
    assert ext["high"] == 20 and ext["low"] == 1 and ext["n_bars"] == 1


def test_window_extremes_returns_none_not_zero_when_empty():
    """A 0 low reads as "the stop was touched" to every caller downstream."""
    ext = bm.window_extremes({}, "2026-07-01", "2026-07-09")
    assert ext["high"] is None and ext["low"] is None and ext["n_bars"] == 0


def test_touched_levels_reports_unknown_rather_than_a_confident_false():
    t = bm.touched_levels("AMD", "2026-07-01", "2026-07-09",
                          stop=90.0, target=110.0, bars={})
    assert t["hit_stop"] is None and t["hit_target"] is None
    assert t["source"] == "unavailable"


def test_touched_levels_on_real_bars():
    bars = {"2026-07-03": {"high": 112.0, "low": 89.0, "close": 100.0}}
    t = bm.touched_levels("AMD", "2026-07-01", "2026-07-09",
                          stop=90.0, target=110.0, bars=bars)
    assert t["hit_stop"] is True and t["hit_target"] is True
    assert t["source"] == "daily_bars"
