"""Deterministic local risk desk — the fallback when the grok CLI is absent.

Regression for 2026-10-01 run 20261001-87d7c5: the box's cloud routine has no
`grok` binary, so adversarial_review hard-rejected the 15:00-slot HPE BUY as
risk_desk_unavailable (and every BUY the box ever proposed). The fixture is
that run's exact proposal as parsed from state/brain_response.md.

These tests pin BOTH directions: a valid proposal now passes the desk, and
the desk still refuses (journaled veto) anything that breaks a cap — it must
never become a silent approve-everything.
"""
import copy
import json
import shutil
from pathlib import Path

import pytest

import journal
import validator
from runlib import brain_io, local_risk_desk

FIXTURE = Path(__file__).parent / "fixtures" / "hpe_20261001_87d7c5_proposal.json"


@pytest.fixture
def hpe():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def book():
    # Mirrors the live book at 15:20 ET on 2026-10-01 scaled to the pinned
    # $10k test account: one NOW seat (software_platforms), mostly cash.
    return {
        "cash_usd": 8900.0, "total_equity_usd": 10000.0,
        "positions": [{
            "ticker": "NOW", "quantity": 8.0, "avg_cost": 135.9,
            "market_value_usd": 1100.0, "last_price": 138.5,
            "opened_at": "2026-09-22T16:28:12+00:00",
            "demand_driver": "software_platforms",
            "plan": {"stop_loss": 125.0, "target_price": 164.0,
                     "entry_price_max": 137.8, "confidence": 0.65,
                     "demand_driver": "software_platforms"},
            "trailing_stop": 125.06,
        }],
        "pending_orders": {}, "history": [],
    }


@pytest.fixture
def mc():
    return {
        "HPE": {"atr_pct": 6.05, "expected_move_pct": 7.27,
                "market_cap_usd": 88_475_941_989.9},
        "NOW": {"atr_pct": 3.1, "market_cap_usd": 160_000_000_000.0},
    }


@pytest.fixture
def ctx(tmp_path):
    p = tmp_path / "context_full_test.json"
    p.write_text(json.dumps({
        "news_and_catalysts": {"HPE": ["Networking Investor Day; FY27 networking "
                                       "growth raised; Juniper synergies"]},
        "portfolio": {},
    }))
    return str(p)


@pytest.fixture
def no_cli(monkeypatch):
    """The box: no grok binary, desk required, local fallback on."""
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    real = validator.load_config

    def cfg():
        c = real()
        rc = c.setdefault("risk_controls", {})
        rc["require_risk_desk_for_buys"] = True
        rc["local_risk_desk_fallback"] = True
        return c
    monkeypatch.setattr(validator, "load_config", cfg)


@pytest.fixture
def jlog(monkeypatch):
    seen = {"rej": [], "imp": []}
    monkeypatch.setattr(journal, "log_rejection",
                        lambda p, r, rid: seen["rej"].append((p.get("ticker"), r)))
    monkeypatch.setattr(journal, "log_improvement",
                        lambda msg, rid=None, *a, **k: seen["imp"].append(msg))
    return seen


def _run(props, ctx, book, mc):
    return brain_io.adversarial_review(props, ctx, "RID-TEST",
                                       local_inputs=lambda: (book, mc))


def test_rr_exactly_at_floor_is_not_rejected_by_float_noise(hpe, book, mc):
    """64.90/58.90/76.90 is exactly 2.00R; binary floats said 1.9999999999999976."""
    res = validator.validate_proposals([copy.deepcopy(hpe)], book, mc)[0]
    assert not any(r.startswith("risk_reward_too_low") for r in res.reasons), res.reasons


def test_hpe_proposal_now_passes_local_desk_and_validator(no_cli, jlog, hpe, ctx, book, mc):
    kept = _run([copy.deepcopy(hpe)], ctx, book, mc)
    assert [p["ticker"] for p in kept] == ["HPE"]
    assert kept[0]["risk_desk_mode"] == "local_deterministic"
    assert not any("risk_desk_unavailable" in str(r) for _, r in jlog["rej"])
    # Visible, never silent: the run journals that the local desk stood in.
    assert any("LOCAL DETERMINISTIC" in m for m in jlog["imp"])
    # Haircut is bounded and only ever downward.
    assert 0.54 <= kept[0]["confidence"] <= hpe["confidence"]
    res = validator.validate_proposals(kept, book, mc)[0]
    assert res.approved, res.reasons


