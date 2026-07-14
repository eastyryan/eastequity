"""Offline tests for tools.scenario_ev (pure stdlib, no network).

    .venv/bin/python tests/test_scenario_ev.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.scenario_ev import expected_value  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# CLAUDE.md's NVDA schema example: entry 190, horizon 30 days.
NVDA = {"bull": {"price": 240, "prob": 0.3},
        "base": {"price": 215, "prob": 0.45},
        "bear": {"price": 170, "prob": 0.25}}
# Hand-computed:
#   ev      = (0.3*50 + 0.45*25 - 0.25*20) / 190 = 21.25/190 = 0.111842... -> 11.18%
#   ann     = 0.111842 * 365/30 = 1.36075...                 -> 136.07%
#   upside  = (0.3*50 + 0.45*25) / 190 = 26.25/190           -> 13.82%
#   downside= -0.25*20 / 190 = -5/190                        -> -2.63%
#   ratio   = 26.25 / 5 = 5.25;  prob_sum = 1.0; no warning


def test_nvda_example():
    print("NVDA example (hand-computed):")
    out = expected_value(NVDA, 190, 30)
    check("ev_pct 11.18", out["ev_pct"] == 11.18, str(out["ev_pct"]))
    check("ev_annualized_pct 136.07", out["ev_annualized_pct"] == 136.07,
          str(out["ev_annualized_pct"]))
    check("prob_weighted_upside_pct 13.82",
          out["prob_weighted_upside_pct"] == 13.82,
          str(out["prob_weighted_upside_pct"]))
    check("prob_weighted_downside_pct -2.63 (negative)",
          out["prob_weighted_downside_pct"] == -2.63,
          str(out["prob_weighted_downside_pct"]))
    check("upside_downside_ratio 5.25",
          out["upside_downside_ratio"] == 5.25,
          str(out["upside_downside_ratio"]))
    check("prob_sum 1.0", out["prob_sum"] == 1.0)
    check("no warning", out["warning"] is None, str(out["warning"]))


def test_string_coercion():
    print("string-number coercion:")
    stringy = {"bull": {"price": "240", "prob": "0.3"},
               "base": {"price": "215", "prob": "0.45"},
               "bear": {"price": "170", "prob": "0.25"}}
    out = expected_value(stringy, "190", "30")
    check("strings coerce to same ev_pct", out["ev_pct"] == 11.18, str(out["ev_pct"]))
    check("strings coerce to same annualized",
          out["ev_annualized_pct"] == 136.07, str(out["ev_annualized_pct"]))


def test_prob_sum_warning():
    print("probability-sum warning:")
    low = {"bull": {"price": 240, "prob": 0.3},
           "base": {"price": 215, "prob": 0.45},
           "bear": {"price": 170, "prob": 0.15}}  # sums to 0.9
    out = expected_value(low, 190, 30)
    check("warning fired below 0.95",
          out["warning"] == "scenario_probs_sum_to_0.9", str(out["warning"]))
    check("prob_sum reported", out["prob_sum"] == 0.9, str(out["prob_sum"]))
    check("values still computed alongside warning", out["ev_pct"] is not None)
    high = {"bull": {"price": 240, "prob": 0.5},
            "base": {"price": 215, "prob": 0.45},
            "bear": {"price": 170, "prob": 0.25}}  # sums to 1.2
    out = expected_value(high, 190, 30)
    check("warning fired above 1.05",
          out["warning"] == "scenario_probs_sum_to_1.2", str(out["warning"]))
    ok = expected_value(NVDA, 190, 30)
    check("sum 1.0 inside tolerance -> no warning", ok["warning"] is None)


def _all_values_none(out):
    keys = ("ev_pct", "ev_annualized_pct", "prob_weighted_upside_pct",
            "prob_weighted_downside_pct", "upside_downside_ratio", "prob_sum")
    return all(out[k] is None for k in keys)


def test_missing_scenarios():
    print("missing scenarios:")
    for bad in ({}, None, [], {"bull": "not-a-dict"},
                {"bull": {"price": "n/a", "prob": None}},
                {"bull": {"price": -240, "prob": 0.3}}):
        out = expected_value(bad, 190, 30)
        check(f"unusable scenarios {bad!r} -> all None + warning",
              _all_values_none(out) and out["warning"] == "missing_scenarios",
              str(out["warning"]))


def test_bad_entry_price():
    print("bad entry price:")
    for bad in (0, -5, None, "junk"):
        out = expected_value(NVDA, bad, 30)
        check(f"entry {bad!r} -> all None + warning",
              _all_values_none(out) and out["warning"] == "bad_entry_price",
              str(out["warning"]))


def test_annualization_gating():
    print("annualization gating (one stated horizon, no ladders):")
    for horizon, expect_none in ((2, True), (3, False), (30, False),
                                 (365, False), (366, True), (None, True),
                                 ("nope", True)):
        out = expected_value(NVDA, 190, horizon)
        ok = (out["ev_annualized_pct"] is None) == expect_none
        check(f"horizon {horizon!r} -> annualized {'None' if expect_none else 'set'}",
              ok and out["ev_pct"] == 11.18, str(out["ev_annualized_pct"]))


def test_no_downside():
    print("no-downside book:")
    up_only = {"bull": {"price": 120, "prob": 0.5},
               "base": {"price": 110, "prob": 0.5}}
    out = expected_value(up_only, 100, 30)
    check("downside 0 when no scenario below entry",
          out["prob_weighted_downside_pct"] == 0.0,
          str(out["prob_weighted_downside_pct"]))
    check("ratio None when downside is 0",
          out["upside_downside_ratio"] is None,
          str(out["upside_downside_ratio"]))
    flat = {"base": {"price": 100, "prob": 1.0}}
    out = expected_value(flat, 100, 30)
    check("price == entry -> ev 0, ratio None",
          out["ev_pct"] == 0.0 and out["upside_downside_ratio"] is None)


def test_partial_junk_rows():
    print("partial junk rows:")
    mixed = {"bull": {"price": 240, "prob": 0.3},
             "base": {"price": 215, "prob": 0.45},
             "bear": {"price": 170, "prob": 0.25},
             "moon": {"price": "n/a", "prob": 0.9},      # dropped: bad price
             "doom": {"price": 100, "prob": -0.5}}       # dropped: negative prob
    out = expected_value(mixed, 190, 30)
    check("junk rows dropped, clean rows kept",
          out["ev_pct"] == 11.18 and out["prob_sum"] == 1.0,
          f"ev={out['ev_pct']} sum={out['prob_sum']}")


if __name__ == "__main__":
    for fn in (test_nvda_example, test_string_coercion, test_prob_sum_warning,
               test_missing_scenarios, test_bad_entry_price,
               test_annualization_gating, test_no_downside,
               test_partial_junk_rows):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All scenario-EV checks passed.")
