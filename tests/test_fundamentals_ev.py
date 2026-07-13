"""Offline unit tests for the enterprise-value / leverage layer in
tools.fundamentals (no network, no yfinance, no pandas).

Injects fake companyfacts-shaped dicts to exercise the NEW deterministic math:

  * TTM EBITDA = TTM operating income + the de-YTD'd D&A add-back, with a
    depreciation-only labeled fallback and a null+reason when D&A is absent,
  * net_debt/EBITDA (price-free) with its coarse leverage read (net_cash /
    conservative / moderate / levered) and the non-positive-EBITDA guard,
  * enterprise value = market cap + total debt - cash, and the price-dependent
    ratios degrading to reason "price_unavailable" when yfinance is absent
    (which it is here) instead of raising or looking like zero.

Run:  python3 tests/test_fundamentals_ev.py
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.fundamentals as F

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def approx(a, b, tol=1.0):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# fake companyfacts builder
# ---------------------------------------------------------------------------
def dur(start, end, val, form="10-Q"):
    return {"start": start, "end": end, "val": val, "form": form}


def inst(end, val, form="10-Q"):
    return {"end": end, "val": val, "form": form}


# Nine calendar quarters, oldest first (fiscal = calendar).
QTR = [
    ("2024-04-01", "2024-06-30"), ("2024-07-01", "2024-09-30"),
    ("2024-10-01", "2024-12-31"), ("2025-01-01", "2025-03-31"),
    ("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30"),
    ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31"),
    ("2026-04-01", "2026-06-30"),
]


def _ytd_rows(fy_start, q_ends, q_vals):
    """Cumulative YTD rows for one fiscal (calendar) year from per-quarter values."""
    rows, running = [], 0.0
    for end, v in zip(q_ends, q_vals):
        running += v
        rows.append(dur(fy_start, end, running))
    return rows


def build_facts(da="dda"):
    """da: 'dda' -> DepreciationDepletionAndAmortization, 'dep_only' -> Depreciation,
    'none' -> no D&A tag at all."""
    gaap = {}

    def put(tag, rows):
        gaap[tag] = {"units": {"USD": rows}}

    def put_sh(tag, rows):
        gaap[tag] = {"units": {"shares": rows}}

    # Income statement, single-quarter (distinct starts): op income = 200..280M.
    put("RevenueFromContractWithCustomerExcludingAssessedTax",
        [dur(s, e, 1000e6 + i * 50e6) for i, (s, e) in enumerate(QTR)])
    put("OperatingIncomeLoss",
        [dur(s, e, 200e6 + i * 10e6) for i, (s, e) in enumerate(QTR)])
    put_sh("WeightedAverageNumberOfDilutedSharesOutstanding",
           [dur(s, e, 1000e6 - i * 2e6) for i, (s, e) in enumerate(QTR)])

    # D&A on the cash-flow statement, reported YTD-cumulative: 50M/qtr in FY2025,
    # 55M/qtr in FY2026. TTM D&A (last 4 de-YTD'd quarters) = 50+50+55+55 = 210M.
    da_rows = (_ytd_rows("2025-01-01",
                         ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"],
                         [50e6, 50e6, 50e6, 50e6])
               + _ytd_rows("2026-01-01", ["2026-03-31", "2026-06-30"], [55e6, 55e6]))
    if da == "dda":
        put("DepreciationDepletionAndAmortization", da_rows)
    elif da == "dep_only":
        put("Depreciation", da_rows)

    # Buybacks (YTD) — only affects buyback_yield, which is price-gated here anyway.
    put("PaymentsForRepurchaseOfCommonStock",
        _ytd_rows("2025-01-01",
                  ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"],
                  [30e6, 30e6, 30e6, 30e6])
        + _ytd_rows("2026-01-01", ["2026-03-31", "2026-06-30"], [35e6, 35e6]))

    # Balance sheet (instant): total debt = 540 LT + 110 current = 650M; cash 300M.
    put("CashAndCashEquivalentsAtCarryingValue", [inst("2026-06-30", 300e6)])
    put("LongTermDebtNoncurrent", [inst("2026-06-30", 500e6)])
    put("DebtCurrent", [inst("2026-06-30", 100e6)])
    put("FinanceLeaseLiabilityNoncurrent", [inst("2026-06-30", 40e6)])
    put("FinanceLeaseLiabilityCurrent", [inst("2026-06-30", 10e6)])

    return {"entityName": "FakeCo Inc", "facts": {"us-gaap": gaap}}


def install_fake_sec(facts):
    mod = types.ModuleType("tools.sec_filings")
    mod.ticker_to_cik = lambda t: "0000000001"
    mod.get_company_facts = lambda c: facts
    sys.modules["tools.sec_filings"] = mod


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_leverage_read():
    print("_leverage_read coarse bands + guards:")
    # net cash — safe whatever EBITDA does.
    r, lab, why = F._leverage_read(-100.0, 500.0)
    check("net cash -> label net_cash, negative ratio", lab == "net_cash" and approx(r, -0.2, 0.01), f"{r},{lab}")
    r, lab, why = F._leverage_read(-100.0, -50.0)
    check("net cash + neg EBITDA -> net_cash, ratio None", lab == "net_cash" and r is None, f"{r},{lab}")
    # positive-net-debt bands.
    r, lab, _ = F._leverage_read(300.0, 200.0)
    check("1.5x -> conservative", approx(r, 1.5, 0.01) and lab == "conservative", f"{r},{lab}")
    r, lab, _ = F._leverage_read(400.0, 200.0)
    check("2.0x boundary -> moderate", approx(r, 2.0, 0.01) and lab == "moderate", f"{r},{lab}")
    r, lab, _ = F._leverage_read(600.0, 200.0)
    check("3.0x boundary -> moderate", approx(r, 3.0, 0.01) and lab == "moderate", f"{r},{lab}")
    r, lab, _ = F._leverage_read(700.0, 200.0)
    check("3.5x -> levered", approx(r, 3.5, 0.01) and lab == "levered", f"{r},{lab}")
    # not-computable guards -> label None + reason (never zero).
    r, lab, why = F._leverage_read(300.0, None)
    check("EBITDA unavailable -> null + reason", r is None and lab is None and why == "ebitda_unavailable", why)
    r, lab, why = F._leverage_read(300.0, -10.0)
    check("net debt + non-positive EBITDA -> ebitda_non_positive (HIGH risk, not zero)",
          r is None and lab is None and why == "ebitda_non_positive", why)
    r, lab, why = F._leverage_read(None, 200.0)
    check("net debt unavailable -> null + reason", r is None and lab is None and why == "net_debt_unavailable", why)


def test_enterprise_value():
    print("_enterprise_value:")
    check("EV = mcap + debt - cash", approx(F._enterprise_value(10000.0, 650.0, 300.0), 10350.0, 0.01))
    check("no market cap -> None", F._enterprise_value(None, 650.0, 300.0) is None)
    check("debt/cash default to 0", approx(F._enterprise_value(10000.0, None, None), 10000.0, 0.01))


def test_ttm_da_extraction():
    print("TTM D&A extraction from YTD cash-flow facts:")
    gaap = build_facts()["facts"]["us-gaap"]
    da_rows, tag = F._series_tagged(gaap, F.DA_TAGS, instant=False, keep=12)
    check("picks DepreciationDepletionAndAmortization", tag == "DepreciationDepletionAndAmortization", str(tag))
    ttm_da, end, _ = F._ttm(F._quarterize_flow(da_rows))
    check("TTM D&A = 50+50+55+55 = 210M", approx(ttm_da, 210e6, 1e6), str(ttm_da))


# ---------------------------------------------------------------------------
# end-to-end (fake sec_filings injected; yfinance genuinely absent)
# ---------------------------------------------------------------------------
def test_ebitda_and_leverage_end_to_end():
    print("get_deep_fundamentals EV/leverage end-to-end:")
    install_fake_sec(build_facts())
    out = F.get_deep_fundamentals("FAKE")
    check("status ok", out.get("status") == "ok", str(out.get("reason")))
    r = out["quality_ratios"]["ratios"]

    eb = r.get("ebitda_ttm_usd", {})
    check("ebitda_ttm_usd = 1060 op + 210 D&A = 1270M", approx(eb.get("value"), 1270e6, 2e6), str(eb.get("value")))
    comp = eb.get("components", {})
    check("EBITDA components expose op income 1060M", approx(comp.get("ttm_operating_income_usd"), 1060e6, 2e6), str(comp))
    check("EBITDA components expose D&A 210M", approx(comp.get("ttm_d_and_a_usd"), 210e6, 2e6), str(comp))
    check("EBITDA labels the D&A tag used", comp.get("d_and_a_tag") == "DepreciationDepletionAndAmortization", str(comp))

    nde = r.get("net_debt_to_ebitda", {})
    check("net_debt_to_ebitda = 350M / 1270M ~ 0.28x", approx(nde.get("value"), 0.28, 0.01), str(nde.get("value")))
    check("leverage_read = conservative (<2x)", nde.get("leverage_read") == "conservative", str(nde.get("leverage_read")))

    # yfinance is absent in this env -> every price-dependent ratio null + reason.
    for k in ("market_cap_usd", "enterprise_value_usd", "ev_to_ebitda", "buyback_yield_pct"):
        cell = r.get(k, {})
        check(f"{k} null with reason price_unavailable",
              cell.get("value") is None and cell.get("reason") == "price_unavailable", str(cell))
    check("price-dependent ratios present (not omitted)",
          "enterprise_value_usd" in r and "ev_to_ebitda" in r, str(list(r.keys())))
    # Every pre-existing key must survive additively.
    for k in ("net_debt_usd", "total_debt_usd", "buyback_ttm_usd", "operating_margin_ttm_pct"):
        check(f"existing key preserved: {k}", k in r, str(list(r.keys())))


def test_da_absent_nulls_ebitda():
    print("D&A absent -> EBITDA + downstream ratios null with reasons (not zero):")
    install_fake_sec(build_facts(da="none"))
    out = F.get_deep_fundamentals("FAKE")
    r = out["quality_ratios"]["ratios"]
    eb = r.get("ebitda_ttm_usd", {})
    check("ebitda_ttm_usd value None", eb.get("value") is None, str(eb))
    check("ebitda reason d_and_a_unavailable", eb.get("reason") == "d_and_a_unavailable", str(eb.get("reason")))
    nde = r.get("net_debt_to_ebitda", {})
    check("net_debt_to_ebitda null with reason ebitda_unavailable",
          nde.get("value") is None and nde.get("reason") == "ebitda_unavailable", str(nde))


def test_depreciation_only_fallback_labeled():
    print("depreciation-only fallback is used and labeled:")
    install_fake_sec(build_facts(da="dep_only"))
    out = F.get_deep_fundamentals("FAKE")
    eb = out["quality_ratios"]["ratios"].get("ebitda_ttm_usd", {})
    check("EBITDA still computed from Depreciation", approx(eb.get("value"), 1270e6, 2e6), str(eb.get("value")))
    check("xbrl_tags names Depreciation", "Depreciation" in (eb.get("xbrl_tags") or ""), str(eb.get("xbrl_tags")))
    check("note flags depreciation-only understatement", "depreciation-only" in (eb.get("note") or ""), str(eb.get("note")))


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_leverage_read()
    test_enterprise_value()
    test_ttm_da_extraction()
    test_ebitda_and_leverage_end_to_end()
    test_da_absent_nulls_ebitda()
    test_depreciation_only_fallback_labeled()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All EV/leverage checks passed.")
