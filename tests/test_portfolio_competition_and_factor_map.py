"""Offline tests for portfolio competition + factor map + process gates.

    python3 tests/test_portfolio_competition_and_factor_map.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.portfolio_competition import (  # noqa: E402
    build_portfolio_competition,
    competition_hint,
    pick_challengers,
    setup_score,
    validate_seat_reviews,
)
from tools.factor_map import (  # noqa: E402
    build_factor_map,
    build_unwind_playbook,
    concentration_level,
    validate_factor_response,
)
from tools.process_gates import audit_brain_process  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_setup_score_and_challengers():
    print("setup score + challengers:")
    row = {"swing_setup_score": 3.2, "rel_strength_1m_pct": 5}
    check("uses swing score", setup_score(row) == 3.2)
    fallback = setup_score({"above_50dma": True, "above_200dma": True,
                            "rel_strength_1m_pct": 10})
    check("fallback composite", fallback is not None and fallback > 0)

    dmap = {"DELL": "hyperscaler_server_capex", "SMCI": "hyperscaler_server_capex",
            "VST": "datacenter_power", "NET": "software_platforms"}
    rows = [
        {"ticker": "SMCI", "swing_setup_score": 4.0},
        {"ticker": "VST", "swing_setup_score": 3.5},
        {"ticker": "NET", "swing_setup_score": 2.0},
    ]
    ch = pick_challengers("DELL", "hyperscaler_server_capex", rows, {"DELL"}, dmap)
    check("has challengers", len(ch) >= 2)
    check("same theme first-ish", any(c["ticker"] == "SMCI" for c in ch))
    check("cross theme present", any(c["ticker"] == "VST" for c in ch))

    h = competition_hint(2.0, [{"ticker": "SMCI", "setup_score": 4.0}])
    check("under pressure", h["label"] == "under_pressure", h["label"])
    h2 = competition_hint(4.0, [{"ticker": "X", "setup_score": 2.0}])
    check("defensible", h2["label"] == "defensible", h2["label"])


def test_build_competition():
    print("build_portfolio_competition:")
    port = {
        "total_equity_usd": 10000,
        "positions": [
            {"ticker": "DELL", "market_value_usd": 2000, "avg_cost": 100,
             "last_price": 110, "demand_driver": "hyperscaler_server_capex"},
            {"ticker": "NET", "market_value_usd": 1500, "avg_cost": 80,
             "last_price": 90, "demand_driver": "software_platforms"},
        ],
    }
    scan = {
        "top_setups": [
            {"ticker": "SMCI", "swing_setup_score": 4.5, "rel_strength_1m_pct": 12},
            {"ticker": "DELL", "swing_setup_score": 2.0, "rel_strength_1m_pct": 1},
            {"ticker": "NET", "swing_setup_score": 3.0, "rel_strength_1m_pct": 4},
            {"ticker": "VST", "swing_setup_score": 3.8, "rel_strength_1m_pct": 6},
        ],
    }
    dmap = {
        "DELL": "hyperscaler_server_capex", "SMCI": "hyperscaler_server_capex",
        "NET": "software_platforms", "VST": "datacenter_power",
    }
    out = build_portfolio_competition(port, scan=scan, driver_map=dmap)
    check("status ok", out["status"] == "ok")
    check("two seats", out["n_seats"] == 2)
    by_t = {s["ticker"]: s for s in out["seats"]}
    check("DELL under pressure", by_t["DELL"]["competition_hint"] == "under_pressure",
          by_t["DELL"]["competition_hint"])
    check("under_pressure list", "DELL" in out["under_pressure"])

    empty = build_portfolio_competition({"positions": []}, scan=scan)
    check("empty book", empty["status"] == "empty_book")


def test_seat_reviews_validation():
    print("seat_reviews validation:")
    clean, issues = validate_seat_reviews([
        {"ticker": "DELL", "action": "keep", "reason": "Still best vs SMCI on structure",
         "challenger_considered": "SMCI"},
        {"ticker": "NET", "action": "trim", "reason": "Free capital for VST diversifier"},
    ], ["DELL", "NET"])
    check("clean two", len(clean) == 2 and not issues, str(issues))

    _, bad = validate_seat_reviews(
        [{"ticker": "DELL", "action": "hold", "reason": "ok enough here"}],
        ["DELL", "NET"])
    check("invalid action + missing NET",
          any("action_invalid" in i for i in bad)
          and any("missing_holding:NET" in i for i in bad), str(bad))


def test_factor_map():
    print("factor map:")
    check("extreme level",
          concentration_level(0.55, 0.9, 0.8, 3, False) == "extreme")
    check("high level",
          concentration_level(0.40, 0.72, 0.5, 3, False) == "high")
    check("low level",
          concentration_level(0.15, 0.3, 0.2, 3, False) == "low")

    pb = build_unwind_playbook("extreme", "semi_memory", 0.6, 0.9, ["PLMR"], True)
    check("extreme playbook actions", len(pb["actions"]) >= 3)
    check("new buy restrictive", "must not rise" in pb["new_buy_rules"].lower()
          or "extreme" in pb["new_buy_rules"].lower())

    port = {
        "total_equity_usd": 10000,
        "positions": [
            {"ticker": "MU", "market_value_usd": 3000, "demand_driver": "semi_memory"},
            {"ticker": "NVDA", "market_value_usd": 3000, "demand_driver": "ai_compute_gpu"},
            {"ticker": "DELL", "market_value_usd": 2500,
             "demand_driver": "hyperscaler_server_capex"},
            {"ticker": "JNJ", "market_value_usd": 500, "demand_driver": "healthcare"},
        ],
    }
    dmap = {p["ticker"]: p["demand_driver"] for p in port["positions"]}
    fm = build_factor_map(
        port, driver_map=dmap,
        portfolio_risk={"shared_left_tail": True, "avg_pairwise_holding_corr": 0.75},
        theme_cap_pct=0.35,
    )
    check("status ok", fm["status"] == "ok")
    check("high or extreme", fm["concentration_level"] in ("high", "extreme"),
          fm["concentration_level"])
    check("requires response", fm["requires_factor_response"] is True)
    check("kills sentence", "semi_memory" in fm["what_kills_the_book"]
          or "ai_compute" in fm["what_kills_the_book"]
          or "hyperscaler" in fm["what_kills_the_book"]
          or "demand_driver" in fm["what_kills_the_book"])
    check("orthogonal has JNJ", "JNJ" in fm["orthogonal_tickers"])
    check("ai stack high", fm["ai_stack_pct_of_equity"] >= 0.8)

    clean, issues = validate_factor_response(
        {"concentration_level": "high",
         "plan": "No new same-theme buys; trim weakest AI seat if under pressure.",
         "actions": ["no_same_theme_buy", "trim", "cash"]},
        required=True, expected_level="high")
    check("factor response ok", clean is not None and not issues, str(issues))
    _, miss = validate_factor_response(None, required=True)
    check("factor missing", "factor_response_missing" in miss)


def test_process_audit_integration():
    print("process audit seat + factor:")
    parsed = {
        "proposals": [],
        "no_trade_reason": "Waiting for cleaner entries.",
        "watchlist": [{"ticker": "AMD", "status": "hold", "one_line": "wait"}],
        "rejected_ideas": [
            {"ticker": "MU", "reason": "Cycle peak risk and weak structure today"},
            {"ticker": "PANW", "reason": "Extended; not a fat pitch after the pop"},
        ],
        "seat_reviews": [
            {"ticker": "DELL", "action": "keep",
             "reason": "Still better risk/reward than SMCI given cost basis and plan",
             "challenger_considered": "SMCI"},
        ],
        "factor_response": {
            "concentration_level": "high",
            "plan": "Theme heat high; no new server-capex adds until a seat is freed.",
            "actions": ["no_same_theme_buy", "hold_plan"],
        },
    }
    a = audit_brain_process(
        parsed, depth="full", top_scan_tickers=["MU", "PANW"],
        held_tickers=["DELL"],
        require_seat_reviews=True,
        require_factor_response=True,
        factor_concentration_level="high",
    )
    check("process ok", a["process_ok"] is True, str(a["issues"]))
    check("seat kept", len(a["seat_reviews"]) == 1)
    check("factor kept", a["factor_response"] is not None)

    bad = {
        "proposals": [{"ticker": "X", "action": "BUY"}],
        "watchlist": [{"ticker": "AMD", "status": "hold", "one_line": "x"}],
        # missing seat_reviews and factor_response
    }
    a2 = audit_brain_process(
        bad, depth="holdings_watchlist",
        held_tickers=["DELL", "NET"],
        require_seat_reviews=True,
        require_factor_response=True,
        factor_concentration_level="extreme",
    )
    check("missing seats fail", a2["process_ok"] is False, str(a2["issues"]))
    check("seat issues present",
          any("seat_reviews" in i for i in a2["issues"]), str(a2["issues"]))
    check("factor issues present",
          any("factor_response" in i for i in a2["issues"]), str(a2["issues"]))


def main():
    test_setup_score_and_challengers()
    test_build_competition()
    test_seat_reviews_validation()
    test_factor_map()
    test_process_audit_integration()
    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("All portfolio competition / factor map tests passed.")


if __name__ == "__main__":
    main()
