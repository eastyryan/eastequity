"""The falsifiability fields must contain something falsifiable.

THE GAP (audited 2026-07-19)
----------------------------
CLAUDE.md specifies variant_perception as four parts — CONSENSUS cited from the
bundle, MY VIEW, MECHANISM, and a RESOLUTION with a date — and calls the field
"validator-enforced" in TWO places. It was enforced by exactly one thing:

    if f not in p or p[f] in (None, ""):        # _check_required_fields

A single non-space character satisfied it. A grep confirmed `variant_perception`
appeared nowhere else in validator.py.

thesis_invalidators was presence + length only, at min_invalidator_chars = 12, so
"drops below 50dma" (17 chars) passed as a binding falsifier — while CLAUDE.md's
own example, "estimate cuts >5%", carries a metric AND a threshold.

scenarios was required PRESENT and never validated: probabilities need not have
summed to 1, and tools/scenario_ev.py — which computes honest probability-
weighted EV over exactly these numbers — had only TWO callers, runlib/analytics
and runlib/publish. Both rendering paths. The arithmetic that catches
narrative-fitting was computed and thrown at a dashboard.

WHAT THESE CHECKS DO AND DO NOT DO
    They check SHAPE, not argument quality. No regex grades a thesis, and the
    risk desk and the reader remain the judges of that. But a field the prompt
    twice calls enforced should not be satisfiable by one character, and a
    proposal whose own probability-weighted return is negative fails by its own
    arithmetic.
"""

from __future__ import annotations

import pytest

import validator

CFG = validator.load_config()

GOOD_VP = (
    "Consensus models a flat multiple into FY2027 and the sell-side mean target "
    "sits 4% below spot; I think estimates lag the order book by a full quarter. "
    "The mechanism is that backlog conversion is booked on shipment, so revenue "
    "already contracted does not appear until the next print. Resolves at the Q3 "
    "earnings report on 2026-08-27.")


def _buy(**over) -> dict:
    p = {
        "ticker": "NVDA", "action": "BUY", "entry_price_max": 100.0,
        "variant_perception": GOOD_VP,
        "scenarios": {"bull": {"price": 125, "prob": 0.3},
                      "base": {"price": 112, "prob": 0.5},
                      "bear": {"price": 92, "prob": 0.2}},
        "thesis_invalidators": {
            "invalidating_print": "Estimate cuts greater than 5% for FY2027",
            "invalidating_structure": "Close below the 50-DMA on rising volume",
            "time_box": "If no reclaim within 25 trading days, exit",
        },
        "holding_horizon_days": 30,
    }
    p.update(over)
    return p


def _reasons(p, check) -> list:
    out: list = []
    check(p, CFG, out)
    return out


# ---------------------------------------------------------------------------
# variant_perception
# ---------------------------------------------------------------------------

def test_a_compliant_variant_perception_passes():
    assert _reasons(_buy(), validator._check_variant_perception) == []


def test_one_character_no_longer_satisfies_the_field():
    """THE BUG, verbatim: `p[f] in (None, "")` was the whole enforcement."""
    r = _reasons(_buy(variant_perception="y"), validator._check_variant_perception)
    assert any("too_thin" in x for x in r), r


def test_boilerplate_without_a_figure_is_rejected():
    """CONSENSUS is spec'd as cited from the bundle — a target, a multiple, a
    rating. Prose with no number has not cited anything."""
    vp = ("Consensus believes the multiple is fair and I believe it is not, because "
          "the market is underrating the mechanism by which demand converts into "
          "revenue over the coming period, which will become apparent in due course "
          "when the company next reports and the numbers speak for themselves.")
    r = _reasons(_buy(variant_perception=vp), validator._check_variant_perception)
    assert "variant_perception_cites_no_figure" in r, r


def test_no_dated_resolution_is_rejected():
    vp = ("Consensus models a flat multiple and the mean target sits 4% below spot; "
          "I think estimates lag the order book. The mechanism is that backlog "
          "conversion is booked on shipment, so contracted revenue does not appear "
          "in the reported figures until later. It will resolve eventually.")
    r = _reasons(_buy(variant_perception=vp), validator._check_variant_perception)
    assert "variant_perception_has_no_dated_resolution" in r, r


