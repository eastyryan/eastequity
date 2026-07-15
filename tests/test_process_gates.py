"""Offline tests for process_gates + stack_cards."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.process_gates import (
    audit_brain_process,
    normalize_watchlist,
    validate_rejected_ideas,
)
from tools.stack_cards import build_stack_cards, stack_card_for

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_watchlist_status():
    print("watchlist status:")
    wl, issues = normalize_watchlist([
        {"ticker": "AMD", "status": "HOLD", "one_line": "x"},
        {"ticker": "MU"},  # missing status
        {"ticker": "ZZ", "status": "maybe"},
    ])
    check("normalized 3", len(wl) == 3)
    check("HOLD lowercased", wl[0]["status"] == "hold")
    check("missing -> hold", wl[1]["status"] == "hold")
    check("issues logged", any("status_missing" in i for i in issues))


def test_rejected_ideas_and_audit():
    print("rejected_ideas + full audit:")
    clean, issues = validate_rejected_ideas([
        {"ticker": "MU", "reason": "Cycle peak risk and unconfirmed reclaim"},
        {"ticker": "PANW", "reason": "Extended after headline; not fat pitch setup"},
    ])
    check("two clean", len(clean) == 2 and not any("need_" in i for i in issues))

    parsed = {
        "proposals": [],
        "no_trade_reason": "Nothing clears the bar today.",
        "watchlist": [{"ticker": "AMD", "status": "hold", "one_line": "wait"}],
        "rejected_ideas": clean,
    }
    a = audit_brain_process(parsed, depth="full", top_scan_tickers=["MU", "PANW", "NET"])
    check("full no-trade ok", a["full_run_no_trade_ok"] is True, str(a["issues"]))

    bad = {"proposals": [], "no_trade_reason": "Just waiting.", "watchlist": [
        {"ticker": "AMD", "one_line": "x"}]}  # no status, no rejected_ideas
    a2 = audit_brain_process(bad, depth="full", top_scan_tickers=["MU", "NET"])
    check("full no-trade fails without rejected",
          a2["full_run_no_trade_ok"] is False, str(a2["issues"]))
    check("status issue", any("status" in i for i in a2["issues"]))


def test_stack_cards():
    print("stack cards:")
    c = stack_card_for("DELL")
    check("DELL layer", "server" in (c.get("layer") or "").lower() or "OEM" in (c.get("layer") or ""))
    check("DELL driver", c.get("demand_driver") == "hyperscaler_server_capex")
    check("has differential", bool(c.get("differential_question")))
    pack = build_stack_cards(["NVDA", "MU"])
    check("two cards", pack.get("n") == 2)
    check("NVDA customers", bool(pack["by_ticker"]["NVDA"].get("typical_customers")))


if __name__ == "__main__":
    test_watchlist_status()
    test_rejected_ideas_and_audit()
    test_stack_cards()
    if FAILURES:
        print(f"\nFAILED: {FAILURES}")
        sys.exit(1)
    print("\nAll process/stack tests passed.")
