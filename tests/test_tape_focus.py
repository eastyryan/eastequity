"""Offline tests for tools.tape_focus — no network."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.tape_focus import (
    extract_cashtags,
    extract_candidate_tickers,
    promote_focus_from_tape,
    universe_8k_promotions,
    universe_mentions_in_headlines,
)

FAILURES: list[str] = []
UNI = {"NVDA", "AMD", "DELL", "HPE", "ANET", "MU", "META"}


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_cashtags():
    print("cashtags:")
    check("$NVDA hit", "NVDA" in extract_cashtags("Chip rally: $NVDA leads"))
    check("no bare false", "THE" not in extract_cashtags("THE market rose"))


def test_mentions():
    print("headline mentions:")
    heads = [
        {"title": "Nvidia $NVDA jumps on China news"},
        {"title": "AMD and MU memory rally continues"},
        {"title": "Fed holds rates steady"},
    ]
    m = universe_mentions_in_headlines(heads, UNI, max_names=5)
    tickers = [x["ticker"] for x in m]
    check("NVDA from cashtag", "NVDA" in tickers)
    check("AMD bare", "AMD" in tickers)
    check("MU bare", "MU" in tickers)
    check("FED not promoted", "FED" not in tickers)
    check("empty safe", universe_mentions_in_headlines([], UNI) == [])


def test_8k_and_promote():
    print("8k + promote:")
    eights = {"status": "ok", "filers": [
        {"ticker": "DELL", "form": "8-K"},
        {"ticker": "ZZZZ", "form": "8-K"},  # off universe
    ]}
    p8 = universe_8k_promotions(eights, UNI)
    check("DELL 8k", any(x["ticker"] == "DELL" for x in p8))
    check("ZZZZ dropped", not any(x["ticker"] == "ZZZZ" for x in p8))

    already = ["DELL", "HPE"]
    heads = [{"title": "Arista $ANET beats estimates"},
             {"title": "Meta $META AI spend"}]
    add, recs = promote_focus_from_tape(
        already, headlines=heads, todays_8ks=eights, universe=UNI, max_total_extra=5)
    check("does not re-add DELL", "DELL" not in add)
    check("adds ANET from tape", "ANET" in add)
    check("records present", len(recs) == len(add))
    # Cap
    add2, _ = promote_focus_from_tape(
        [], headlines=heads, todays_8ks=eights, universe=UNI, max_total_extra=1)
    check("respects max_total_extra", len(add2) <= 1)


if __name__ == "__main__":
    test_cashtags()
    test_mentions()
    test_8k_and_promote()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        sys.exit(1)
    print("\nAll tape_focus tests passed.")
