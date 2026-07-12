"""Deep fundamentals tool — second-layer EDGAR XBRL pull for focus tickers.

Complements tools/sec_filings.get_filing_brief with the line items a swing
thesis actually turns on: operating leverage (op income, R&D, SG&A), balance
sheet stress (inventory, receivables, current assets/liabilities), forward
demand (deferred revenue + RemainingPerformanceObligation for software names),
and capital allocation (buybacks, acquisitions).

Keyless: everything comes from data.sec.gov companyfacts via tools.net.get_sec
(User-Agent + proxy fallback handled there). One fetch per ticker — the
quality-ratio inputs (revenue, gross profit, SBC, shares, cash, debt, OCF,
capex) are extracted from the same payload with longer history than the
6-quarter display window, so YoY comparisons never starve.

XBRL fallback tag lists adapted from the user's Ledgerline project
(~/finance/lib/edgar.ts) — that tag map is battle-tested across issuers;
deferred-revenue / RPO / acquisition tags added fresh here. The quarterly
row filter is reused from tools.sec_filings._annualish.

The quality_ratios section is PRE-COMPUTED so the LLM brain never does
arithmetic. Every ratio is {"value": x, "trend": ..., "note": ...}; anything
that cannot be computed is listed under "omitted" with a reason. Fail-soft
everywhere: a missing tag never breaks the run.

CLI: python -m tools.fundamentals NOW
"""

from __future__ import annotations

import json
from datetime import date