def test_fallback_off_keeps_strict_rejection(monkeypatch, jlog, hpe, ctx, book, mc):
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    real = validator.load_config

    def cfg():
        c = real()
        c.setdefault("risk_controls", {})["require_risk_desk_for_buys"] = True
        c["risk_controls"]["local_risk_desk_fallback"] = False
        return c
    monkeypatch.setattr(validator, "load_config", cfg)
    assert _run([copy.deepcopy(hpe)], ctx, book, mc) == []
    assert jlog["rej"] and "risk_desk_unavailable" in jlog["rej"][0][1][0]


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p.pop("stop_loss"), "stop_missing"),
    (lambda p: p.update(stop_loss=0), "stop_missing"),
    (lambda p: p.update(stop_loss=66.0), "stop_not_below_entry"),
    (lambda p: p.update(stop_loss=63.5, target_price=76.9), "stop_inside_noise"),
    (lambda p: p.update(target_price=69.0, stop_loss=62.9), "target_too_close"),
    (lambda p: p.update(risk_reward_ratio=2.6), "rr_flattered"),
    (lambda p: p.pop("thesis_invalidators"), "validator_dry_run"),
    (lambda p: p.update(ticker="ZZZZ"), "validator_dry_run"),
])
def test_hard_fails_are_vetoed_and_journaled(no_cli, jlog, hpe, ctx, book, mc,
                                             mutate, needle):
    p = copy.deepcopy(hpe)
    mutate(p)
    kept = _run([p], ctx, book, mc)
    assert kept == [], f"{needle}: desk approved a broken BUY"
    assert jlog["rej"], "a veto must be journaled"
    assert needle in jlog["rej"][0][1][0]
    assert jlog["rej"][0][1][0].startswith("risk_desk_veto:")


def test_missing_volatility_data_fails_closed(no_cli, jlog, hpe, ctx, book, mc):
    mc.pop("HPE")
    assert _run([copy.deepcopy(hpe)], ctx, book, mc) == []
    assert "volatility_data_missing" in jlog["rej"][0][1][0]


def test_cash_cap_is_enforced(no_cli, jlog, hpe, ctx, book, mc):
    book["cash_usd"] = 50.0
    assert _run([copy.deepcopy(hpe)], ctx, book, mc) == []
    assert "cash" in jlog["rej"][0][1][0]


def test_theme_cap_is_enforced_by_dry_run(no_cli, jlog, hpe, ctx, book, mc):
    """A book already full of the same demand_driver must veto the new seat."""
    seats = []
    for i, t in enumerate(["DELL", "SMCI", "ANET"]):
        seats.append({"ticker": t, "quantity": 10.0, "avg_cost": 100.0,
                      "market_value_usd": 1000.0, "last_price": 100.0,
                      "opened_at": "2026-09-2%dT15:00:00+00:00" % (i + 1),
                      "demand_driver": "hyperscaler_server_capex",
                      "plan": {"stop_loss": 90.0, "entry_price_max": 100.0,
                               "demand_driver": "hyperscaler_server_capex"},
                      "trailing_stop": 90.0})
        mc[t] = {"atr_pct": 3.0, "market_cap_usd": 5e10}
    book["positions"] += seats
    assert _run([copy.deepcopy(hpe)], ctx, book, mc) == []
    assert "validator_dry_run" in jlog["rej"][0][1][0]


def test_portfolio_unavailable_vetoes(no_cli, jlog, hpe, tmp_path, mc):
    empty_ctx = tmp_path / "c.json"
    empty_ctx.write_text(json.dumps({"news_and_catalysts": {}}))  # no portfolio
    kept = brain_io.adversarial_review([copy.deepcopy(hpe)], str(empty_ctx), "R",
                                       local_inputs=lambda: (None, mc))
    assert kept == [] and "portfolio_unavailable" in jlog["rej"][0][1][0]


