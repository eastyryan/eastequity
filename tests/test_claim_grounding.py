"""Offline tests for validator claim grounding — the fabricated-catalyst net.

Everything here is pure: a synthetic context bundle in, reason strings out. No
network, no LLM, no file IO beyond the config the validator already loads.

The cases are not invented. Each fabrication below is lifted verbatim from a
real risk-desk veto in journal/rejected/, because those are the failures this
gate exists to make deterministic:

  * DDOG 2026-07-22 — "JMP Securities raised its target to $311 from $225".
    The desk's veto: "this string, and any variant of 'JMP', appears ZERO times
    anywhere in the full context bundle".
  * MELI 2026-07-24 — a "Loggi acquisition", a Citi fair-value cut and "14 new
    Brazil fulfillment centers", none of them in the bundle.

The two properties that matter most, and that the suite pins hard:
  (a) a fabricated entity is CAUGHT, and
  (b) the check FAILS OPEN — no bundle, no claim text, or a figure the brain
      derived itself must never produce a rejection. A false rejection costs a
      real trade, which on this book is the more expensive error.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def bundle(**extra):
    """A minimal but realistically-shaped context bundle."""
    b = {
        "sec_filings": {
            "DDOG": {"company": "Datadog, Inc."},
            "MELI": {"company": "MERCADOLIBRE INC"},
            "SNOW": {"company": "Snowflake Inc."},
        },
        "news_and_catalysts": {
            "tickers": {
                "DDOG": {"headlines": [
                    {"title": "Datadog (DDOG) Stock Sinks As Market Gains",
                     "source": "Zacks", "subject": "direct"},
                ]},
                "MELI": {"headlines": [
                    {"title": "MercadoLibre's GMV Growth Is Accelerating",
                     "source": "Simply Wall St.", "subject": "direct"},
                ]},
            }
        },
        "analyst_ratings": {"DDOG": {"target_mean_price": 264.99, "n_analysts": 46}},
    }
    b.update(extra)
    return b


def mc(b=None):
    return {"_bundle": b if b is not None else bundle()}


def buy(ticker, variant_perception, catalysts=None):
    return {"action": "BUY", "ticker": ticker,
            "variant_perception": variant_perception,
            "catalysts": catalysts if catalysts is not None else []}


def grounding_reasons(p, context, enforce=True):
    """Run the blocking gate with the flag forced, so the test pins the LOGIC
    rather than the current default (which ships off pending live evidence)."""
    cfg = validator.load_config()
    cfg.setdefault("trade_quality_requirements", {})["enforce_claim_grounding"] = enforce
    reasons = []
    validator._check_claim_grounding(p, cfg, context, reasons)
    return reasons


# --------------------------------------------------------------------------- #
# The real fabrications
# --------------------------------------------------------------------------- #
def test_ddog_fabricated_analyst_target_is_caught():
    """The 2026-07-22 incident: an entire thesis resting on a sell-side note
    that does not exist. 'JMP' appears nowhere in the bundle."""
    p = buy("DDOG",
            "Consensus treats DDOG as a decelerating observability name. "
            "JMP Securities raised its target to $311 from $225 on 2026-07-22, "
            "and the market has not repriced the infra-vs-apps split. "
            "Resolution: the Q3 print on 2026-11-06.",
            ["JMP Securities raised its target to $311 from $225, 2026-07-22"])
    reasons = grounding_reasons(p, mc())
    assert any("unsourced_claim" in r for r in reasons)
    assert any("JMP" in r for r in reasons)


def test_meli_fabricated_deal_and_firm_are_caught():
    """The 2026-07-24 incident: 'Loggi', 'Citi' and 'Brazil' all absent."""
    p = buy("MELI",
            "Consensus reads the margin dip as structural. The Loggi "
            "acquisition and 14 new Brazil fulfillment centers explain it as "
            "provisioning, and Citi cut fair value to $1,750. "
            "Resolution: the Q3 print on 2026-11-05.",
            ["Loggi acquisition closes"])
    flagged = " ".join(grounding_reasons(p, mc()))
    assert "Loggi" in flagged


def test_entity_present_in_the_bundle_is_not_flagged():
    """The mirror image: the SAME shape of claim, but the firm really is in the
    evidence. This is what separates a grounding check from a keyword ban."""
    b = bundle()
    b["news_and_catalysts"]["tickers"]["DDOG"]["headlines"].append(
        {"title": "JMP Securities raises Datadog target to $311",
         "source": "Benzinga", "subject": "direct"})
    p = buy("DDOG",
            "Consensus treats DDOG as decelerating. JMP Securities raised its "
            "target to $311 from $225 on 2026-07-22, and the tape has not "
            "repriced it. Resolution: the Q3 print on 2026-11-06.",
            ["JMP Securities target raise"])
    assert grounding_reasons(p, mc(b)) == []


# --------------------------------------------------------------------------- #
# Fail-open — the expensive direction to get wrong
# --------------------------------------------------------------------------- #
def test_no_bundle_never_rejects():
    """The validator is called from tests and tools with a bare context.
    Rejecting every BUY because the evidence was not handed over would be a
    worse failure than the fabrication this gate exists to catch."""
    p = buy("DDOG", "JMP Securities raised its target to $311 on 2026-07-22. " * 3)
    assert grounding_reasons(p, {}) == []
    assert grounding_reasons(p, {"_bundle": {}}) == []


def test_brain_derived_figures_are_not_claims():
    """The brain's OWN target and stop are supposed to be absent from the
    bundle — they are its output, not its evidence. Flagging them would reject
    every well-formed proposal ever written."""
    p = buy("SNOW",
            "Consensus has Snowflake decelerating after the last guide. My "
            "target is $340 against a stop at $250, a spread the tape does not "
            "price. Resolution: the Q3 print on 2026-11-20.")
    assert grounding_reasons(p, mc()) == []


def test_flag_off_is_advisory_only():
    """Ships default-off pending live evidence (see autonomy_config
    _claim_grounding_note); the signal still reaches the risk desk."""
    p = buy("DDOG", "JMP Securities raised its target to $311 from $225 on "
                    "2026-07-22 and the market has not repriced it yet.",
            ["JMP Securities raised its target"])
    assert grounding_reasons(p, mc(), enforce=False) == []
    # ...but the desk-facing helper still reports it.
    assert validator.unsourced_claims(p, mc())


def test_sells_and_holds_are_never_grounded():
    """Exit paths must never be blocked by a thesis-text check."""
    for action in ("SELL_TO_CLOSE", "HOLD"):
        p = {"action": action, "ticker": "DDOG",
             "variant_perception": "JMP Securities raised its target to $311.",
             "catalysts": ["Loggi acquisition"]}
        assert grounding_reasons(p, mc()) == []
        assert validator.unsourced_claims(p, mc()) == []


# --------------------------------------------------------------------------- #
# Precision — tuned against the 7 real BUY proposals in the journal
# --------------------------------------------------------------------------- #
def test_prose_fragments_are_not_treated_as_entities():
    """The bare proper-noun pass produced 'MINIMUM CONTRACTED', 'Case After
    Strong' and 'Agreement Strengthens Deal' from real proposals — capitalized
    prose that no bundle contains verbatim. Multi-word candidates are therefore
    only taken from an explicit attribution pattern."""
    p = buy("SNOW",
            "Consensus underrates the backlog. MINIMUM CONTRACTED revenue and "
            "CASH DEPOSITS are Strategic Customer Agreements the Street reads "
            "as one-off. Resolution: the Q3 print on 2026-11-20.")
    assert grounding_reasons(p, mc()) == []


def test_ticker_and_company_name_are_exempt():
    """A proposal naming its own subject is not citing a third party."""
    p = buy("MELI",
            "Consensus misreads MERCADOLIBRE margins. MercadoLibre is "
            "absorbing provisioning the Street books as structural. "
            "Resolution: the Q3 print on 2026-11-05.")
    assert grounding_reasons(p, mc()) == []


# --------------------------------------------------------------------------- #
# Self-poisoning — the bug this gate shipped with and had to be cured of
# --------------------------------------------------------------------------- #
def test_config_text_in_the_bundle_cannot_ground_a_claim():
    """THE REGRESSION. The bundle carries `hard_limits`, a verbatim dump of
    autonomy_config.json — and that config's note documenting THIS FEATURE quotes
    the incident it was built from ("JMP Securities raised its target to $311").

    So on the first live bundle after shipping, the fabricated DDOG claim matched
    the documentation of its own fabrication and the gate reported it clean. Any
    section carrying our own prose — config, prompt scaffolding, prior theses —
    lets the brain ground a claim against something WE wrote instead of something
    the world reported. Only the evidence allowlist may be searched."""
    b = bundle()
    b["hard_limits"] = {
        "_claim_grounding_note": "Built from DDOG 07-22 ('JMP Securities raised "
                                 "its target to $311' - appears ZERO times)."
    }
    b["reasoning_process"] = {"note": "Do not fabricate a Loggi-style catalyst."}
    p = buy("DDOG",
            "Consensus treats DDOG as decelerating. JMP Securities raised its "
            "target to $311 from $225 on 2026-07-22. Resolution: Q3 2026-11-06.",
            ["JMP Securities raised its target to $311"])
    flagged = " ".join(grounding_reasons(p, mc(b)))
    assert "JMP" in flagged, "config prose grounded a fabricated claim again"


def test_a_bundle_with_no_evidence_sections_fails_open():
    """A bundle carrying only config/scaffolding is not evidence. With nothing
    citable handed over, the gate must decline rather than flag everything."""
    p = buy("DDOG", "JMP Securities raised its target to $311 on 2026-07-22 and "
                    "the market has not repriced that at all yet.",
            ["JMP Securities target raise"])
    assert grounding_reasons(p, mc({"hard_limits": {"x": 1}})) == []
