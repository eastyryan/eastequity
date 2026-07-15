"""Offline tests: partnership artifact filters + the $1B market-cap floor.

    python3 tests/test_partnerships_mcap.py

Pure Python, no network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.partnerships import _match_partnership, _agreement_8ks  # noqa: E402
import validator  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_headline_matcher():
    print("partnership headline matcher:")
    hits = [
        "Intel signs multi-year agreement to supply chips for Apple iPhones",
        "Zeta Global announces strategic partnership with Palantir",
        "Nebius teams up with Microsoft on AI capacity",
        "ServiceNow expands NVIDIA collaboration for agentic AI",
        "Broadcom lands $10B custom silicon contract",
        "Company enters joint venture for HBM supply",
    ]
    misses = [
        "Shares fall after Q2 earnings miss",
        "Analyst raises price target on strong guidance",
        "CEO to present at tech conference",
        None, "",
    ]
    check("all real deal headlines match", all(_match_partnership(h) for h in hits))
    check("earnings/analyst noise does not match", not any(_match_partnership(h) for h in misses))


def test_agreement_8k_filter():
    print("8-K material-agreement filter:")
    from datetime import datetime, timezone, timedelta
    recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    old_date = (datetime.now(timezone.utc) - timedelta(days=400)).date().isoformat()
    recent = {
        "form":            ["8-K", "8-K", "10-Q", "8-K", "8-K"],
        "filingDate":      [recent_date, recent_date, recent_date, old_date, recent_date],
        "items":           ["1.01,9.01", "2.02,9.01", "", "1.01", "8.01"],
        "accessionNumber": ["0001-11-000001", "0001-11-000002", "0001-11-000003",
                            "0001-11-000004", "0001-11-000005"],
        "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm", "e.htm"],
    }
    out = _agreement_8ks(recent, cik="0001234567", lookback_days=120)
    kinds = {(r["filed"], tuple(r["items"])) for r in out}
    check("material agreement (1.01) kept", (recent_date, ("1.01",)) in kinds, str(kinds))
    check("other-event (8.01) kept and labeled",
          any("8.01" in r["items"] and "verify substance" in r["labels"][0] for r in out))
    check("earnings 8-K (2.02 only) excluded",
          not any(r["items"] == ["2.02"] for r in out))
    check("old filing outside window excluded",
          not any(r["filed"] == old_date for r in out))
    check("urls built", all(r["url"].startswith("https://www.sec.gov/") for r in out))


def _proposal(ticker="DELL", size=1000):
    return {"ticker": ticker, "action": "BUY", "instrument": "EQUITY",
            "position_size_usd": size, "entry_price_max": 450.0, "stop_loss": 412.0,
            "target_price": 510.0, "holding_horizon_days": 30, "confidence": 0.7,
            "risk_reward_ratio": 1.6, "thesis": "clean setup", "macro_context": "ok",
            "variant_perception": "Consensus sees X; I see Y because Z; resolves at earnings.",
        "thesis_invalidators": {"invalidating_print": "EPS guide cut or estimate cuts >5%", "invalidating_structure": "Close below the 50-DMA on rising volume", "time_box": "If no progress toward thesis in 25 trading days, exit"},
        "demand_driver": "hyperscaler_server_capex",
            "catalysts": ["x"]}


def test_market_cap_floor():
    print("$1B market-cap floor:")
    pf = {"total_equity_usd": 10000, "positions": []}
    # sub-$1B -> rejected
    r = validator.validate_proposals([_proposal()], pf,
                                     {"DELL": {"market_cap_usd": 4.2e8}})[0]
    check("sub-$1B BUY rejected", not r.approved and
          any("below_min_market_cap" in x for x in r.reasons), " | ".join(r.reasons))
    # $300B -> passes
    r2 = validator.validate_proposals([_proposal()], pf,
                                      {"DELL": {"market_cap_usd": 3.0e11}})[0]
    check("large cap passes", r2.approved, " | ".join(r2.reasons))
    # no market cap available -> fails open (universe review is the real gate)
    r3 = validator.validate_proposals([_proposal()], pf, {"DELL": {}})[0]
    check("missing cap fails open", r3.approved, " | ".join(r3.reasons))
    # SELL never blocked by cap
    sell = dict(_proposal(), action="SELL_TO_CLOSE")
    r4 = validator.validate_proposals([sell], pf, {"DELL": {"market_cap_usd": 4.2e8}})[0]
    check("SELL_TO_CLOSE unaffected",
          not any("below_min_market_cap" in x for x in r4.reasons))


if __name__ == "__main__":
    for fn in (test_headline_matcher, test_agreement_8k_filter, test_market_cap_floor):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All partnership + market-cap checks passed.")
