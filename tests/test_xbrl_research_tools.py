"""Offline unit tests for the XBRL research tools (no network, no yfinance).

Injects fake companyfacts-shaped dicts to exercise the NEW deterministic logic
added to fundamentals / filing_text / guidance_ledger:

  * total-debt aggregation (current + noncurrent + finance leases) and the
    stale-debt carry-forward (net_debt no longer blind to short-term debt),
  * de-YTD quarterization of cash-flow facts and TTM roll-ups,
  * SBC tag selection (income-statement 3-month tag preferred; YTD not dropped),
  * rule-of-40 on TTM (value + pass, no bogus "trend"),
  * guidance unit/magnitude sanity (a 1000x mislabel is left ungraded),
  * fiscal-calendar-aware period-end estimation (shifted fiscal years),
  * tightened forward-guidance language detection,
  * MD&A results-of-operations targeting.

Run:  python3 tests/test_xbrl_research_tools.py
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def approx(a, b, tol=1.0):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------------------
# Fake companyfacts builder
# ---------------------------------------------------------------------------
def dur(start, end, val, fy=None, fp=None, form="10-Q", filed=None):
    return {"start": start, "end": end, "val": val, "fy": fy, "fp": fp,
            "form": form, "filed": filed or end}


def inst(end, val, form="10-Q", filed=None):
    return {"end": end, "val": val, "form": form, "filed": filed or end}


# Nine calendar quarters, oldest first.
QTR = [
    ("2024-04-01", "2024-06-30"), ("2024-07-01", "2024-09-30"),
    ("2024-10-01", "2024-12-31"), ("2025-01-01", "2025-03-31"),
    ("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30"),
    ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31"),
    ("2026-04-01", "2026-06-30"),
]
REV = [1000e6 + i * 50e6 for i in range(len(QTR))]  # rising -> positive TTM growth


def _ytd_rows(fy_start, fy_year, q_ends, q_vals):
    """Cumulative YTD rows for one fiscal (calendar) year from per-quarter values."""
    rows, running = [], 0.0
    for end, v in zip(q_ends, q_vals):
        running += v
        rows.append(dur(fy_start, end, running, fy=fy_year, fp="FY"))
    return rows


def build_facts(**over):
    gaap = {}

    def put(tag, rows):
        gaap[tag] = {"units": {"USD": rows}}

    def put_sh(tag, rows):
        gaap[tag] = {"units": {"shares": rows}}

    def put_eps(tag, rows):
        gaap[tag] = {"units": {"USD/shares": rows}}

    # Income statement, single-quarter (distinct starts).
    put("RevenueFromContractWithCustomerExcludingAssessedTax",
        [dur(s, e, REV[i], fp="Q%d" % (((i) % 4) + 1)) for i, (s, e) in enumerate(QTR)])
    put("GrossProfit", [dur(s, e, REV[i] * 0.6) for i, (s, e) in enumerate(QTR)])
    put("OperatingIncomeLoss", [dur(s, e, REV[i] * 0.2) for i, (s, e) in enumerate(QTR)])
    put("InterestExpense", [dur(s, e, 8e6) for (s, e) in QTR])
    if over.get("sbc") != "ytd_only":
        put("AllocatedShareBasedCompensationExpense",
            [dur(s, e, REV[i] * 0.05) for i, (s, e) in enumerate(QTR)])
    if over.get("sbc") in ("ytd_only", "both"):
        # Cash-flow YTD SBC (fiscal = calendar).
        put("ShareBasedCompensation",
            _ytd_rows("2025-01-01", 2025, ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"],
                      [40e6, 40e6, 40e6, 40e6])
            + _ytd_rows("2026-01-01", 2026, ["2026-03-31", "2026-06-30"], [45e6, 45e6]))

    put_sh("WeightedAverageNumberOfDilutedSharesOutstanding",
           [dur(s, e, 1000e6 - i * 2e6) for i, (s, e) in enumerate(QTR)])

    # Cash-flow YTD: OCF and capex, per-quarter -> cumulative.
    put("NetCashProvidedByUsedInOperatingActivities",
        _ytd_rows("2025-01-01", 2025, ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"],
                  [200e6, 210e6, 220e6, 230e6])
        + _ytd_rows("2026-01-01", 2026, ["2026-03-31", "2026-06-30"], [240e6, 250e6]))
    put("PaymentsToAcquirePropertyPlantAndEquipment",
        _ytd_rows("2025-01-01", 2025, ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"],
                  [50e6, 52e6, 54e6, 56e6])
        + _ytd_rows("2026-01-01", 2026, ["2026-03-31", "2026-06-30"], [58e6, 60e6]))
    put("PaymentsForRepurchaseOfCommonStock",
        _ytd_rows("2025-01-01", 2025, ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"],
                  [30e6, 30e6, 30e6, 30e6])
        + _ytd_rows("2026-01-01", 2026, ["2026-03-31", "2026-06-30"], [35e6, 35e6]))

    # Balance sheet (instant).
    cash_end = over.get("cash_end", "2026-06-30")
    put("CashAndCashEquivalentsAtCarryingValue", [inst(cash_end, over.get("cash", 300e6))])
    debt_end = over.get("debt_end", "2026-06-30")
    if not over.get("no_debt"):
        put("LongTermDebtNoncurrent", [inst(debt_end, 500e6)])
        put("DebtCurrent", [inst(debt_end, 100e6)])
        put("FinanceLeaseLiabilityCurrent", [inst(debt_end, 10e6)])
        put("FinanceLeaseLiabilityNoncurrent", [inst(debt_end, 40e6)])
    put("OperatingLeaseLiabilityCurrent", [inst("2026-06-30", 20e6)])
    put("OperatingLeaseLiabilityNoncurrent", [inst("2026-06-30", 80e6)])
    put("AssetsCurrent", [inst("2026-06-30", 800e6)])
    put("LiabilitiesCurrent", [inst("2026-06-30", 400e6)])
    put("InventoryNet", [inst(e, 90e6) for (_s, e) in QTR])

    return {"entityName": "FakeCo Inc", "facts": {"us-gaap": gaap}}


def install_fake_sec(facts):
    mod = types.ModuleType("tools.sec_filings")
    mod.ticker_to_cik = lambda t: "0000000001"
    mod.get_company_facts = lambda c: facts
    sys.modules["tools.sec_filings"] = mod


# ---------------------------------------------------------------------------
# fundamentals: helper-level tests
# ---------------------------------------------------------------------------
def test_quarterize_and_ttm():
    print("de-YTD quarterization + TTM:")
    import tools.fundamentals as F
    ocf = F._series(build_facts()["facts"]["us-gaap"],
                    ["NetCashProvidedByUsedInOperatingActivities"], instant=False, keep=12)
    q = F._quarterize_flow(ocf)
    vals = {r["period_end"]: round(r["value"] / 1e6) for r in q}
    check("Q1 2025 recovered (200M)", vals.get("2025-03-31") == 200, str(vals))
    check("Q2 2025 recovered (210M)", vals.get("2025-06-30") == 210, str(vals))
    check("Q3 2025 recovered (220M)", vals.get("2025-09-30") == 220, str(vals))
    check("Q4 2025 recovered (230M)", vals.get("2025-12-31") == 230, str(vals))
    check("Q2 2026 recovered (250M)", vals.get("2026-06-30") == 250, str(vals))
    ttm, end, prior = F._ttm(q)
    check("TTM OCF = 220+230+240+250 = 940M", approx(ttm, 940e6, 1e6), str(ttm))


def test_compute_total_debt():
    print("total-debt aggregation:")
    import tools.fundamentals as F
    gaap = build_facts()["facts"]["us-gaap"]
    d = F._compute_total_debt(gaap)
    check("long_term = 500 + 40 finance lease = 540M", approx(d["long_term_debt"], 540e6), str(d))
    check("current = 100 + 10 finance lease = 110M", approx(d["current_debt"], 110e6), str(d))
    check("total = 650M (NOT the LT-only 500M)", approx(d["total_debt"], 650e6), str(d))
    check("as_of is the debt snapshot date", d["as_of"] == "2026-06-30", str(d.get("as_of")))
    # No-debt company -> None (caller treats as debt-free).
    check("no debt tags -> None",
          F._compute_total_debt(build_facts(no_debt=True)["facts"]["us-gaap"]) is None)
    # Bundled capital-lease tag must not double-count finance leases.
    g2 = {"LongTermDebtAndCapitalLeaseObligations": {"units": {"USD": [inst("2026-06-30", 600e6)]}},
          "FinanceLeaseLiabilityNoncurrent": {"units": {"USD": [inst("2026-06-30", 40e6)]}}}
    d2 = F._compute_total_debt(g2)
    check("bundled LT tag excludes separate finance-lease add", approx(d2["long_term_debt"], 600e6),
          str(d2))


# ---------------------------------------------------------------------------
# fundamentals: end-to-end (fake sec_filings injected)
# ---------------------------------------------------------------------------
def test_deep_fundamentals_end_to_end():
    print("get_deep_fundamentals end-to-end:")
    install_fake_sec(build_facts())
    import tools.fundamentals as F
    out = F.get_deep_fundamentals("FAKE")
    check("status ok", out.get("status") == "ok", str(out.get("reason")))
    r = out["quality_ratios"]["ratios"]

    nd = r.get("net_debt_usd", {})
    comp = nd.get("components", {})
    check("net_debt = 650M total - 300M cash = 350M", approx(nd.get("value"), 350e6), str(nd.get("value")))
    check("components expose total_debt 650M", approx(comp.get("total_debt"), 650e6), str(comp))
    check("components expose long_term + current", approx(comp.get("long_term_debt"), 540e6)
          and approx(comp.get("current_debt"), 110e6), str(comp))
    check("total_debt_usd surfaced separately", approx(r.get("total_debt_usd", {}).get("value"), 650e6))

    sbc = r.get("sbc_pct_of_revenue", {})
    check("SBC uses income-statement 3-month tag",
          sbc.get("xbrl_tag") == "AllocatedShareBasedCompensationExpense", str(sbc.get("xbrl_tag")))
    check("SBC period_days ~ single quarter", 80 <= (sbc.get("period_days") or 0) <= 100,
          str(sbc.get("period_days")))
    check("SBC% = 5% of revenue", approx(sbc.get("value"), 5.0, 0.2), str(sbc.get("value")))

    r40 = r.get("rule_of_40", {})
    check("rule_of_40 has numeric rule_of_40_value", isinstance(r40.get("rule_of_40_value"), (int, float)))
    check("rule_of_40_pass is a bool", isinstance(r40.get("rule_of_40_pass"), bool))
    check("rule_of_40 no longer emits a bogus 'trend'", "trend" not in r40, str(r40.keys()))

    check("free_cash_flow_ttm_usd = 712M", approx(r.get("free_cash_flow_ttm_usd", {}).get("value"), 712e6, 2e6),
          str(r.get("free_cash_flow_ttm_usd", {}).get("value")))
    check("current_ratio = 2.0", approx(r.get("current_ratio", {}).get("value"), 2.0, 0.01))
    check("interest_coverage present", "interest_coverage" in r, str(list(r.keys())))
    check("gross_margin_ttm_pct present (~60%)", approx(r.get("gross_margin_ttm_pct", {}).get("value"), 60.0, 0.5))
    check("operating_margin_ttm_pct present (~20%)", approx(r.get("operating_margin_ttm_pct", {}).get("value"), 20.0, 0.5))
    check("buyback_ttm_usd present", "buyback_ttm_usd" in r)
    check("operating_lease_liability_usd surfaced (100M, not in debt)",
          approx(r.get("operating_lease_liability_usd", {}).get("value"), 100e6))


def test_stale_debt_carry_forward():
    print("stale debt is carried forward, not zeroed:")
    install_fake_sec(build_facts(debt_end="2024-06-30", cash_end="2026-06-30"))
    import tools.fundamentals as F
    out = F.get_deep_fundamentals("FAKE")
    nd = out["quality_ratios"]["ratios"].get("net_debt_usd", {})
    comp = nd.get("components", {})
    check("debt NOT zeroed (total still 650M)", approx(comp.get("total_debt"), 650e6), str(comp))
    check("net_debt = 650M - 300M = 350M (not -cash)", approx(nd.get("value"), 350e6), str(nd.get("value")))
    check("stale flag set", comp.get("stale") is True, str(comp))


def test_sbc_ytd_only_not_dropped():
    print("SBC reported only YTD is surfaced, not silently omitted:")
    install_fake_sec(build_facts(sbc="ytd_only"))
    import tools.fundamentals as F
    out = F.get_deep_fundamentals("FAKE")
    ratios = out["quality_ratios"]["ratios"]
    omitted = out["quality_ratios"].get("omitted", {})
    sbc = ratios.get("sbc_pct_of_revenue", {})
    check("sbc_pct_of_revenue present (not omitted)",
          "sbc_pct_of_revenue" in ratios and "sbc_pct_of_revenue" not in omitted, str(sbc))
    check("labeled as YTD-derived", "DERIVED" in (sbc.get("note") or ""), str(sbc.get("note")))
    check("period_days reflects the YTD span (>100)", (sbc.get("period_days") or 0) > 100,
          str(sbc.get("period_days")))


# ---------------------------------------------------------------------------
# guidance_ledger: unit sanity + fiscal-calendar period ends
# ---------------------------------------------------------------------------
def test_unit_check():
    print("guidance unit/magnitude sanity:")
    import tools.guidance_ledger as G
    # Consistent: revenue 25e9 vs guide 24.5-25.5 billions -> ok.
    ok, sug, fac = G._unit_check(25e9, 24.5, 25.5, "usd_billions")
    check("consistent billions -> ok", ok, f"ok={ok} sug={sug}")
    # Mislabeled: numbers mean billions but unit says 'usd' -> flagged.
    ok, sug, fac = G._unit_check(25e9, 24.5, 25.5, "usd")
    check("1000x mislabel flagged", not ok, f"ok={ok}")
    check("suggests usd_billions", sug == "usd_billions", f"sug={sug}")
    # EPS sign-flip (guided profit, delivered loss) is a real MISS, not a unit error.
    ok, sug, fac = G._unit_check(-0.50, 1.80, 1.90, "usd_per_share")
    check("EPS loss vs profit guide still gradable (ok)", ok, f"ok={ok}")
    # Consistent EPS.
    ok, sug, fac = G._unit_check(1.85, 1.80, 1.90, "usd_per_share")
    check("consistent EPS -> ok", ok)


def test_grade_guidance_unit_mismatch(tmp_ledger):
    print("grade_guidance leaves a mislabel ungraded:")
    import tools.guidance_ledger as G
    G.record_guidance([{"ticker": "ZZZ", "metric": "revenue", "period": "FY2026Q2",
                        "guide_low": 24.5, "guide_high": 25.5, "unit": "usd"}])
    graded = G.grade_guidance("ZZZ", {"revenue": {"FY2026Q2": 25e9}})
    check("not graded (unit mismatch)", graded == [], str(graded))
    led = G._load_ledger()
    entry = led["ZZZ"][0]
    check("verdict stays None", entry.get("verdict") is None, str(entry.get("verdict")))
    check("unit_mismatch note set", "unit_mismatch" in (entry.get("note") or ""), str(entry.get("note")))
    # A correctly-labeled entry still grades to a beat.
    G.record_guidance([{"ticker": "ZZZ", "metric": "revenue", "period": "FY2026Q3",
                        "guide_low": 24.5, "guide_high": 25.5, "unit": "usd_billions"}])
    graded = G.grade_guidance("ZZZ", {"revenue": {"FY2026Q3": 26e9}})
    check("correctly-labeled entry grades to beat",
          len(graded) == 1 and graded[0]["verdict"] == "beat", str(graded))


def test_fiscal_period_ends():
    print("fiscal-calendar-aware period ends (shifted fiscal year):")
    import tools.guidance_ledger as G
    # NVDA-like: fiscal year ends late January. FY2026Q2 ends 2025-07-27.
    units = [
        dur("2025-04-28", "2025-07-27", 30e9, fy=2026, fp="Q2"),
        dur("2024-04-29", "2024-07-28", 26e9, fy=2025, fp="Q2"),
        dur("2025-01-27", "2026-01-25", 130e9, fy=2026, fp="FY", form="10-K"),
    ]
    ends = G._fiscal_label_ends(units)
    from datetime import date
    check("FY2026Q2 end is actual 2025-07-27", ends.get("FY2026Q2") == date(2025, 7, 27), str(ends))
    check("FY2026 end 2026-01-25", ends.get("FY2026") == date(2026, 1, 25), str(ends))
    check("FY2026Q4 inherits fiscal-year end", ends.get("FY2026Q4") == date(2026, 1, 25), str(ends))
    # Uses actual end, not the calendar assumption (which would be 2026-06-30).
    est = G._period_estimated_end("FY2026Q2", ends)
    check("estimated end uses actual fiscal date", est == date(2025, 7, 27), str(est))
    # Future period projects from prior-year same quarter + 365d.
    est2 = G._period_estimated_end("FY2027Q2", ends)
    check("future FY2027Q2 projected ~2026-07-27", est2 == date(2026, 7, 27), str(est2))
    # No map -> calendar fallback (old behavior preserved).
    check("fallback to calendar when no map", G._period_estimated_end("FY2030Q1", {}) == date(2030, 3, 31))


# ---------------------------------------------------------------------------
# filing_text: guidance language + MD&A targeting
# ---------------------------------------------------------------------------
def test_guidance_language_detector():
    print("tightened guidance-language detector:")
    import tools.filing_text as T
    pos = ("For the second quarter of fiscal 2027, we expect revenue to be in the "
           "range of $24.5 billion to $25.5 billion, and diluted EPS of $1.85 to $1.95.")
    check("real numeric guidance -> True", T._has_guidance_language(pos) is True)
    neg = ("Our outlook and future operating results could be materially and adversely "
           "affected by competition, supply constraints, and macroeconomic conditions, "
           "and our actual results may differ from management's current expectations.")
    check("risk-factor boilerplate -> False", T._has_guidance_language(neg) is False)
    pm = "Third-quarter revenue is expected to be $28.0 billion, plus or minus 2%."
    check("point estimate with +/- -> True", T._has_guidance_language(pm) is True)


def test_mdna_targeting():
    print("MD&A results-of-operations targeting:")
    import tools.filing_text as T
    boiler = "This section contains forward-looking statements subject to risks. " * 8
    body = ("Item 7. Management's Discussion and Analysis of Financial Condition and "
            "Results of Operations\n\n" + boiler + "\n\nResults of Operations\n\n"
            + "Revenue increased 20% to $5.0 billion driven by data-center demand. "
              "Gross margin expanded 150 bps. " * 30
            + "\n\nLiquidity and Capital Resources\n\nWe held $3.0 billion of cash. "
            + "Item 7A. Quantitative and Qualitative Disclosures About Market Risk\n\n"
            + "interest rate risk discussion.")
    excerpt, truncated, section = T._extract_mdna("Table of Contents\n" + body, 12000)
    check("section labeled mdna_results_of_operations", section == "mdna_results_of_operations", section)
    check("excerpt starts at Results of Operations",
          excerpt.lstrip().startswith("Results of Operations"), excerpt[:60])
    check("boilerplate skipped (not in excerpt head)",
          "forward-looking statements" not in excerpt[:200], excerpt[:80])


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
def _with_tmp_ledger(fn):
    """Run a test against an isolated temp ledger file."""
    import tempfile
    import tools.guidance_ledger as G
    orig = G.LEDGER_FILE
    fd, path = tempfile.mkstemp(suffix=".json")
    import os
    os.close(fd)
    os.unlink(path)
    G.LEDGER_FILE = Path(path)
    try:
        fn(None)
    finally:
        G.LEDGER_FILE = orig
        if os.path.exists(path):
            os.unlink(path)


if __name__ == "__main__":
    test_quarterize_and_ttm()
    test_compute_total_debt()
    test_deep_fundamentals_end_to_end()
    test_stale_debt_carry_forward()
    test_sbc_ytd_only_not_dropped()
    test_unit_check()
    _with_tmp_ledger(test_grade_guidance_unit_mismatch)
    test_fiscal_period_ends()
    test_guidance_language_detector()
    test_mdna_targeting()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All XBRL research-tool checks passed.")
