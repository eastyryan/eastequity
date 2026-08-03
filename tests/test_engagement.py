"""Offline tests for engagement discipline — no network.

These cover the half the 2026-08-03 audit found missing: every other fix removed
obstacles, none created a cost to NOT acting. A fired trigger produced exactly
one consequence anywhere in the codebase (a shadow-book row graded weeks later),
so ANET and GE reached their own stated levels across four runs on 07-23/24 for
free, and the book sat 100% cash from 07-16 to 07-28 with nothing noticing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.engagement import (
    count_unbought_hits,
    decay_watchlist,
    deployment_status,
    stale_commitments,
    validate_trigger_reviews,
    validate_waiting_for,
)


# ---------------------------------------------------------------------------
# Trigger accountability
# ---------------------------------------------------------------------------
def test_a_fired_trigger_owes_a_resolution():
    clean, issues = validate_trigger_reviews(None, ["ANET", "GE"])
    assert clean == []
    assert any(i.startswith("trigger_reviews_missing_need_2") for i in issues)


def test_buying_the_name_discharges_the_obligation():
    """The action speaks — no row owed for a trigger you actually acted on."""
    clean, issues = validate_trigger_reviews(None, ["ANET"], ["ANET"])
    assert clean == [] and issues == []


def test_hold_unchanged_is_not_an_available_action():
    clean, issues = validate_trigger_reviews(
        [{"ticker": "ANET", "action": "hold",
          "reason": "still waiting for the volume to confirm the move"}],
        ["ANET"])
    assert clean == []
    assert any(i.startswith("trigger_reviews_action_invalid:ANET") for i in issues)


def test_requalify_without_a_new_level_is_rejected():
    """Requalify with no new trigger is just 'hold' wearing a new name."""
    clean, issues = validate_trigger_reviews(
        [{"ticker": "ANET", "action": "requalify",
          "reason": "the level was wrong but I still want to own this name"}],
        ["ANET"])
    assert clean == []
    assert any(i.startswith("trigger_reviews_requalify_without_new_trigger")
               for i in issues)


def test_a_weak_reason_is_rejected():
    clean, issues = validate_trigger_reviews(
        [{"ticker": "GE", "action": "drop", "reason": "no"}], ["GE"])
    assert clean == []
    assert any(i.startswith("trigger_reviews_reason_weak:GE") for i in issues)


def test_well_formed_resolutions_pass():
    clean, issues = validate_trigger_reviews(
        [{"ticker": "ANET", "action": "requalify",
          "reason": "Reached $175 but the base is shallower than I underwrote.",
          "new_trigger": "$168 or a close back above $180"},
         {"ticker": "GE", "action": "drop",
          "reason": "Third time at my level and I still will not buy it."}],
        ["ANET", "GE"])
    assert issues == []
    assert {r["ticker"] for r in clean} == {"ANET", "GE"}
    assert clean[0]["new_trigger"] == "$168 or a close back above $180"


def test_partial_coverage_names_who_is_missing():
    clean, issues = validate_trigger_reviews(
        [{"ticker": "ANET", "action": "drop",
          "reason": "Thesis never firmed up, removing it from the list."}],
        ["ANET", "GE"])
    assert len(clean) == 1
    assert any("GE" in i for i in issues)


# ---------------------------------------------------------------------------
# Watchlist decay
# ---------------------------------------------------------------------------
def test_unbought_hit_days_are_counted_distinctly():
    hits = count_unbought_hits([
        {"ticker": "ANET", "unbought_hit_days": ["2026-07-23", "2026-07-23",
                                                 "2026-07-24"]},
        {"ticker": "BKR", "unbought_hit_days": ["2026-07-17"], "acted": True},
    ])
    assert hits["ANET"] == 2      # two DAYS, not three runs
    assert "BKR" not in hits      # bought — no longer a miss


def test_a_name_that_keeps_hitting_unbought_is_force_dropped():
    wl = [{"ticker": "ANET", "status": "hold", "would_buy_at": "$175"},
          {"ticker": "BKR", "status": "hold", "would_buy_at": "$58"}]
    kept, dropped = decay_watchlist(wl, {"ANET": 3, "BKR": 1},
                                    max_unbought_hits=3)
    assert [w["ticker"] for w in kept] == ["BKR"]
    assert dropped[0]["ticker"] == "ANET" and dropped[0]["unbought_hits"] == 3


def test_a_name_being_promoted_to_buy_is_never_decayed():
    wl = [{"ticker": "ANET", "status": "buy", "would_buy_at": "$175"}]
    kept, dropped = decay_watchlist(wl, {"ANET": 9}, max_unbought_hits=3)
    assert len(kept) == 1 and dropped == []


# ---------------------------------------------------------------------------
# Cash-drag accountability
# ---------------------------------------------------------------------------
def test_a_deployed_book_owes_nothing():
    st = deployment_status(
        [{"ticker": "BKR", "market_value_usd": 60.0}], 984.0, None, "2026-08-03")
    assert not st["flat"] and not st["requires_commitment"]
    assert st["open_positions"] == 1


def test_flat_days_accumulate_and_then_require_a_commitment():
    st = deployment_status([], 1000.0, None, "2026-07-16",
                           commitment_after_days=3)
    assert st["flat"] and st["flat_days"] == 1
    assert not st["requires_commitment"]

    later = deployment_status([], 1000.0, st, "2026-07-19",
                              commitment_after_days=3)
    assert later["flat_days"] == 4
    assert later["requires_commitment"]


def test_many_runs_in_one_day_advance_the_counter_once():
    st = deployment_status([], 1000.0, None, "2026-07-16")
    again = deployment_status([], 1000.0, st, "2026-07-16")
    assert again["flat_days"] == st["flat_days"] == 1


def test_deploying_capital_resets_the_counter():
    flat = deployment_status([], 1000.0, None, "2026-07-27")
    assert flat["flat"]
    now = deployment_status([{"ticker": "BKR", "market_value_usd": 60.0}],
                            984.0, flat, "2026-07-28")
    assert not now["flat"] and now["flat_since"] is None


def test_a_commitment_needs_a_date_not_just_a_condition():
    got, issues = validate_waiting_for(
        {"ticker": "MPC", "condition": "waiting for a better entry on the pullback"},
        required=True)
    assert got is None and "waiting_for_missing_by_date" in issues


def test_a_dated_commitment_passes():
    got, issues = validate_waiting_for(
        {"ticker": "MPC",
         "condition": "Enter on the post-print reaction if the 8/4 guide holds",
         "by_date": "2026-08-08"}, required=True)
    assert issues == [] and got["by_date"] == "2026-08-08"


def test_no_commitment_owed_when_not_required():
    got, issues = validate_waiting_for(None, required=False)
    assert issues == [] and got is None


def test_expired_commitments_come_back():
    overdue = stale_commitments(
        [{"ticker": "MPC", "condition": "reclaim $182", "by_date": "2026-07-28"}],
        "2026-08-03")
    assert len(overdue) == 1 and overdue[0]["days_overdue"] == 6


def test_a_live_commitment_is_not_flagged():
    assert stale_commitments(
        [{"ticker": "MPC", "condition": "reclaim $182", "by_date": "2026-08-09"}],
        "2026-08-03") == []
