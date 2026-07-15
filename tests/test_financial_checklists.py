"""Offline tests for financial checklists + options IV/OI pure helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.financial_checklists import (
    build_financial_checklists,
    checklist_for_ticker,
    model_type_for_driver,
)
from tools.options_signal import iv_percentile, oi_day_change, term_structure_read

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_models():
    print("model mapping:")
    check("NBIS driver type", model_type_for_driver("hyperscaler_cloud_infra") == "capex_growth_scaleup")
    check("MU semi", model_type_for_driver("semi_memory") == "semi_cyclical")
    check("MSFT mega", model_type_for_driver("mega_tech_platforms") == "mega_platform")
    check("JPM fin", model_type_for_driver("financials") == "financials")
    c = checklist_for_ticker("NBIS")
    check("NBIS has lines", len(c.get("lines") or []) >= 4)
    check("weight mentions growth", "growth" in (c.get("weight") or "").lower()
          or "Revenue" in (c.get("weight") or ""))
    pack = build_financial_checklists(["NBIS", "MU", "JPM"])
    check("three", pack["n"] == 3)
    check("diverse models", len(pack["models_present"]) >= 2)


def test_iv_oi_term():
    print("options pure helpers:")
    warm = iv_percentile(50.0, [40, 45, 50, 55, 60, 70])
    check("percentile ok", warm.get("status") == "ok")
    check("mid-ish", 30 <= warm.get("iv_percentile", 0) <= 80)
    cold = iv_percentile(50.0, [40, 45])
    check("warming", cold.get("status") == "warming_up")
    oi = oi_day_change(
        {"call_open_interest": 10000, "put_open_interest": 8000},
        {"date": "2026-07-13", "call_open_interest": 9000, "put_open_interest": 5000},
    )
    check("oi ok", oi.get("status") == "ok")
    check("put oi up", oi.get("put_oi_change") == 3000)
    term = term_structure_read(90.0, 50.0, 10, 40)
    check("backwardation", term.get("shape") == "steep_backwardation")


if __name__ == "__main__":
    test_models()
    test_iv_oi_term()
    if FAILURES:
        print(f"FAILED {FAILURES}")
        sys.exit(1)
    print("All financial checklist / options hist tests passed.")