def test_sells_are_not_subject_to_the_check():
    p = _buy(action="SELL_TO_CLOSE", variant_perception="y")
    assert _reasons(p, validator._check_variant_perception) == []


# ---------------------------------------------------------------------------
# thesis_invalidators shape
# ---------------------------------------------------------------------------

def test_invalidating_print_needs_a_threshold():
    """'drops below 50dma' was 17 chars and passed at min_invalidator_chars=12."""
    inv = dict(_buy()["thesis_invalidators"])
    inv["invalidating_print"] = "Guidance disappoints the market badly"
    r = _reasons(_buy(thesis_invalidators=inv), validator._check_thesis_invalidators)
    assert any("no_threshold" in x for x in r), r


def test_time_box_needs_a_date_or_a_count():
    inv = dict(_buy()["thesis_invalidators"])
    inv["time_box"] = "Exit if the thesis has not started working soon enough"
    r = _reasons(_buy(thesis_invalidators=inv), validator._check_thesis_invalidators)
    assert any("no_date" in x for x in r), r


@pytest.mark.parametrize("tb", [
    "If no reclaim within 25 trading days, exit",   # count with an intervening word
    "Exit by 2026-09-30 if the guide has not landed",
    "Cut if unresolved by Q3",
    "Give it 6 to 8 weeks from entry",
])
def test_real_time_boxes_are_accepted(tb):
    """The check must not reject the plainest ways of writing a time box."""
    inv = dict(_buy()["thesis_invalidators"])
    inv["time_box"] = tb
    r = _reasons(_buy(thesis_invalidators=inv), validator._check_thesis_invalidators)
    assert not any("no_date" in x for x in r), (tb, r)


# ---------------------------------------------------------------------------
# scenario arithmetic
# ---------------------------------------------------------------------------

def test_probabilities_must_sum_to_one():
    sc = {"bull": {"price": 125, "prob": 0.6},
          "base": {"price": 112, "prob": 0.6},
          "bear": {"price": 92, "prob": 0.6}}
    r = _reasons(_buy(scenarios=sc), validator._check_scenarios)
    assert any("do_not_sum_to_1" in x for x in r), r


def test_bull_must_be_above_entry_and_bear_below():
    sc = {"bull": {"price": 90, "prob": 0.3},
          "base": {"price": 112, "prob": 0.5},
          "bear": {"price": 130, "prob": 0.2}}
    r = _reasons(_buy(scenarios=sc), validator._check_scenarios)
    assert "scenario_bull_not_above_entry" in r
    assert "scenario_bear_not_below_entry" in r


def test_negative_expected_value_fails_by_its_own_arithmetic():
    """The sharpest check: the proposal's own numbers say it loses money."""
    sc = {"bull": {"price": 105, "prob": 0.1},
          "base": {"price": 99, "prob": 0.2},
          "bear": {"price": 70, "prob": 0.7}}
    r = _reasons(_buy(scenarios=sc), validator._check_scenarios)
    assert any("expected_value_not_positive" in x for x in r), r


def test_prose_only_scenarios_are_rejected():
    """The legacy {"base": "Plays out."} shape carries no numbers, so no EV math
    is possible and CLAUDE.md's schema is explicit about price/prob."""
    sc = {"base": "Plays out.", "bull": "Expands.", "bear": "Reverts."}
    r = _reasons(_buy(scenarios=sc), validator._check_scenarios)
    assert r, "a scenarios block with no numbers must not pass silently"


def test_a_sound_scenario_block_passes():
    assert _reasons(_buy(), validator._check_scenarios) == []


# ---------------------------------------------------------------------------
# wired into the real pipeline, not just callable
# ---------------------------------------------------------------------------

def test_checks_are_reachable_from_validate_proposals():
    """A check nobody calls is decoration — the failure this repo keeps hitting."""
    import inspect
    src = inspect.getsource(validator.validate_proposals)
    assert "_check_variant_perception" in src
    assert "_check_scenarios" in src
