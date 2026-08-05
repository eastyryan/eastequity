"""Tests for the 2026-08-05 unblocking fixes. Pure Python, no network:

    python3 tests/test_calibration_probe.py

Regression context, from journal/rejected/2026-08-05.jsonl:
- 15:01Z run 1d39ec: a validated DXCM entry (0.65 conf, RR 2.17, 45d horizon,
  clean sourcing after the OTC claim was dropped) was killed by the bare
  substring match `"intraday" in thesis` over the DESCRIPTIVE phrase
  "digesting -3.6% intraday today". CLAUDE.md's own prose says "sold intraday"
  and "same-day print" — the check now flags intraday INTENT only.
- 20:43Z run a4e811: HPE at an honest 0.57 died on confidence_too_low against
  the 0.60 floor. The floor could never learn whether 0.50-0.60 setups win
  because it never let one trade; such BUYs now execute as half-risk
  calibration probes (max one open) and grade into their own bucket.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator
from tools.performance_breakdown import _confidence_bucket

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


CFG = validator.load_config()
TQR = CFG["trade_quality_requirements"]


def swing_reasons(thesis):
    reasons = []
    validator._check_swing_rules(
        {"action": "BUY", "thesis": thesis, "holding_horizon_days": 45},
        CFG, reasons)
    return [r for r in reasons if r.startswith("intraday_language")]


print("intraday language: descriptive uses pass")
# The exact phrase that killed DXCM run 1d39ec.
check("DXCM 'digesting -3.6% intraday today'",
      not swing_reasons("now digesting -3.6% intraday today to a live price of "
                        "$83.84 - a clean first pullback six days after the print"))
check("'faded intraday'", not swing_reasons("the pop faded intraday but closed green"))
check("'same-day print reaction'",
      not swing_reasons("a same-day print reaction retained 80% of its gap"))
check("'intraday high' / 'intraday low'",
      not swing_reasons("holding above the prior intraday high on rising volume"))
check("'midday trade' is not 'day trade'",
      not swing_reasons("volume dried up in midday trade"))

print("intraday language: intent still fails")
check("'day trade'", bool(swing_reasons("this is a day trade around the open")))
check("'day-trading'", bool(swing_reasons("suited to day-trading the range")))
check("'scalping'", bool(swing_reasons("scalping the gap for a quick point")))
check("'intraday exit'", bool(swing_reasons("plan an intraday exit before the close")))
check("'flip it same-day'", bool(swing_reasons("flip it same-day if it spikes")))

print("confidence probe lane")
PORTFOLIO = {"total_equity_usd": 1000, "positions": [], "history": []}


def buy(conf, ticker="HPE"):
    return {
        "ticker": ticker, "action": "BUY", "confidence": conf,
        "thesis": "x", "holding_horizon_days": 30,
        "entry_price_max": 53.5, "stop_loss": 49.25, "target_price": 62.5,
        "position_size_usd": 124,
    }


def conf_reasons(p, portfolio=None, probes_this_batch=0):
    reasons = []
    validator._check_confidence(p, CFG, reasons)
    validator._check_probe_limits(p, CFG, portfolio or PORTFOLIO,
                                  probes_this_batch, reasons)
    return reasons


p57 = buy(0.57)
r = conf_reasons(p57)
check("HPE 0.57 no longer rejected", not r, str(r))
check("0.57 stamped calibration_probe", p57.get("calibration_probe") is True)

p62 = buy(0.62)
r = conf_reasons(p62)
check("0.62 passes clean", not r, str(r))
check("0.62 NOT stamped", not p62.get("calibration_probe"))

p45 = buy(0.45)
r = conf_reasons(p45)
check("0.45 still hard-rejected", any("confidence_too_low" in x for x in r), str(r))

print("probe risk budget is scaled")
base = float(CFG["position_sizing"]["risk_based_sizing"]["risk_per_trade_pct"])
scale = float(TQR["calibration_probe"]["risk_scale"])
check("probe budget = base * risk_scale",
      abs(validator._risk_budget_pct(p57, CFG) - base * scale) < 1e-12,
      f"{validator._risk_budget_pct(p57, CFG)} vs {base * scale}")
check("full entry keeps base budget",
      abs(validator._risk_budget_pct(p62, CFG) - base) < 1e-12)

print("probe cap: one on the book at a time")
held_probe = {"ticker": "XYZ", "quantity": 1.0, "avg_cost": 50.0,
              "plan": {"stop_loss": 45.0, "confidence": 0.55,
                       "calibration_probe": True}}
r = conf_reasons(buy(0.57), portfolio={"total_equity_usd": 1000,
                                       "positions": [held_probe]})
check("second probe rejected while one is open",
      any("probe_cap_reached" in x for x in r), str(r))
r = conf_reasons(buy(0.57), probes_this_batch=1)
check("second probe rejected within a batch",
      any("probe_cap_reached" in x for x in r), str(r))
r = conf_reasons(buy(0.62), portfolio={"total_equity_usd": 1000,
                                       "positions": [held_probe]})
check("open probe does not block a full-confidence entry", not r, str(r))

print("probe fills grade into their own bucket")
check("0.57 -> 0.50-0.60", _confidence_bucket(0.57) == "0.50-0.60")
check("0.62 -> 0.60-0.65", _confidence_bucket(0.62) == "0.60-0.65")
check("0.45 -> unknown", _confidence_bucket(0.45) == "unknown")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all checks passed")
