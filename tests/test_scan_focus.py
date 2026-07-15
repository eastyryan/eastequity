"""Offline unit tests for promote_focus_candidates and scan-tier helpers.

No network. Run:
    python3 tests/test_scan_focus.py
    python3 tests/test_universe_scanner.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.universe_scanner import (  # noqa: E402
    promote_focus_candidates,
    scan_universe,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _row(ticker: str, **kw) -> dict:
    base = {
        "ticker": ticker,
        "swing_setup_score": 1.0,
        "liquid": True,
        "post_earnings_drift_candidate": False,
        "analyst_estimates": None,
    }
    base.update(kw)
    return base


def test_scan_universe_signature():
    print("scan_universe signature (tiered depths):")
    sig = inspect.signature(scan_universe)
    params = sig.parameters
    check("top_n default 15", params["top_n"].default == 15)
    check("tickers default None", params["tickers"].default is None)
    check("enrich default True", params["enrich"].default is True)
    check("weekly default False", params["weekly"].default is False)
    # Backward-compat: positional top_n still works
    check("accepts positional top_n",
          list(params)[0] == "top_n")


def test_promote_empty_and_guards():
    print("promote_focus_candidates guards:")
    check("empty scan -> []", promote_focus_candidates({}, [], 2) == [])
    check("max_extra 0 -> []", promote_focus_candidates({"top_setups": [_row("A")]}, [], 0) == [])
    check("non-dict scan -> []", promote_focus_candidates(None, [], 2) == [])  # type: ignore[arg-type]
    check("already covers all -> []",
          promote_focus_candidates(
              {"top_setups": [_row("AAA", post_earnings_drift_candidate=True)]},
              ["AAA"], 2) == [])


def test_promote_prefers_post_earnings_drift():
    print("promote: post_earnings_drift_candidate first:")
    scan = {
        "top_setups": [
            _row("A1", swing_setup_score=5.0),
            _row("A2", swing_setup_score=4.0),
            _row("A3", swing_setup_score=3.0),
            _row("A4", swing_setup_score=2.0),
            _row("A5", swing_setup_score=1.5),
            _row("A6", swing_setup_score=9.0),  # high score beyond first 5
            _row("PED", swing_setup_score=0.5, post_earnings_drift_candidate=True),
        ],
        "contrarian_setups": [],
        "deep_value_200w": [],
        "supplier_pullbacks": [],
    }
    out = promote_focus_candidates(scan, already=["A1", "A2"], max_extra=2)
    check("PED taken first", out[0] == "PED" if out else False, str(out))
    check("second is high-score beyond top5 (A6)", out == ["PED", "A6"], str(out))


def test_promote_beyond_first_five_by_score():
    print("promote: high score among top_setups beyond first 5:")
    top = [
        _row(f"T{i}", swing_setup_score=10 - i) for i in range(1, 6)
    ] + [
        _row("LOW", swing_setup_score=1.0),
        _row("HIGH", swing_setup_score=8.0),
        _row("MID", swing_setup_score=4.0),
    ]
    scan = {
        "top_setups": top,
        "contrarian_setups": [],
        "deep_value_200w": [],
        "supplier_pullbacks": [],
    }
    # already takes none of beyond-5; should pick HIGH then MID by score
    out = promote_focus_candidates(scan, already=["T1", "T2", "T3", "T4", "T5"], max_extra=2)
    check("picks HIGH then MID", out == ["HIGH", "MID"], str(out))
    # already includes HIGH
    out2 = promote_focus_candidates(scan, already=["HIGH"], max_extra=1)
    check("skips already HIGH", out2 == ["MID"] or out2 == ["LOW"] or "HIGH" not in out2, str(out2))
    check("next after HIGH is MID", out2[0] == "MID", str(out2))


def test_promote_eps_revision_up_liquid():
    print("promote: liquid + eps_revision_direction up:")
    scan = {
        "top_setups": [
            _row("ILLIQ", liquid=False,
                 analyst_estimates={"eps_revision_direction": "up"}),
            _row("EPSUP", liquid=True,
                 analyst_estimates={"eps_revision_direction": "up"}),
            _row("EPSDN", liquid=True,
                 analyst_estimates={"eps_revision_direction": "down"}),
        ],
        "contrarian_setups": [],
        "deep_value_200w": [],
        "supplier_pullbacks": [],
    }
    out = promote_focus_candidates(scan, already=[], max_extra=2)
    check("takes liquid EPS-up only", out == ["EPSUP"], str(out))
    check("skips illiquid EPS-up", "ILLIQ" not in out)
    check("skips EPS-down", "EPSDN" not in out)


def test_promote_prefers_top_setups_over_lanes():
    print("promote: top_setups before side lanes:")
    scan = {
        "top_setups": [
            _row("TOPPED", post_earnings_drift_candidate=True),
        ],
        "contrarian_setups": [
            _row("CONPED", post_earnings_drift_candidate=True, swing_setup_score=99),
        ],
        "deep_value_200w": [
            _row("DV", liquid=True,
                 analyst_estimates={"eps_revision_direction": "up"}),
        ],
        "supplier_pullbacks": [],
    }
    out = promote_focus_candidates(scan, already=[], max_extra=1)
    check("top_setups PED beats lane PED", out == ["TOPPED"], str(out))
    out2 = promote_focus_candidates(scan, already=["TOPPED"], max_extra=2)
    check("then lane PED before other signals", out2[0] == "CONPED", str(out2))
    check("then deep_value liquid eps-up", out2 == ["CONPED", "DV"], str(out2))


def test_promote_max_extra_cap():
    print("promote: respects max_extra:")
    scan = {
        "top_setups": [
            _row(f"P{i}", post_earnings_drift_candidate=True) for i in range(5)
        ],
        "contrarian_setups": [],
        "deep_value_200w": [],
        "supplier_pullbacks": [],
    }
    out = promote_focus_candidates(scan, already=[], max_extra=2)
    check("len == 2", len(out) == 2, str(out))
    check("first two PEDs", out == ["P0", "P1"], str(out))
    out1 = promote_focus_candidates(scan, already=[], max_extra=1)
    check("max_extra=1", out1 == ["P0"], str(out1))


def test_promote_case_normalize():
    print("promote: ticker case / already normalize:")
    scan = {
        "top_setups": [
            _row("nvda", post_earnings_drift_candidate=True),
            _row("AMD", post_earnings_drift_candidate=True),
        ],
        "contrarian_setups": [],
        "deep_value_200w": [],
        "supplier_pullbacks": [],
    }
    out = promote_focus_candidates(scan, already=["NVDA"], max_extra=2)
    check("already nvda case-insensitive skip", out == ["AMD"], str(out))
    check("returned tickers uppercased", all(t == t.upper() for t in out), str(out))


if __name__ == "__main__":
    for fn in (
        test_scan_universe_signature,
        test_promote_empty_and_guards,
        test_promote_prefers_post_earnings_drift,
        test_promote_beyond_first_five_by_score,
        test_promote_eps_revision_up_liquid,
        test_promote_prefers_top_setups_over_lanes,
        test_promote_max_extra_cap,
        test_promote_case_normalize,
    ):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All scan-focus checks passed.")
