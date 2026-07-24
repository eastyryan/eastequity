"""Trend semantics on quality_ratios: "unknown" is not "flat".

THE BUG THIS PINS (2026-07-23). `_trend()` returned "flat" when the delta was None —
i.e. when there was no prior period to compare against — and 23 call sites hardcoded the
literal string "trend": "flat" without comparing anything at all. Measured on a live
name, 17 of 20 ratios reported "flat" having done no comparison. A brain reading
`operating_margin_ttm_pct: {value: 5.21, trend: "flat"}` concludes margins are stable;
the truth was that margins had never been checked. On real MPC data that same field is
`deteriorating`, 5.21% against 7.10% a year earlier.

Same failure shape as an absent days_to_earnings reading as "no print imminent": a
not-measured value wearing the costume of a measured one. These tests keep the two
distinguishable.

Pure — no network, no filings, no clock.
"""
from tools.fundamentals import _level_trend, _margin_trend, _trend


# --------------------------------------------------------------------------- #
# _trend: the root fix
# --------------------------------------------------------------------------- #
def test_none_delta_is_unknown_never_flat():
    """The whole bug in one assertion."""
    assert _trend(None, 0.5) == "unknown"
    assert _trend(None, 0.5, higher_is_better=False) == "unknown"


def test_real_zero_delta_is_still_flat():
    """A measured non-move must remain 'flat' — otherwise the fix destroys the
    signal it was meant to protect."""
    assert _trend(0.0, 0.5) == "flat"
    assert _trend(0.4, 0.5) == "flat"          # inside the band
    assert _trend(-0.4, 0.5) == "flat"


def test_direction_and_polarity():
    assert _trend(2.0, 0.5) == "improving"
    assert _trend(-2.0, 0.5) == "deteriorating"
    # higher_is_better=False inverts (leverage, SBC, inventory)
    assert _trend(2.0, 0.5, higher_is_better=False) == "deteriorating"
    assert _trend(-2.0, 0.5, higher_is_better=False) == "improving"


# --------------------------------------------------------------------------- #
# _margin_trend: TTM margin vs the SAME margin a year earlier
# --------------------------------------------------------------------------- #
def test_margin_compression_is_reported_as_deteriorating():
    """The live MPC case: 5.21% TTM operating margin against 7.10% a year ago was
    being published as 'flat'."""
    tr, note = _margin_trend(521.0, 10000.0, 710.0, 10000.0, label="TTM operating margin")
    assert tr == "deteriorating"
    assert "5.21%" in note and "7.10%" in note and "-1.89pp" in note


def test_margin_expansion_is_reported_as_improving():
    tr, note = _margin_trend(4041.0, 10000.0, 3957.0, 10000.0)
    assert tr == "improving"
    assert "+0.84pp" in note or "+0.83pp" in note


def test_missing_prior_year_is_unknown_and_says_so():
    """A margin with no prior-year TTM must NOT come back 'flat'."""
    for prior_num, prior_den in ((None, 10000.0), (710.0, None), (710.0, 0)):
        tr, note = _margin_trend(521.0, 10000.0, prior_num, prior_den)
        assert tr == "unknown"
        assert "NOT measured" in note


def test_margin_move_inside_the_band_is_flat():
    tr, _ = _margin_trend(1000.0, 10000.0, 1002.0, 10000.0)   # 10.00% vs 10.02%
    assert tr == "flat"


# --------------------------------------------------------------------------- #
# _level_trend: TTM dollars / coverage vs a year earlier
# --------------------------------------------------------------------------- #
def test_level_band_is_relative_not_absolute():
    """$50m of drift means different things on a $200m base and a $6bn one, so the
    flat band is a PERCENT of the prior value."""
    assert _level_trend(250e6, 200e6, flat_band_pct=10.0)[0] == "improving"   # +25%
    assert _level_trend(6.05e9, 6.0e9, flat_band_pct=10.0)[0] == "flat"       # +0.8%


def test_level_missing_or_zero_prior_is_unknown():
    assert _level_trend(100.0, None)[0] == "unknown"
    assert _level_trend(100.0, 0)[0] == "unknown"          # no division by zero
    assert _level_trend(None, 100.0)[0] == "unknown"


def test_level_polarity_inverts_for_bad_is_up_metrics():
    assert _level_trend(200.0, 100.0, higher_is_better=False)[0] == "deteriorating"


def test_negative_prior_uses_magnitude_not_sign():
    """FCF crossing from -100 to +50 is a 150% improvement, not a sign artefact."""
    tr, _ = _level_trend(50.0, -100.0, flat_band_pct=10.0)
    assert tr == "improving"
