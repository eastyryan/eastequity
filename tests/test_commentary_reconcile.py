"""Offline tests for the 2026-08-04 fix — no network.

The brain writes `commentary` in the same JSON block as its proposals, before
the risk desk / validator / executor run, so the prose can announce a trade that
then dies downstream and the dashboard publishes the contradiction: digest says
"I bought DXCM today", open positions shows only BKR.

tools/commentary_reconcile checks the prose against actual fills AFTER execution
and emits a deterministic correction. These tests pin both halves of its
contract: it must catch every shape of the real incident, and it must stay
silent on the market-description prose that shares the same vocabulary.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.commentary_reconcile import (
    reconcile,
    unexecuted_actions,
)


def _veto(ticker, action="BUY", reason="risk_desk_veto: unsourced catalyst."):
    return {"proposal": {"ticker": ticker, "action": action},
            "approved": False, "reasons": [reason]}


def _fill(ticker, action="BUY", status="filled"):
    return {"ticker": ticker, "action": action, "status": status}


# ---------------------------------------------------------------------------
# The incident: DXCM, run 20260804-865280.
# ---------------------------------------------------------------------------
DXCM_COMMENTARY = (
    "I bought a small position in DexCom (DXCM) today -- the maker of continuous "
    "glucose monitors just posted a strong quarter, raised its full-year guidance, "
    "and released positive clinical trial results. My only other holding, Baker "
    "Hughes, is doing fine and I'm holding it -- nothing has broken that thesis."
)


def test_catches_the_dxcm_incident():
    """A vetoed BUY the commentary announced as done must produce a correction."""
    rep = reconcile(DXCM_COMMENTARY, [_veto("DXCM")], [])
    assert rep["issues"] == ["commentary_claimed_unexecuted_trade:DXCM"]
    assert rep["false_claims"][0]["ticker"] == "DXCM"
    assert "vetoed by the risk desk" in rep["correction"]
    assert "This run executed no trades." in rep["correction"]


def test_correction_names_the_real_ground():
    """The ground is read off the rejection, not assumed to be a veto."""
    results = [{"proposal": {"ticker": "HPE", "action": "BUY"}, "approved": False,
                "reasons": ["size_exceeds_pct_cap:1000 > 10% of 9974.77"]}]
    rep = reconcile("We're adding Hewlett Packard Enterprise (HPE) here.", results, [])
    assert "rejected by the validator" in rep["correction"]

    blocked = [{"proposal": {"ticker": "HPE", "action": "BUY"}, "approved": False,
                "reasons": ["process_gate_blocked: seat_reviews missing"]}]
    assert "blocked by a process gate" in reconcile(
        "I bought HPE today.", blocked, [])["correction"]


def test_approved_but_unfilled_reads_differently_from_a_rejection():
    """An approved order that simply did not fill is a different fact."""
    ok = [{"proposal": {"ticker": "NVDA", "action": "BUY"}, "approved": True,
           "reasons": []}]
    rep = reconcile("I bought NVDA today.", ok, [])
    assert "approved but the order did not fill" in rep["correction"]


def test_executed_trade_is_never_corrected():
    """The normal case: the prose is true and nothing is published over it."""
    ok = [{"proposal": {"ticker": "DXCM", "action": "BUY"}, "approved": True,
           "reasons": []}]
    rep = reconcile(DXCM_COMMENTARY, ok, [_fill("DXCM")])
    assert rep["correction"] is None
    assert rep["issues"] == []


def test_buy_and_sell_are_matched_separately():
    """A filled BUY does not excuse a SELL on the same name that never happened."""
    results = [_veto("BKR", "SELL_TO_CLOSE"), {"proposal": {"ticker": "BKR",
               "action": "BUY"}, "approved": True, "reasons": []}]
    rep = reconcile("I sold BKR this morning.", results, [_fill("BKR", "BUY")])
    assert rep["issues"] == ["commentary_claimed_unexecuted_trade:BKR"]


# ---------------------------------------------------------------------------
# Silence. Every string below shares vocabulary with a claim and is not one;
# all four market-description cases are real prose from archived runs.
# ---------------------------------------------------------------------------
def test_market_description_is_not_an_execution_claim():
    quiet = [
        # 20260713-9f805f
        "Two of our three themes played out today: chip and server-hardware stocks "
        "(including our Dell and HPE positions) sold off sector-wide.",
        # 20260724-2fe063
        "I'm watching that four insiders sold about $8.8 million of HPE stock.",
        # 20260724-7cb7cf
        "Momentum leaders are getting sold while the index holds up, so I'm cautious "
        "about HPE.",
        # 20260728-f921ba
        "Watchlist names -- Snowflake, HPE, Arista -- are being sold off hard.",
    ]
    for text in quiet:
        assert reconcile(text, [_veto("HPE")], [])["correction"] is None, text


def test_intent_and_negation_are_not_claims():
    quiet = [
        "I did not buy DXCM today.",
        "I would buy DXCM near $82 but the geometry is wrong.",
        "I plan to add DXCM if it holds this level.",
        "I am proposing a small position in DXCM.",
        "I'm considering DXCM but I have not opened anything.",
        "I looked at DXCM rather than buying it.",
    ]
    for text in quiet:
        assert reconcile(text, [_veto("DXCM")], [])["correction"] is None, text


def test_past_trade_reference_is_not_this_runs_claim():
    """HPE prose from 08-04: a real closed trade being recalled, not announced."""
    text = ("I'm not jumping back into HPE, the exact stock I sold at a loss three "
            "weeks ago, without fresh homework.")
    assert reconcile(text, [_veto("DXCM")], [])["correction"] is None


def test_nothing_unexecuted_means_nothing_to_check():
    """The quiet guard: with no failed proposal there is no contradiction."""
    rep = reconcile("I bought a small position in DexCom today.", [], [])
    assert rep["checked"] is False
    assert rep["correction"] is None


# ---------------------------------------------------------------------------
# Name-only phrasing — the symbol never appears, so tier 1 cannot bind it.
# ---------------------------------------------------------------------------
def test_name_only_claim_is_caught_when_the_run_traded_nothing():
    text = "I bought a small position in DexCom today, sized to the risk budget."
    rep = reconcile(text, [_veto("DXCM")], [])
    assert rep["issues"] == ["commentary_claimed_unexecuted_trade:unnamed"]
    assert "DXCM buy was vetoed by the risk desk" in rep["correction"]


def test_name_only_claim_is_left_alone_when_something_did_fill():
    """With a real fill on the run, an unbound sentence may be describing it."""
    results = [_veto("DXCM"), {"proposal": {"ticker": "BKR", "action": "BUY"},
                               "approved": True, "reasons": []}]
    rep = reconcile("I bought a small position in Baker Hughes today.",
                    results, [_fill("BKR")])
    assert rep["correction"] is None


# ---------------------------------------------------------------------------
# Input shapes and edges.
# ---------------------------------------------------------------------------
def test_accepts_validation_result_objects():
    """Live callers pass ValidationResult objects, archives pass dicts."""
    class R:
        proposal = {"ticker": "MU", "action": "BUY"}
        approved = False
        reasons = ["risk_desk_veto: stale-price chase."]

    assert reconcile("I bought MU today.", [R()], [])["false_claims"]


def test_unfilled_status_does_not_count_as_a_fill():
    pending = [{"ticker": "DXCM", "action": "BUY", "status": "pending"}]
    assert unexecuted_actions([_veto("DXCM")], pending)[0]["ticker"] == "DXCM"


def test_missing_commentary_is_not_an_error():
    for empty in (None, "", 123, {}):
        assert reconcile(empty, [_veto("DXCM")], [])["correction"] is None