def test_unreadable_bundle_vetoes(no_cli, jlog, hpe, book):
    kept = brain_io.adversarial_review([copy.deepcopy(hpe)], "/nonexistent.json", "R",
                                       local_inputs=lambda: (book, {"HPE": {"atr_pct": 6}}))
    assert kept == [] and "context_bundle_unreadable" in jlog["rej"][0][1][0]


def test_input_callable_crash_does_not_approve(no_cli, jlog, hpe, tmp_path):
    c = tmp_path / "c.json"
    c.write_text(json.dumps({"news_and_catalysts": {}}))

    def boom():
        raise RuntimeError("broker down")
    kept = brain_io.adversarial_review([copy.deepcopy(hpe)], str(c), "R",
                                       local_inputs=boom)
    assert kept == []


def test_local_desk_internal_crash_falls_back_to_strict_rejection(
        no_cli, jlog, monkeypatch, hpe, ctx, book, mc):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(local_risk_desk, "review", boom)
    assert _run([copy.deepcopy(hpe)], ctx, book, mc) == []
    assert "risk_desk_unavailable" in jlog["rej"][0][1][0]


def test_theme_overlap_and_unsourced_haircuts_are_bounded(no_cli, jlog, hpe, ctx, book, mc):
    p = copy.deepcopy(hpe)
    # One small held seat in HPE's own driver: allowed by the caps, but a stack.
    book["positions"].append({
        "ticker": "DELL", "quantity": 1.0, "avg_cost": 500.0,
        "market_value_usd": 500.0, "last_price": 500.0,
        "opened_at": "2026-09-25T15:00:00+00:00",
        "demand_driver": "hyperscaler_server_capex",
        "plan": {"stop_loss": 480.0, "entry_price_max": 500.0,
                 "demand_driver": "hyperscaler_server_capex"},
        "trailing_stop": 480.0})
    book["cash_usd"] -= 500.0
    mc["DELL"] = {"atr_pct": 3.0, "market_cap_usd": 3e11}
    rv = local_risk_desk.review([p], ctx, portfolio=book, market_context=mc)
    r = rv["HPE"]
    assert r["verdict"] == "approve", r["objection"]
    # unsourced cap (-0.03) + theme stack (-0.03), clamped to the -0.10 ceiling
    assert r["confidence_adjustment"] == pytest.approx(-0.06)
    assert "hyperscaler_server_capex" in r["objection"]
    assert "not web-verified" in r["objection"].lower() or "NOT web-verified" in r["objection"]


def test_sells_untouched_and_llm_failure_uses_local_desk(monkeypatch, jlog, hpe, ctx, book, mc):
    """CLI present but desk output unparsable -> local desk, not pass-through."""
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/grok")
    monkeypatch.setattr(brain_io, "run_claude", lambda *a, **k: "no json here")
    real = validator.load_config

    def cfg():
        c = real()
        c.setdefault("risk_controls", {})["require_risk_desk_for_buys"] = True
        c["risk_controls"]["local_risk_desk_fallback"] = True
        return c
    monkeypatch.setattr(validator, "load_config", cfg)
    sell = {"ticker": "NOW", "action": "SELL_TO_CLOSE"}
    kept = _run([copy.deepcopy(hpe), sell], ctx, book, mc)
    assert [p["ticker"] for p in kept] == ["HPE", "NOW"]
    assert kept[0]["risk_desk_mode"] == "local_deterministic"


def test_live_config_enables_fallback_and_keeps_desk_required():
    rc = validator.load_config()["risk_controls"]
    assert rc["require_risk_desk_for_buys"] is True
    assert rc["local_risk_desk_fallback"] is True


def test_orchestrator_feeds_the_local_desk_live_inputs():
    """Without local_inputs the dry run would fall back to the bundle's book
    snapshot and a volatility-only market_context — weaker than the validator."""
    import inspect
    import orchestrator
    src = inspect.getsource(orchestrator)
    assert "local_inputs=lambda: (get_portfolio_state()," in src
    assert "_build_market_context(context, _desk_props)" in src
