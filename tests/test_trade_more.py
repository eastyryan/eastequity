"""2026-10-01 "trade more" changes (user-approved, PAPER only):

  * STARTER entries — half-risk first tranche at a watchlist trigger on a full
    in-session slot with a fresh live overlay; does NOT consume a probe slot.
  * TWO SEATS PER THEME — a second same-driver seat while the book is <40%
    deployed and the challenger's setup score >= the holder's, capped at 0.75%
    risk; blocked otherwise; a third seat always blocked.
  * Engagement flat threshold read from config (0.30).
  * max_open_probes = 2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator
from tests.test_risk_sizing import buy, pf, pos

ROOT = Path(__file__).resolve().parent.parent
CFG = validator.load_config()
BASE = float(CFG["position_sizing"]["risk_based_sizing"]["risk_per_trade_pct"])


def _bundle(depth="full", stale=False, status="ok", scores=None):
    return {"_bundle": {
        "run_depth": depth,
        "price_freshness_live": {"status": status, "stale": stale, "age_min": 2.0},
        "universe_scan": {"indicators_by_ticker": {
            t: {"swing_setup_score": s} for t, s in (scores or {}).items()}},
    }}


# --------------------------------------------------------------------------- #
# Starter
# --------------------------------------------------------------------------- #
def test_starter_is_tagged_and_half_risk_on_a_fresh_full_slot():
    p = buy(ticker="ABBV", driver="healthcare", size=1500.0, entry_type="starter")
    r = validator.validate_proposals([p], pf(), _bundle())[0]
    assert r.approved, r.reasons
    assert p.get("starter") is True
    assert not p.get("calibration_probe")
    scale = float(CFG["trade_quality_requirements"]["starter_entry"]["risk_scale"])
    assert abs(validator._risk_budget_pct(p, CFG) - BASE * scale) < 1e-12
    # (conftest pins a $10k test book) $10k x 0.5% = $50 risk at an 8% stop -> $625.
    assert abs(p["position_size_usd"] - 625.0) < 0.01, p["position_size_usd"]


def test_starter_rejected_off_a_full_slot_or_on_a_stale_overlay():
    p = buy(ticker="ABBV", driver="healthcare", entry_type="starter")
    r = validator.validate_proposals([p], pf(), _bundle(depth="holdings_watchlist"))[0]
    assert any("starter_wrong_slot" in x for x in r.reasons), r.reasons
    p = buy(ticker="ABBV", driver="healthcare", entry_type="starter")
    r = validator.validate_proposals([p], pf(), _bundle(stale=True))[0]
    assert any("starter_requires_fresh_live_overlay" in x for x in r.reasons), r.reasons
    p = buy(ticker="ABBV", driver="healthcare", entry_type="starter")
    r = validator.validate_proposals([p], pf(), {})[0]   # no bundle -> fail closed
    assert not r.approved


def test_starter_does_not_consume_a_probe_slot():
    held_probes = [dict(pos(f"P{i}", 50, 45, 1, "healthcare"),
                        plan={"stop_loss": 45.0, "calibration_probe": True,
                              "demand_driver": "consumer"})
                   for i in range(2)]
    p = buy(ticker="ABBV", driver="healthcare", entry_type="starter")
    r = validator.validate_proposals([p], pf(positions=held_probes), _bundle())[0]
    assert not any("probe_cap" in x for x in r.reasons), r.reasons


def test_a_full_confidence_entry_is_not_a_starter():
    p = buy(ticker="ABBV", driver="healthcare")
    validator.validate_proposals([p], pf(), _bundle())
    assert not p.get("starter")
    assert abs(validator._risk_budget_pct(p, CFG) - BASE) < 1e-12


def test_entry_type_survives_parsing_and_lands_on_the_plan():
    from runlib.brain_io import OPTIONAL_PROPOSAL_FIELDS
    assert "entry_type" in OPTIONAL_PROPOSAL_FIELDS
    src = (ROOT / "runlib" / "brain_io.py").read_text()
    assert '"entry_type", "starter", "theme_second_seat")' in src


def test_trigger_alerts_flag_the_starter_zone():
    from tools.watchlist_triggers import check_watchlist_triggers, starter_band_pct
    assert starter_band_pct(2.0) == 1.0       # 0.5 x ATR
    assert starter_band_pct(6.0) == 1.5       # capped at 1.5%
    assert starter_band_pct(None) == 1.5
    wl = [{"ticker": "AMD", "would_buy_at": "near $500"}]
    a = check_watchlist_triggers(wl, {"AMD": 504.0}, {"AMD": 2.0})[0]   # +0.8%
    assert a["starter_zone"] is True
    a = check_watchlist_triggers(wl, {"AMD": 507.0}, {"AMD": 2.0})[0]   # +1.4% > 1.0
    assert a["starter_zone"] is False
    a = check_watchlist_triggers(wl, {"AMD": 495.0})[0]                 # through it
    assert a["starter_zone"] is True


# --------------------------------------------------------------------------- #
# Two seats per theme
# --------------------------------------------------------------------------- #
def _dell(mv_qty=10, cost=100.0):
    return pos("DELL", cost, cost * 0.95, mv_qty, "hyperscaler_server_capex")


def test_second_same_theme_seat_allowed_under_40pct_and_capped_at_075():
    positions = [_dell()]                       # $1,000 MV on $10k = 10% deployed
    p = buy(ticker="HPE", size=1500.0, driver="hyperscaler_server_capex")
    r = validator.validate_proposals(
        [p], pf(positions=positions),
        _bundle(scores={"HPE": 2.0, "DELL": 1.5}))[0]
    assert r.approved, r.reasons
    assert not any("theme_second_seat" in x or "theme_seat_cap" in x
                   for x in r.reasons), r.reasons
    assert p.get("theme_second_seat") is True
    seat = CFG["position_sizing"]["risk_based_sizing"]["second_theme_seat"]
    assert abs(validator._risk_budget_pct(p, CFG) - float(seat["risk_pct"])) < 1e-12
    assert float(seat["risk_pct"]) <= 0.0075
    # $75 risk at an 8% stop -> $937.50
    assert abs(p["position_size_usd"] - 937.5) < 0.01, p["position_size_usd"]


def test_second_seat_blocked_when_book_is_40pct_deployed():
    positions = [_dell(mv_qty=40)]              # $4,000 MV = 40% deployed
    p = buy(ticker="HPE", size=2000.0, driver="hyperscaler_server_capex")
    r = validator.validate_proposals([p], pf(positions=positions), _bundle())[0]
    assert not r.approved
    assert any("theme_second_seat_blocked" in x for x in r.reasons), r.reasons


def test_second_seat_blocked_when_challenger_setup_is_weaker():
    positions = [_dell()]
    p = buy(ticker="HPE", size=2000.0, driver="hyperscaler_server_capex")
    r = validator.validate_proposals(
        [p], pf(positions=positions), _bundle(scores={"HPE": 1.0, "DELL": 1.5}))[0]
    assert any("theme_second_seat_weaker_setup" in x for x in r.reasons), r.reasons


def test_third_seat_in_one_theme_is_blocked():
    positions = [_dell(), pos("SMCI", 50, 47, 10, "hyperscaler_server_capex")]
    p = buy(ticker="HPE", size=500.0, driver="hyperscaler_server_capex")
    r = validator.validate_proposals([p], pf(positions=positions), _bundle())[0]
    if validator._position_demand_driver(positions[1]) == "hyperscaler_server_capex":
        assert any("theme_seat_cap" in x for x in r.reasons), r.reasons


def test_an_add_to_a_held_ticker_is_not_a_second_seat():
    positions = [_dell(mv_qty=40)]
    p = buy(ticker="DELL", size=300.0, entry=110.0, stop=102.0, target=135.0,
            driver="hyperscaler_server_capex")
    validator.validate_proposals([p], pf(positions=positions), _bundle())
    assert not p.get("theme_second_seat")


def test_theme_risk_cap_fits_two_seats():
    rbs = CFG["position_sizing"]["risk_based_sizing"]
    assert rbs["theme_initial_risk_cap_pct"] == 0.025
    assert rbs["risk_per_trade_pct"] + rbs["second_theme_seat"]["risk_pct"] \
        <= rbs["theme_initial_risk_cap_pct"]
    assert CFG["theme_risk"]["max_demand_driver_concentration_pct"] == 0.35


# --------------------------------------------------------------------------- #
# Engagement + probes
# --------------------------------------------------------------------------- #
def test_engagement_threshold_comes_from_config():
    from tools.engagement import deployment_status, flat_threshold_pct
    assert CFG["engagement"]["flat_threshold_pct"] == 0.30
    assert flat_threshold_pct(CFG) == 0.30
    assert flat_threshold_pct({"engagement": {"flat_threshold_pct": 0.1}}) == 0.1
    assert flat_threshold_pct({}) == 0.30
    st = deployment_status([{"ticker": "X", "market_value_usd": 250.0}], 1000.0,
                           None, "2026-10-01", flat_threshold_pct=flat_threshold_pct(CFG))
    assert st["flat"]                                    # 25% < 30%
    st = deployment_status([{"ticker": "X", "market_value_usd": 250.0}], 1000.0,
                           None, "2026-10-01", flat_threshold_pct=0.2)
    assert not st["flat"]


def test_every_in_session_full_run_under_threshold_owes_a_commitment():
    from tools.engagement import commitment_required
    eng = {"flat": True, "requires_commitment": False}
    assert commitment_required(eng, "full", True)
    assert not commitment_required(eng, "full", False)
    assert not commitment_required(eng, "holdings_watchlist", True)
    assert not commitment_required({"flat": False}, "full", True)
    assert commitment_required({"flat": True, "requires_commitment": True},
                               "light", False)


def test_max_open_probes_is_two():
    probe = CFG["trade_quality_requirements"]["calibration_probe"]
    assert probe["max_open_probes"] == 2
    held = [{"ticker": f"P{i}", "plan": {"calibration_probe": True}} for i in range(2)]
    reasons: list[str] = []
    validator._check_probe_limits({"calibration_probe": True}, CFG,
                                  {"positions": held[:1]}, 0, reasons)
    assert reasons == []
    validator._check_probe_limits({"calibration_probe": True}, CFG,
                                  {"positions": held}, 0, reasons)
    assert any("probe_cap_reached" in x for x in reasons)


# --------------------------------------------------------------------------- #
# Lesson supersession (journal is append-only; retired lessons are annotated)
# --------------------------------------------------------------------------- #
def test_retired_lessons_are_annotated_not_deleted():
    from runlib.context_gather import mark_superseded_lessons
    notes = [
        {"date": "2026-09-25", "note": "Weekly self-review: ... or it waits — "
                                       "never a second half-size test. ..."},
        {"date": "2026-09-18", "note": "Weekly self-review: a trigger may only name "
                                       "a completed prior session's high."},
        {"date": "2026-09-11", "note": "Weekly self-review: unrelated lesson."},
    ]
    out = mark_superseded_lessons(notes, CFG)
    assert len(out) == 3
    assert out[0]["note"].startswith("[SUPERSEDED 2026-10-01")
    assert "never a second half-size test" in out[0]["note"]   # history kept
    assert out[0]["superseded"]["match"] == "never a second half-size test"
    assert out[1]["note"].startswith("[SUPERSEDED")
    assert "superseded" not in out[2]
    assert notes[0]["note"].startswith("Weekly")                # input not mutated


def test_requires_commitment_wires_full_in_session_runs():
    import orchestrator
    import tools.market_calendar as mc
    ctx = {"engagement": {"flat": True, "requires_commitment": False}}
    orig = mc.is_market_open
    try:
        mc.is_market_open = lambda *a, **k: True
        assert orchestrator._requires_commitment(ctx, "full")
        assert not orchestrator._requires_commitment(ctx, "holdings_watchlist")
        mc.is_market_open = lambda *a, **k: False
        assert not orchestrator._requires_commitment(ctx, "full")
    finally:
        mc.is_market_open = orig