# Concept -> ordered fallback tag lists. First tag with usable, RECENT facts
# wins (companies switch tags; a stale series must not shadow a live one).
# Tag lists largely adapted from ~/finance/lib/edgar.ts (Ledgerline).
CONCEPT_TAGS = {
    "operating_income": ["OperatingIncomeLoss"],
    "rd_expense": ["ResearchAndDevelopmentExpense",
                   "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "sga_expense": ["SellingGeneralAndAdministrativeExpense",
                    "GeneralAndAdministrativeExpense"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating",
                         "InterestAndDebtExpense", "InterestExpenseDebt"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "inventory": ["InventoryNet", "InventoryFinishedGoodsNetOfReserves"],
    "accounts_receivable": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "total_current_assets": ["AssetsCurrent"],
    "total_current_liabilities": ["LiabilitiesCurrent"],
    "stockholders_equity": ["StockholdersEquity",
                            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "deferred_revenue": ["ContractWithCustomerLiabilityCurrent",
                         "ContractWithCustomerLiability",
                         "DeferredRevenueCurrent", "DeferredRevenue"],
    "remaining_performance_obligation": ["RevenueRemainingPerformanceObligation"],
    "share_buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired",
                     "PaymentsToAcquireBusinessesGross"],
}

# Instant facts (balance-sheet snapshots) have no start date; everything else
# is a duration/flow fact whose rows may be YTD-cumulative in Q2/Q3 filings.
INSTANT_CONCEPTS = {"inventory", "accounts_receivable", "total_current_assets",
                    "total_current_liabilities", "stockholders_equity",
                    "deferred_revenue", "remaining_performance_obligation"}

STALE_DAYS = 550  # a tag whose newest fact is older than this lost to a fresher fallback


def _span_days(u: dict):
    try:
        return (date.fromisoformat(u["end"]) - date.fromisoformat(u["start"])).days
    except (KeyError, ValueError, TypeError):
        return None


def _rows_from_units(units: list, instant: bool, keep: int) -> list:
    """Dedupe/sort 10-Q & 10-K facts, newest last, keep the last `keep` periods."""
    rows = [u for u in units if u.get("form") in ("10-Q", "10-K")
            and u.get("frame") is None]
    seen, out = set(), []
    for u in sorted(rows, key=lambda u: (u.get("end", ""), u.get("start") or "")):
        key = (u.get("start"), u.get("end"))
        if key in seen:
            continue
        seen.add(key)
        row = {"period_end": u.get("end"), "value": u.get("val"), "form": u.get("form")}
        if not instant:
            days = _span_days(u)
            if days is not None:
                row["period_days"] = days  # >100 days => YTD-cumulative, not single quarter
        out.append(row)
    return out[-keep:]


def _series(gaap: dict, tags: list, instant: bool, keep: int = 6) -> list:
    """Best fallback-tag series: first tag with recent data; else freshest available."""
    best, best_end = [], ""
    for tag in tags:
        unit_map = gaap.get(tag, {}).get("units", {})
        units = unit_map.get("USD") or unit_map.get("USD/shares") or unit_map.get("shares")
        if not units:
            continue
        rows = _rows_from_units(units, instant, keep)
        if not rows:
            continue
        end = rows[-1]["period_end"] or ""
        try:
            fresh = (date.today() - date.fromisoformat(end)).days <= STALE_DAYS
        except ValueError:
            fresh = False
        if fresh:
            return rows          # first fresh tag in priority order wins
        if end > best_end:       # remember the least-stale series as a fallback
            best, best_end = rows, end
    return best


def _quarterly(rows: list) -> list:
    """Keep only single-quarter duration rows (~90 days)."""
    return [r for r in rows if 80 <= (r.get("period_days") or -1) <= 100]


def _latest(rows) -> dict:
    return rows[-1] if rows else {}


def _find_by_end(rows, period_end) -> dict:
    for r in rows or []:
        if period_end and r.get("period_end") == period_end:
            return r
    return {}


def _n_back(rows, n: int) -> dict:
    """Row n periods before the latest (rows are newest-last)."""
    return rows[-1 - n] if rows and len(rows) > n else {}


def _year_ago(rows) -> dict:
    """Row ending ~365 days before the latest row (date-matched, not index-
    matched - quarterly series from companyfacts are often sparse)."""
    latest = _latest(rows)
    try:
        target = date.fromisoformat(latest["period_end"])
    except (KeyError, ValueError, TypeError):
        return {}
    best, best_diff = {}, 61
    for r in rows[:-1]:
        try:
            diff = abs((target - date.fromisoformat(r["period_end"])).days - 365)
        except (KeyError, ValueError, TypeError):
            continue
        if diff < best_diff:
            best, best_diff = r, diff
    return best


def _trend(delta, flat_band: float, higher_is_better: bool = True) -> str:
    if delta is None or abs(delta) <= flat_band:
        return "flat"
    good = delta > 0 if higher_is_better else delta < 0
    return "improving" if good else "deteriorating"


def _pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(100.0 * a / b, 2)


def get_deep_fundamentals(ticker: str) -> dict:
    """Second-layer fundamentals + pre-computed quality ratios for one ticker."""
    from tools.net import get_sec
    from tools.sec_filings import ticker_to_cik

    try:
        cik = ticker_to_cik(ticker)
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    if cik is None:
        return {"status": "error", "reason": f"no CIK found for {ticker}"}

    try:
        facts = get_sec(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
    except Exception as e:
        return {"status": "error", "reason": f"companyfacts unavailable: {e}"}
    gaap = facts.get("facts", {}).get("us-gaap", {})

    quarterly = {}
    for concept, tags in CONCEPT_TAGS.items():
        rows = _series(gaap, tags, instant=concept in INSTANT_CONCEPTS, keep=6)
        if rows:
            quarterly[concept] = rows

    # ---- ratio inputs: same payload, longer history so YoY never starves ----
    revenue = _quarterly(_series(gaap, [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues", "SalesRevenueNet"], instant=False, keep=24))
    gross = _quarterly(_series(gaap, ["GrossProfit"], instant=False, keep=24))
    sbc = _quarterly(_series(gaap, [
        "ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
        instant=False, keep=24))
    shares = _quarterly(_series(gaap, [
        "WeightedAverageNumberOfDilutedSharesOutstanding"], instant=False, keep=24))
    cash = _series(gaap, [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
        instant=True, keep=8)
    lt_debt = _series(gaap, [
        "LongTermDebtNoncurrent", "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations"], instant=True, keep=8)
    ocf = _series(gaap, [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        instant=False, keep=12)
    capex = _series(gaap, [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements"],
        instant=False, keep=12)
    op_inc_q = _quarterly(_series(gaap, ["OperatingIncomeLoss"], instant=False, keep=24))

    ratios, omitted = {}, {}
    rev_l, rev_o = _latest(revenue), _year_ago(revenue)

    # gross margin: latest Q vs 4 quarters ago
    try:
        m_l = _pct(_find_by_end(gross, rev_l.get("period_end")).get("value"), rev_l.get("value"))
        m_o = _pct(_find_by_end(gross, rev_o.get("period_end")).get("value"), rev_o.get("value"))
        if m_l is not None:
            ratios["gross_margin_pct"] = {
                "value": m_l,
                "trend": _trend(None if m_o is None else m_l - m_o, 0.5),
                "note": ("latest Q %s%% vs %s%% four quarters ago (%s)"
                         % (m_l, m_o, rev_l.get("period_end")) if m_o is not None
                         else "latest Q gross margin; no year-ago comparison available"),
            }
        else:
            omitted["gross_margin_pct"] = "GrossProfit not reported for the latest revenue quarter"
    except Exception as e:
        omitted["gross_margin_pct"] = str(e)

    # operating margin: latest Q vs 4 quarters ago
    try:
        m_l = _pct(_find_by_end(op_inc_q, rev_l.get("period_end")).get("value"), rev_l.get("value"))
        m_o = _pct(_find_by_end(op_inc_q, rev_o.get("period_end")).get("value"), rev_o.get("value"))
        if m_l is not None:
            ratios["operating_margin_pct"] = {
                "value": m_l,
                "trend": _trend(None if m_o is None else m_l - m_o, 0.5),
                "note": ("latest Q %s%% vs %s%% four quarters ago"
                         % (m_l, m_o) if m_o is not None
                         else "latest Q operating margin; no year-ago comparison"),
            }
        else:
            omitted["operating_margin_pct"] = "no single-quarter operating income matching latest revenue quarter"
    except Exception as e:
        omitted["operating_margin_pct"] = str(e)

    # SBC as % of revenue (single-quarter rows on both sides)
    try:
        v = _pct(_find_by_end(sbc, rev_l.get("period_end")).get("value"), rev_l.get("value"))
        p = _pct(_find_by_end(sbc, rev_o.get("period_end")).get("value"), rev_o.get("value"))
        if v is not None:
            ratios["sbc_pct_of_revenue"] = {
                "value": v,
                "trend": _trend(None if p is None else v - p, 0.5, higher_is_better=False),
                "note": "SBC is %s%% of latest Q revenue - haircut GAAP-adjusted earnings by this" % v,
            }
        else:
            omitted["sbc_pct_of_revenue"] = "no single-quarter SBC matching the latest revenue quarter"
    except Exception as e:
        omitted["sbc_pct_of_revenue"] = str(e)

    # YoY diluted share change (net dilution vs buybacks)
    try:
        s_l, s_o = _latest(shares), _year_ago(shares)
        if s_l.get("value") and s_o.get("value"):
            chg = round(100.0 * (s_l["value"] - s_o["value"]) / s_o["value"], 2)
            ratios["diluted_shares_yoy_change_pct"] = {
                "value": chg,
                "trend": _trend(chg, 0.25, higher_is_better=False),
                "note": ("share count shrinking - net buybacks" if chg < -0.25 else
                         "share count growing - net dilution" if chg > 0.25 else
                         "share count roughly flat") + " (%s vs %s)" % (
                             s_l.get("period_end"), s_o.get("period_end")),
            }
        else:
            omitted["diluted_shares_yoy_change_pct"] = "diluted share counts unavailable YoY"
    except Exception as e:
        omitted["diluted_shares_yoy_change_pct"] = str(e)

    # net debt (LT debt - cash), latest snapshot, trend vs prior period
    try:
        c_l, d_l = _latest(cash), _latest(lt_debt)
        # A debt series whose newest fact is far older than the newest cash fact
        # means the company stopped reporting LT debt (repaid it): treat as zero.
        debt_stale = False
        try:
            debt_stale = (d_l and c_l and
                          (date.fromisoformat(c_l["period_end"])
                           - date.fromisoformat(d_l["period_end"])).days > STALE_DAYS)
        except (KeyError, ValueError, TypeError):
            pass
        if c_l.get("value") is not None and (debt_stale or not d_l):
            ratios["net_debt_usd"] = {
                "value": -c_l["value"],
                "trend": "flat",
                "note": "no recent long-term debt reported - treated as debt-free; "
                        "value is negative cash as of %s" % c_l.get("period_end"),
            }
        elif c_l.get("value") is not None and d_l.get("value") is not None:
            nd = d_l["value"] - c_l["value"]
            c_p = _find_by_end(cash, _n_back(lt_debt, 1).get("period_end"))
            d_p = _n_back(lt_debt, 1)
            nd_prev = (d_p["value"] - c_p["value"]
                       if c_p.get("value") is not None and d_p.get("value") is not None else None)
            ratios["net_debt_usd"] = {
                "value": nd,
                "trend": _trend(None if nd_prev is None else nd - nd_prev,
                                abs(nd) * 0.02 + 1e6, higher_is_better=False),
                "note": ("net cash position" if nd < 0 else "net debt position")
                        + " as of %s (LT debt minus cash)" % d_l.get("period_end"),
            }
        else:
            omitted["net_debt_usd"] = "cash or long-term debt series unavailable"
    except Exception as e:
        omitted["net_debt_usd"] = str(e)

    # inventory-to-revenue trend: latest vs year ago -> inventory_building flag
    try:
        inv = quarterly.get("inventory", [])
        i_l = _find_by_end(inv, rev_l.get("period_end")) or _latest(inv)
        i_o = _find_by_end(inv, rev_o.get("period_end")) or _year_ago(inv)
        r_l = _pct(i_l.get("value"), rev_l.get("value"))
        r_o = _pct(i_o.get("value"), rev_o.get("value"))
        if r_l is not None and r_o is not None:
            building = (r_l - r_o) > 3.0  # inventory growing meaningfully faster than sales
            ratios["inventory_to_revenue_pct"] = {
                "value": r_l,
                "trend": _trend(r_l - r_o, 3.0, higher_is_better=False),
                "inventory_building": building,
                "note": "inventory %s%% of Q revenue vs %s%% a year ago%s" % (
                    r_l, r_o, " - INVENTORY BUILDING faster than sales" if building else ""),
            }
        else:
            omitted["inventory_to_revenue_pct"] = "no inventory reported (common for software) or revenue missing"
    except Exception as e:
        omitted["inventory_to_revenue_pct"] = str(e)

    # FCF = OCF - capex over the SAME period span (both may be YTD-cumulative)
    fcf_val, fcf_days = None, None
    try:
        o_l = _latest(ocf)
        cx = _find_by_end(capex, o_l.get("period_end"))
        same_span = (cx and abs((o_l.get("period_days") or 0)
                                - (cx.get("period_days") or 0)) <= 15)
        if o_l.get("value") is not None and cx.get("value") is not None and same_span:
            fcf_val = o_l["value"] - cx["value"]
            fcf_days = o_l.get("period_days")
            ytd = (fcf_days or 0) > 100
            ratios["free_cash_flow_usd"] = {
                "value": fcf_val,
                "trend": "flat",
                "note": "OCF minus capex, period ending %s spanning %s days%s" % (
                    o_l.get("period_end"), fcf_days,
                    " (YTD-cumulative, NOT a single quarter)" if ytd else ""),
            }
        else:
            omitted["free_cash_flow_usd"] = "no capex row matching the latest OCF period span"
    except Exception as e:
        omitted["free_cash_flow_usd"] = str(e)

    # Rule of 40 (software lens): YoY revenue growth % + FCF margin %
    try:
        if rev_l.get("value") and rev_o.get("value") and fcf_val is not None:
            growth = 100.0 * (rev_l["value"] - rev_o["value"]) / rev_o["value"]
            # If FCF spans multiple quarters (YTD), scale to per-quarter estimate.
            n_q = max(1, round((fcf_days or 91) / 91.0))
            margin = 100.0 * (fcf_val / n_q) / rev_l["value"]
            r40 = round(growth + margin, 1)
            ratios["rule_of_40"] = {
                "value": r40,
                "trend": "improving" if r40 >= 40 else "deteriorating",
                "note": "revenue growth %.1f%% + FCF margin %.1f%% (software heuristic; FCF %s)"
                        % (growth, margin,
                           "YTD scaled to per-quarter estimate" if n_q > 1 else "single quarter"),
            }
        else:
            omitted["rule_of_40"] = "needs YoY revenue pair and computable FCF"
    except Exception as e:
        omitted["rule_of_40"] = str(e)

    quality = {"ratios": ratios}
    if omitted:
        quality["omitted"] = omitted

    return {
        "status": "ok",
        "ticker": ticker.upper(),
        "cik": cik,
        "company": facts.get("entityName"),
        "note": ("quarterly_facts: instant rows are balance-sheet snapshots; "
                 "duration rows include period_days - rows spanning >100 days are "
                 "YTD-cumulative, not single quarters. quality_ratios are "
                 "pre-computed - do not recompute them."),
        "quarterly_facts": quarterly,
        "quality_ratios": quality,
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(get_deep_fundamentals(sys.argv[1] if len(sys.argv) > 1 else "NOW"),
                     indent=2))
