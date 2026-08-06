"""parse_proposals whitelist tests (runlib/brain_io.py, 2026-08-05 rework).

THE FOOTGUN THIS PINS: the output whitelist was purely hand-maintained — a key
absent from it was silently dropped no matter what the brain wrote, which is
how seat_reviews/factor_response vanished until 2d0e46e and caused the 07-30
process_gate_blocked incident (CRWD and ITW died that day). The rework derives
the pass-through set from autonomy_config's required_proposal_fields +
required_sell_fields UNION OPTIONAL_PROPOSAL_FIELDS, so a registered schema
field can never silently vanish again.

These tests assert BOTH directions: (1) every key of the OLD hand list keeps
its exact pre-rework handling (defaults, caps, shape guards — the new allowed
set must be a strict SUPERSET of the old one), and (2) a field present in the
config's required lists but absent from the old hand list now passes through.

Pure Python, no network, no state/ writes.

    EE_BROKER=simulation python3 tests/test_parse_whitelist.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("EE_BROKER", "simulation")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib.brain_io import (  # noqa: E402
    OPTIONAL_PROPOSAL_FIELDS,
    _allowed_output_keys,
    parse_proposals,
)
import runlib.brain_io as BIO  # noqa: E402
import validator  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# The hand list EXACTLY as it stood before the 2026-08-05 rework. Every key
# here must keep its old handling; the rework may only ADD allowed keys.
OLD_HAND_LIST = (
    "proposals", "no_trade_reason", "commentary", "watchlist",
    "rejected_ideas", "x_post", "guidance_entries", "universe_candidates",
    "seat_reviews", "factor_response", "trigger_reviews", "waiting_for",
)


def _fenced(payload: str) -> str:
    return "prose before\n```json\n" + payload + "\n```\nprose after"


def test_old_hand_list_survives():
    print("old hand list: every key keeps its pre-rework handling:")
    resp = _fenced("""{
        "proposals": [{"ticker": "NVDA", "action": "BUY"}, "not-a-dict"],
        "no_trade_reason": null,
        "commentary": "words",
        "watchlist": [%s],
        "rejected_ideas": [{"ticker": "MU", "reason": "r"}],
        "x_post": "post text",
        "guidance_entries": [{"ticker": "DELL", "metric": "revenue"}],
        "universe_candidates": [%s],
        "seat_reviews": [{"ticker": "NET", "action": "keep"}],
        "factor_response": {"concentration_level": "high"},
        "trigger_reviews": [{"ticker": "ANET", "action": "drop"}],
        "waiting_for": {"ticker": "MPC"}
    }""" % (
        ",".join('{"ticker": "W%d"}' % i for i in range(12)),
        ",".join('{"ticker": "U%d"}' % i for i in range(7)),
    ))
    parsed = parse_proposals(resp)
    for k in OLD_HAND_LIST:
        check(f"key survives: {k}", k in parsed)
    check("proposals shape-guarded (non-dict entries filtered)",
          parsed["proposals"] == [{"ticker": "NVDA", "action": "BUY"}])
    check("watchlist still capped at 10", len(parsed["watchlist"]) == 10)
    check("universe_candidates still capped at 5",
          len(parsed["universe_candidates"]) == 5)
    check("guidance_entries list-guarded",
          parsed["guidance_entries"] == [{"ticker": "DELL", "metric": "revenue"}])
    check("seat_reviews passes through untouched",
          parsed["seat_reviews"] == [{"ticker": "NET", "action": "keep"}])
    check("factor_response passes through untouched",
          parsed["factor_response"] == {"concentration_level": "high"})
    check("waiting_for passes through untouched",
          parsed["waiting_for"] == {"ticker": "MPC"})
    check("no_trade_reason null preserved", parsed["no_trade_reason"] is None)
    # A response carrying ONLY the old keys emits EXACTLY the old key set, in
    # the old order — the rework must not add keys to a legacy-shaped response.
    check("legacy-shaped response emits exactly the old key set, old order",
          list(parsed) == list(OLD_HAND_LIST), str(list(parsed)))


def test_defaults_when_absent():
    print("old defaults when keys are absent from the block:")
    parsed = parse_proposals(_fenced('{"proposals": []}'))
    check("all old keys present with defaults",
          list(parsed) == list(OLD_HAND_LIST), str(list(parsed)))
    check("list defaults", parsed["watchlist"] == [] and
          parsed["rejected_ideas"] == [] and parsed["guidance_entries"] == []
          and parsed["universe_candidates"] == [])
    check("scalar defaults None", parsed["seat_reviews"] is None
          and parsed["factor_response"] is None
          and parsed["trigger_reviews"] is None
          and parsed["waiting_for"] is None)


def test_fallback_path_unchanged():
    print("no machine-readable block: graceful fallback unchanged:")
    parsed = parse_proposals("no json anywhere in this response")
    check("fallback keys are exactly the old set, old order",
          list(parsed) == list(OLD_HAND_LIST), str(list(parsed)))
    check("fallback proposals empty + public-facing reason",
          parsed["proposals"] == [] and "no orders were placed"
          in (parsed["no_trade_reason"] or ""))


def test_allowed_set_is_schema_derived_superset():
    print("allowed set: superset of old list, derived from live config:")
    allowed = set(OLD_HAND_LIST) | _allowed_output_keys()
    check("old hand list is fully contained (superset relationship)",
          set(OLD_HAND_LIST) <= allowed)
    q = validator.load_config()["trade_quality_requirements"]
    required = set(q["required_proposal_fields"]) | set(
        q.get("required_sell_fields") or [])
    check("every config-required field is allowed",
          required <= _allowed_output_keys(),
          str(required - _allowed_output_keys()))
    check("every documented optional field is allowed",
          set(OPTIONAL_PROPOSAL_FIELDS) <= _allowed_output_keys())


def test_config_required_field_passes_through():
    print("a required_proposal_fields entry absent from the old hand list:")
    # Real config first: "ticker" is in required_proposal_fields and was NOT
    # in the old hand list — before the rework it was silently dropped.
    parsed = parse_proposals(_fenced(
        '{"proposals": [], "ticker": "TOP-LEVEL-SURVIVES"}'))
    check("real-config required field now passes through",
          parsed.get("ticker") == "TOP-LEVEL-SURVIVES", str(parsed.keys()))
    # Synthetic config: prove derivation (not hardcoding) — a brand-new field
    # added ONLY to required_proposal_fields flows through with no code change.
    real_load = BIO.validator.load_config
    try:
        BIO.validator.load_config = lambda: {
            "trade_quality_requirements": {
                "required_proposal_fields": ["ticker", "brand_new_gate_field"],
                "required_sell_fields": ["ticker"],
            }
        }
        parsed = parse_proposals(_fenced(
            '{"proposals": [], "brand_new_gate_field": {"x": 1}}'))
        check("field added to config passes through with no code change",
              parsed.get("brand_new_gate_field") == {"x": 1})
    finally:
        BIO.validator.load_config = real_load


def test_optional_fields_pass_and_strays_do_not():
    print("optional fields pass; unregistered keys still dropped:")
    parsed = parse_proposals(_fenced(
        '{"proposals": [], "sell_fraction": 0.5, "earnings_case": "case", '
        '"hallucinated_block": {"x": 1}}'))
    check("OPTIONAL_PROPOSAL_FIELDS entry passes (sell_fraction)",
          parsed.get("sell_fraction") == 0.5)
    check("OPTIONAL_PROPOSAL_FIELDS entry passes (earnings_case)",
          parsed.get("earnings_case") == "case")
    check("unregistered key is still dropped (whitelist is still a whitelist)",
          "hallucinated_block" not in parsed)


def test_config_failure_fails_soft():
    print("config unreadable: parse still works, optional tuple still passes:")
    real_load = BIO.validator.load_config
    try:
        def _boom():
            raise RuntimeError("config unreadable")
        BIO.validator.load_config = _boom
        parsed = parse_proposals(_fenced(
            '{"proposals": [], "sell_fraction": 0.25, "ticker": "X"}'))
        check("parse survives a config failure", parsed["proposals"] == [])
        check("documented optional tuple still passes",
              parsed.get("sell_fraction") == 0.25)
        check("config-derived key is (only) lost while config is down",
              "ticker" not in parsed)
    finally:
        BIO.validator.load_config = real_load


if __name__ == "__main__":
    test_old_hand_list_survives()
    test_defaults_when_absent()
    test_fallback_path_unchanged()
    test_allowed_set_is_schema_derived_superset()
    test_config_required_field_passes_through()
    test_optional_fields_pass_and_strays_do_not()
    test_config_failure_fails_soft()
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURE(S): {FAILURES}'}")
    sys.exit(1 if FAILURES else 0)
