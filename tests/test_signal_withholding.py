"""Measured-negative signals must not promote a name on their own.

tools/sepa_trend.py set the precedent: its VCP layer measured no edge under the
book's real stop rules and is deliberately WITHHELD from the brain, because
"shipping a signal that rigorous-looking with that record invites the brain to act
on it". SIGNAL_VALIDATION.md (86,047 observations, 12 years, 182 names) put two more
lanes in the same position, and these tests pin the response.

The earnings-reaction price leg: gap_held, the one read the lane flags as tradeable,
scored -0.07pp ATR-matched, while gap_filled (+0.29) and down_gap (+0.18) - the two
reads the lane calls failures - scored better. All inside noise, so the honest claim
is that the classification discriminates nothing in either direction.

supplier_pullback: removing ONLY the ai_exposure test from the predicate drops
ATR-matched excess from +0.47 to +0.07. The setup contributes nothing; the effect is
the label, and the label is a 2026 classification applied to 2015 bars.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import universe_scanner as US  # noqa: E402


def test_the_measured_verdict_is_stated_in_the_module():
    """A future reader must not have to rediscover this from the report."""
    assert hasattr(US, "EAR_VALIDATION_NOTE")
    note = US.EAR_VALIDATION_NOTE.lower()
    assert "no edge" in note
    assert "descriptive" in note


def test_the_gap_read_alone_no_longer_promotes_a_name():
    """The withholding, asserted on the actual flag logic.

    earnings_reaction_read may still RETURN its read - it is descriptive colour -
    but the scanner must not set post_earnings_drift_candidate without the
    revision leg, which is the untested-but-theoretically-supported half.
    """
    import inspect
    src = inspect.getsource(US.scan_universe)
    assert 'r["post_earnings_drift_candidate"] = bool(ear_flag) and rev_dir == "up"' in src, \
        "the gap read can still promote a name on its own"


def test_the_reaction_read_is_still_produced_as_colour():
    """WITHHOLD the claim, not the observation. The brain should still see WHAT
    happened; it just must not treat it as evidence."""
    flag, read = US.earnings_reaction_read(
        days_since_earnings=3,
        gap_analysis={"events": [{"direction": "up", "filled": False,
                                  "retained_pct": 0.9, "pct": 4.0}]},
        rel_strength_1m_pct=5.0, revision_direction="up")
    assert read is not None, "the descriptive read must survive the withholding"


def test_supplier_pullback_rows_carry_the_lookahead_caveat():
    """The lane keeps shipping as an attention router, with its record attached."""
    import inspect
    src = inspect.getsource(US.scan_universe)
    assert 'r["lane_validation"]' in src
    assert "lookahead" in src.lower()
    assert "ATTENTION ROUTER" in src


def test_claude_md_no_longer_asserts_the_unsupported_claims():
    """The prompt told the brain a filled gap is 'a failed print, not a dip to buy'
    and ranked supplier_pullback by 3-month RS. Twelve years of data support
    neither."""
    doc = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text()
    assert "is a failed print, not a dip to buy" not in doc
    assert "ranked by 3-month relative strength" not in doc
    assert doc.count("MEASURED 2026-07-19") >= 2, "the verdicts must reach the brain"
