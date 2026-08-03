"""Offline tests for the 2026-08-03 fixes — no network.

Covers the three code-enforced changes made after the audit that found the agent
had made 3 trades in 165 runs:
  * tools/earnings_window  — the coded pre-print blackout that replaces the
    brain's self-invented "no print inside 45 days" exclusion.
  * tools/signal_discipline — triggers may not rest on measured-dead indicators,
    and an unwind may not be used as a trading halt.
  * validator._check_earnings_window / _check_portfolio_beta — the gates.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator
from tools.earnings_window import (
    build_earnings_map,
    earnings_state,
    parse_by_date,
)
from tools.signal_discipline import (
    dead_indicator_veto,
    dead_signals_in,
    sanitize_trigger,
    unwind_used_as_halt,
)


# ---------------------------------------------------------------------------
# earnings_window
# ---------------------------------------------------------------------------
def test_parse_by_date_reads_the_committed_calendar_format():
    parsed = parse_by_date({
        "2026-08-04": "MPC(bmo), AMD(amc), AMGN(amc)",
        "2026-08-06": "NET(amc), TEAM(amc)",
    })
    assert parsed["MPC"][1] == "bmo"
    assert parsed["AMD"][1] == "amc"
    assert parsed["NET"][0].isoformat() == "2026-08-06"
    assert set(parsed) == {"MPC", "AMD", "AMGN", "NET", "TEAM"}


def test_earliest_print_wins_when_a_name_is_listed_twice():
    parsed = parse_by_date({
        "2026-08-10": "FOO(bmo)",
        "2026-08-04": "FOO(amc)",
    })
    assert parsed["FOO"][0].isoformat() == "2026-08-04"


def test_build_map_computes_days_to_earnings_from_as_of():
    ctx = {"earnings_week": {
        "as_of": "2026-08-03",
        "by_date": {"2026-08-04": "MPC(bmo)", "2026-08-06": "NET(amc)"},
    }}
    em = build_earnings_map(ctx)
    assert em["MPC"]["days_to_earnings"] == 1
    assert em["NET"]["days_to_earnings"] == 3
    assert em["MPC"]["source"] == "earnings_week"


def test_scan_rows_only_fill_names_the_calendar_missed():
    ctx = {
        "earnings_week": {"as_of": "2026-08-03",
                          "by_date": {"2026-08-04": "MPC(bmo)"}},
        "universe_scan": {"top_setups": [
            {"ticker": "MPC", "days_to_earnings": 99},   # calendar wins
            {"ticker": "BKR", "days_to_earnings": 80},   # fallback fills
        ]},
    }
    em = build_earnings_map(ctx)
    assert em["MPC"]["days_to_earnings"] == 1
    assert em["BKR"]["days_to_earnings"] == 80
    assert em["BKR"]["source"] == "universe_scan"


def test_blackout_and_drift_classification():
    inside = earnings_state({"days_to_earnings": 1}, blackout_days=3, drift_days=10)
    assert inside["in_blackout"] and not inside["in_drift"]

    # The whole point of the change: a print 30 days out is NOT a blackout.
    outside = earnings_state({"days_to_earnings": 30}, blackout_days=3, drift_days=10)
    assert not outside["in_blackout"]

    # Already reported — the binary is resolved, this is the preferred lane.
    drift = earnings_state({"days_since_earnings": 4}, blackout_days=3, drift_days=10)
    assert drift["in_drift"] and not drift["in_blackout"]

    unmeasured = earnings_state({}, blackout_days=3, drift_days=10)
    assert not unmeasured["measured"]
    assert not unmeasured["in_blackout"]


# ---------------------------------------------------------------------------
# validator earnings gate
# ---------------------------------------------------------------------------
def _cfg(**over):
    cfg = {"trade_quality_requirements": {
        "earnings_window": {
            "enabled": True, "pre_print_blackout_days": 3,
            "post_print_drift_days": 10, "allow_trade_through_with_case": True,
            "min_earnings_case_chars": 80,
        }}}
    cfg["trade_quality_requirements"]["earnings_window"].update(over)
    return cfg


def test_gate_blocks_a_buy_inside_the_blackout():
    reasons: list[str] = []
    validator._check_earnings_window(
        {"action": "BUY", "ticker": "MPC"}, _cfg(),
        {"_earnings": {"MPC": {"days_to_earnings": 1, "when": "bmo",
                               "earnings_date": "2026-08-04"}}},
        reasons)
    assert any(r.startswith("earnings_blackout:MPC") for r in reasons)


def test_gate_allows_a_print_outside_the_window():
    """The half that actually needed fixing — 30 days out is tradeable."""
    reasons: list[str] = []
    validator._check_earnings_window(
        {"action": "BUY", "ticker": "BKR"}, _cfg(),
        {"_earnings": {"BKR": {"days_to_earnings": 30}}}, reasons)
    assert reasons == []


def test_gate_is_silent_on_an_unmeasured_ticker():
    reasons: list[str] = []
    validator._check_earnings_window(
        {"action": "BUY", "ticker": "XYZ"}, _cfg(), {"_earnings": {}}, reasons)
    assert reasons == []


def test_explicit_earnings_case_permits_trading_through_the_print():
    case = ("Trading THROUGH the print deliberately: the binary IS the thesis, "
            "sized at half the normal risk budget against a stop below the base.")
    reasons: list[str] = []
    validator._check_earnings_window(
        {"action": "BUY", "ticker": "MPC", "earnings_case": case}, _cfg(),
        {"_earnings": {"MPC": {"days_to_earnings": 1}}}, reasons)
    assert reasons == []


def test_a_short_earnings_case_does_not_satisfy_the_gate():
    reasons: list[str] = []
    validator._check_earnings_window(
        {"action": "BUY", "ticker": "MPC", "earnings_case": "trading through it"},
        _cfg(), {"_earnings": {"MPC": {"days_to_earnings": 1}}}, reasons)
    assert any(r.startswith("earnings_blackout") for r in reasons)


def test_sells_are_never_blocked_by_the_earnings_gate():
    reasons: list[str] = []
    validator._check_earnings_window(
        {"action": "SELL_TO_CLOSE", "ticker": "MPC"}, _cfg(),
        {"_earnings": {"MPC": {"days_to_earnings": 0}}}, reasons)
    assert reasons == []


# ---------------------------------------------------------------------------
# signal discipline — dead-indicator triggers
# ---------------------------------------------------------------------------
def test_the_measured_dead_reads_are_recognized():
    assert dead_signals_in("wait for the MACD bull cross")
    assert dead_signals_in("needs volume confirmation above average volume")
    assert dead_signals_in("ADX above 25 confirms the trend")
    assert not dead_signals_in("close above $175 on a higher low")


def test_the_one_surviving_volume_read_is_never_stripped():
    """no_supply_pullback scored +0.22/+0.25pp — the only arm that survived."""
    res = sanitize_trigger("near $170 with a no_supply_pullback read")
    assert not res["changed"]
    assert "no_supply_pullback" in res["trigger"]


def test_a_dead_confirmation_leg_is_stripped_and_the_price_level_survives():
    """The exact ANET/GE failure: a price trigger that could never fire."""
    res = sanitize_trigger("close above $175, confirmed by volume")
    assert res["changed"]
    assert "$175" in res["trigger"]
    assert "volume" not in res["trigger"].lower()
    assert res["stripped"]
    assert not res["empty"]


def test_the_crwd_macd_trigger_is_stripped():
    res = sanitize_trigger("reclaim of $195 and a MACD bull cross")
    assert res["changed"]
    assert "$195" in res["trigger"]
    assert "macd" not in res["trigger"].lower()


def test_a_trigger_made_only_of_dead_reads_reports_empty():
    res = sanitize_trigger("once the MACD bull cross prints")
    assert res["empty"] and res["changed"]
    assert res["trigger"] == ""


def test_an_inline_qualifier_is_excised_not_just_a_whole_clause():
    """The real 07-23/24 forms attach the dead read with a connective."""
    assert sanitize_trigger(
        "$345-348 higher low on above average volume")["trigger"] == "$345-348 higher low"
    assert sanitize_trigger(
        "break of $200 on 2x average volume and ADX above 25")["trigger"] == "break of $200"


def test_a_leading_dead_read_does_not_swallow_the_level_behind_it():
    """"confirmed_breakout above the 20d high" keeps the structure."""
    res = sanitize_trigger("a confirmed_breakout above the 20d high")
    assert res["changed"] and not res["empty"]
    assert res["trigger"] == "above the 20d high"

    res2 = sanitize_trigger("unconfirmed_breakout_suspect near $90 support")
    assert res2["trigger"] == "near $90 support"


def test_stripping_a_dead_leg_never_destroys_the_event_gate():
    """The tail must stop at an event cue.

    A bare `[^,;]*` tail swallowed the event half of a compound trigger, storing
    "close above $175 with volume confirmation after the 8/4 print" as
    "close above $175" — silently converting a GATED trigger into an ungated one
    and re-creating the AMD incident in reverse (alert fires before the catalyst).
    With trigger accountability live, a spurious fire costs a forced resolution
    and counts toward the 3-hit force-drop.
    """
    res = sanitize_trigger(
        "close above $175 with volume confirmation after the 8/4 print")
    assert res["changed"]
    assert "volume" not in res["trigger"].lower()
    assert "after the 8/4 print" in res["trigger"]

    for cue in ("before the 8/6 print", "once the 7/22 print lands",
                "post-earnings", "until the 8/18 report"):
        out = sanitize_trigger(f"break of $200 on 2x average volume {cue}")["trigger"]
        assert cue.split()[0] in out.lower(), out
        assert "volume" not in out.lower(), out


def test_a_clean_trigger_is_left_alone():
    res = sanitize_trigger("near $170 or after the 8/4 earnings print")
    assert not res["changed"]
    assert res["trigger"] == "near $170 or after the 8/4 earnings print"


def test_dead_indicator_veto_fires_when_that_is_the_whole_argument():
    hit = dead_indicator_veto("volume confirmation not met, volume_read neutral")
    assert hit and "volume_confirmation" in hit["signals"]


def test_the_negated_volume_phrasings_from_the_journal_are_caught():
    """How a veto actually reads in journal/runs — not "volume confirmation"."""
    for text in ("hit my level but volume did not confirm the move",
                 "still no volume confirmation on the breakout",
                 "ANET and GE both hit price triggers but neither confirmed "
                 "with volume (still neutral, not pocket-pivot)"):
        assert dead_indicator_veto(text) is not None, text


def test_a_satisfied_level_does_not_launder_a_dead_read_veto():
    """In "X but Y" the ground is Y — X is the condition that WAS met."""
    hit = dead_indicator_veto(
        "CRWD crossed the $195 price but the confirming MACD bull cross has "
        "not printed")
    assert hit is not None and "macd" in hit["signals"]

    # ...but a genuine ground after the contrastive still passes.
    assert dead_indicator_veto(
        "ANET is at $176 but reports earnings 8/4, inside the entry window"
    ) is None
    assert dead_indicator_veto(
        "GE hit $345 but the stop would sit 11% away, breaking the RR floor"
    ) is None


def test_dead_indicator_veto_is_silent_when_a_real_ground_exists():
    """Citing an indicator descriptively alongside a real reason is correct."""
    assert dead_indicator_veto(
        "reports earnings on 8/4, inside the window; MACD also has not crossed"
    ) is None
    assert dead_indicator_veto("extended, 22% above the $170 base") is None


# ---------------------------------------------------------------------------
# signal discipline — unwind is a haircut, not a halt
# ---------------------------------------------------------------------------
def test_unwind_as_the_whole_reason_is_flagged():
    hit = unwind_used_as_halt(
        "Momentum factor is in a confirmed unwind (MTUM -12%) - not a day to add.",
        [], momentum_status="unwind")
    assert hit and hit["momentum_status"] == "unwind"


def test_names_rejected_on_their_own_merits_are_not_flagged():
    hit = unwind_used_as_halt(
        "Momentum unwind active, but every name failed on its own terms.",
        [{"ticker": "MPC", "reason": "reports 8/4, inside the swing entry window"},
         {"ticker": "NET", "reason": "236x forward P/E with a binary print 8/6"}],
        momentum_status="unwind")
    assert hit is None


def test_a_healthy_tape_never_flags():
    assert unwind_used_as_halt("no fat pitch today", [],
                               momentum_status="healthy") is None


# ---------------------------------------------------------------------------
# beta gate — unknown beta is assumed hostile, not fatal
# ---------------------------------------------------------------------------
_BETA_CFG = {
    "book_risk": {"enabled": True, "max_portfolio_beta": 1.6,
                  "unknown_beta_assumption": 1.6},
    "position_sizing": {"starting_capital_usd": 1000},
}


def test_unknown_beta_on_a_new_name_no_longer_kills_a_small_buy():
    """The BKR 2026-07-28 case: $49 proposal, 100% cash book, BKR has no beta."""
    reasons: list[str] = []
    validator._check_portfolio_beta(
        {"action": "BUY", "ticker": "BKR", "position_size_usd": 49.1},
        _BETA_CFG, {"total_equity_usd": 984.0, "positions": []},
        {"_book": {"betas": {"SPY": 1.0}}}, reasons)
    assert reasons == []


def test_a_held_position_with_no_beta_still_fails_closed():
    reasons: list[str] = []
    validator._check_portfolio_beta(
        {"action": "BUY", "ticker": "BKR", "position_size_usd": 49.1},
        _BETA_CFG,
        {"total_equity_usd": 984.0,
         "positions": [{"ticker": "MYSTERY", "market_value_usd": 400.0}]},
        {"_book": {"betas": {"BKR": 1.1}}}, reasons)
    assert any(r.startswith("portfolio_beta_unverifiable") for r in reasons)


def test_an_oversized_unknown_beta_position_still_trips_the_ceiling():
    reasons: list[str] = []
    validator._check_portfolio_beta(
        {"action": "BUY", "ticker": "WILD", "position_size_usd": 5000.0},
        _BETA_CFG, {"total_equity_usd": 1000.0, "positions": []},
        {"_book": {"betas": {"SPY": 1.0}}}, reasons)
    assert any(r.startswith("portfolio_beta_cap_exceeded") for r in reasons)
