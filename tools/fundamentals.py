"""Deep fundamentals tool — second-layer EDGAR XBRL pull for focus tickers.

Complements tools/sec_filings.get_filing_brief with the line items a swing
thesis actually turns on: operating leverage (op income, R&D, SG&A), balance
sheet stress (inventory, receivables, current assets/liabilities), forward
demand (deferred revenue + RemainingPerformanceObligation for software names),
and capital allocation (buybacks, acquisitions).

Keyless: everything comes from data.sec.gov companyfacts via the shared
tools.sec_filings.get_company_facts TTL cache (User-Agent + proxy fallback and
the multi-MB payload are fetched once per CIK per run, reused by sec_filings and
guidance_ledger). The quality-ratio inputs (revenue, gross profit, SBC, shares,
cash, debt, OCF, capex, interest) are extracted from the same payload with
longer history than the 6-quarter display window, so YoY/TTM never starve.

net_debt uses TOTAL debt (long-term + current + finance leases) minus cash — not
long-term debt alone — so revolvers / commercial paper / current maturities are
no longer invisible. Durable ratios (rule_of_40, gross/operating margin,
interest coverage, FCF, buybacks) are computed on TRAILING-TWELVE-MONTH figures
de-YTD'd from the cumulative cash-flow facts, removing seasonality.

The enterprise-value / leverage layer adds TTM EBITDA (operating income + the D&A
add-back), net_debt/EBITDA (price-free — the single highest-value leverage read: a
levered long into a thin catalyst is a different trade than a net-cash compounder),
and the price-dependent market_cap / enterprise_value / ev_to_ebitda / buyback_yield
(latest close via yfinance INSIDE the tool, fail-soft to reason "price_unavailable").
A missing input emits value:null + a short reason label, never an omission that could
read as zero leverage.

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
    # IFRS synonyms appear LAST in each list: recency ties prefer the us-gaap
    # tag, but a foreign filer whose only live series is the IFRS tag still wins.
    "operating_income": ["OperatingIncomeLoss", "ProfitLossFromOperatingActivities"],
    "rd_expense": ["ResearchAndDevelopmentExpense",
                   "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "sga_expense": ["SellingGeneralAndAdministrativeExpense",
                    "GeneralAndAdministrativeExpense"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating",
                         "InterestAndDebtExpense", "InterestExpenseDebt",
                         "FinanceCosts"],
    "eps_diluted": ["EarningsPerShareDiluted", "DilutedEarningsLossPerShare"],
    "inventory": ["InventoryNet", "InventoryFinishedGoodsNetOfReserves",
                  "Inventories"],
    "accounts_receivable": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "total_current_assets": ["AssetsCurrent", "CurrentAssets"],
    "total_current_liabilities": ["LiabilitiesCurrent", "CurrentLiabilities"],
    "stockholders_equity": ["StockholdersEquity",
                            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                            "Equity", "EquityAttributableToOwnersOfParent"],
    "deferred_revenue": ["ContractWithCustomerLiabilityCurrent",
                         "ContractWithCustomerLiability",
                         "DeferredRevenueCurrent", "DeferredRevenue"],
    "remaining_performance_obligation": ["RevenueRemainingPerformanceObligation"],
    "share_buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired",
                     "PaymentsToAcquireBusinessesGross"],
    # Depreciation & amortization add-back (cash-flow statement), for EBITDA. The
    # combined D&A tags come first; depreciation-only is a labeled last resort (it
    # understates EBITDA by the amortization it omits).
    "depreciation_amortization": ["DepreciationDepletionAndAmortization",
                                  "DepreciationAmortizationAndAccretionNet",
                                  "DepreciationAndAmortization",
                                  "DepreciationAmortizationAndAccretion",
                                  "DepreciationAndAmortisationExpense",
                                  "Depreciation"],
}

# EBITDA D&A add-back tag priority (reuses the CONCEPT_TAGS entry above so the
# quarterly display and the TTM EBITDA math cannot drift apart).
DA_TAGS = CONCEPT_TAGS["depreciation_amortization"]

# Instant facts (balance-sheet snapshots) have no start date; everything else
# is a duration/flow fact whose rows may be YTD-cumulative in Q2/Q3 filings.
INSTANT_CONCEPTS = {"inventory", "accounts_receivable", "total_current_assets",
                    "total_current_liabilities", "stockholders_equity",
                    "deferred_revenue", "remaining_performance_obligation"}

STALE_DAYS = 550  # a tag whose newest fact is older than this lost to a fresher fallback


def _merged_facts(facts: dict) -> dict:
    """us-gaap merged OVER ifrs-full (us-gaap wins tag collisions) so foreign
    private issuers aren't invisible. Local copy of sec_filings.merged_facts:
    tests inject a minimal fake tools.sec_filings, so this module must not
    depend on the real one for pure helpers."""
    f = facts.get("facts", {})
    merged = dict(f.get("ifrs-full") or {})
    merged.update(f.get("us-gaap") or {})
    return merged


def _reporting_currencies(fact_dict: dict) -> set:
    """3-letter currency units present across a company's monetary facts.
    (Local copy - see _merged_facts docstring.)"""
    ccys: set = set()
    for tag_data in fact_dict.values():
        for unit in (tag_data.get("units") or {}):
            u = str(unit).split("/")[0]
            if len(u) == 3 and u.isalpha() and u.isupper():
                ccys.add(u)
    return ccys


def _span_days(u: dict):
    try:
        return (date.fromisoformat(u["end"]) - date.fromisoformat(u["start"])).days
    except (KeyError, ValueError, TypeError):
        return None


# Periodic forms whose XBRL facts feed the deep-fundamentals series. Includes
# foreign-private-issuer forms (20-F/40-F annuals, 6-K interims) so universe
# names like TSM/ASML/ARM don't silently return blank quality_ratios — mirrors
# the form set sec_filings.dedupe_facts already accepts.
ACCEPTED_FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A",
                  "20-F", "40-F", "20-F/A", "40-F/A", "6-K", "6-K/A")


def _rows_from_units(units: list, instant: bool, keep: int) -> list:
    """Dedupe/sort periodic-filing facts, newest last, keep the last `keep` periods.

    NEVER filter on the `frame` field: for recently reported quarters the
    frame-annotated entry is often the ONLY copy (unframed twins appear later as
    comparatives in subsequent filings), so excluding frames silently serves
    year-old data - the DELL $23.4B-vs-$43.8B incident. Dedupe by (start, end)
    prefers the LATEST-FILED entry so restatements/amendments win."""
    by_period: dict = {}
    for u in units:
        if u.get("form") not in ACCEPTED_FORMS:
            continue
        key = (u.get("start"), u.get("end"))
        prev = by_period.get(key)
        if prev is None or (u.get("filed") or "") > (prev.get("filed") or ""):
            by_period[key] = u
    out = []
    for u in sorted(by_period.values(), key=lambda u: (u.get("end", ""), u.get("start") or "")):
        row = {"period_end": u.get("end"), "value": u.get("val"), "form": u.get("form")}
        if not instant:
            days = _span_days(u)
            if days is not None:
                row["period_days"] = days  # >100 days => YTD-cumulative, not single quarter
            if u.get("start"):
                row["period_start"] = u.get("start")  # needed to de-YTD flow facts
        out.append(row)
    return out[-keep:]


def _series_tagged(gaap: dict, tags: list, instant: bool, keep: int = 6) -> tuple:
    """(rows, tag_used): the tag whose series is STRICTLY newest; ties go to the
    earlier tag in the list (list order = concept preference, umbrella first).

    The old rule - "first tag fresh within 550 days wins" - let a subset tag that
    lagged a year beat the truly current one (LITE's contract-excluding tag, 470
    days stale, outranked the current including-tax variant). Strict recency with
    ordered tie-break gets both properties: never stale when a fresher tag exists,
    and the preferred (most inclusive) concept wins among equally-fresh ones."""
    best, best_end, best_tag = [], "", None
    for tag in tags:
        unit_map = gaap.get(tag, {}).get("units", {})
        units = unit_map.get("USD") or unit_map.get("USD/shares") or unit_map.get("shares")
        if not units:
            continue
        rows = _rows_from_units(units, instant, keep)
        if not rows:
            continue
        end = rows[-1]["period_end"] or ""
        if end > best_end:  # strictly newer wins; ties keep the earlier-listed tag
            best, best_end, best_tag = rows, end, tag
    return best, best_tag


def _series(gaap: dict, tags: list, instant: bool, keep: int = 6) -> list:
    """Best fallback-tag series: first tag with recent data; else freshest available."""
    return _series_tagged(gaap, tags, instant, keep)[0]


def _latest_instant(gaap: dict, tag: str) -> tuple:
    """(value, period_end) of the newest instant (balance-sheet) fact for one tag."""
    unit_map = gaap.get(tag, {}).get("units", {})
    units = unit_map.get("USD")
    if not units:
        return None, None
    r = _latest(_rows_from_units(units, instant=True, keep=8))
    return r.get("value"), r.get("period_end")


def _quarterize_flow(rows: list) -> list:
    """De-YTD flow facts into single-quarter values, newest last.

    Cash-flow / SBC / interest facts are reported year-to-date (Q1 ~90d,
    H1 ~180d, 9M ~270d, FY ~365d), all sharing one fiscal-year `period_start`.
    Grouping by that start and differencing consecutive rows recovers each
    quarter (= ytd(this) - ytd(previous)). A true single-quarter fact (income
    statement 3-month context) has its OWN start, so it forms a singleton group
    and passes through as itself. When both a 3-month and a derived value land
    on the same period_end, the span closest to ~91 days wins.
    """
    from collections import defaultdict
    by_start = defaultdict(list)
    for r in rows:
        if (r.get("period_days") is None or r.get("value") is None
                or not r.get("period_start")):
            continue
        by_start[r["period_start"]].append(r)
    out = []
    for _start, group in by_start.items():
        prev_v, prev_d = 0.0, 0
        for r in sorted(group, key=lambda x: x["period_end"]):
            q_v, q_d = r["value"] - prev_v, r["period_days"] - prev_d
            if 80 <= q_d <= 100:
                out.append({"period_end": r["period_end"], "value": q_v, "period_days": q_d})
            prev_v, prev_d = r["value"], r["period_days"]
    merged = {}
    for r in sorted(out, key=lambda x: abs((x["period_days"] or 0) - 91), reverse=True):
        merged[r["period_end"]] = r   # closest-to-91-day span wins the period_end
    return [merged[k] for k in sorted(merged)]


def _ttm(rows_q: list) -> tuple:
    """(ttm, end, prior_ttm) from single-quarter rows: sum of the last 4 quarters
    and the 4 before that. Returns (None, None, None) if <4 clean quarters."""
    clean = [r for r in rows_q if r.get("value") is not None]
    if len(clean) < 4:
        return None, None, None
    last4 = clean[-4:]
    ttm = sum(r["value"] for r in last4)
    prior = sum(r["value"] for r in clean[-8:-4]) if len(clean) >= 8 else None
    return ttm, last4[-1]["period_end"], prior


def _ttm_fcf(ocf_q: list, capex_q: list) -> tuple:
    """(ttm_fcf, end) = sum over the last 4 single-quarter OCF rows of
    OCF - capex, matching capex to OCF by period_end. None if a quarter's capex
    is missing (never fabricate a partial FCF)."""
    if len(ocf_q) < 4:
        return None, None
    cap_by_end = {r["period_end"]: r["value"] for r in capex_q}
    total, last4 = 0.0, ocf_q[-4:]
    for r in last4:
        cap = cap_by_end.get(r["period_end"])
        if cap is None:
            return None, None
        total += r["value"] - cap
    return total, last4[-1]["period_end"]


def _compute_total_debt(gaap: dict) -> dict | None:
    """Total debt = noncurrent principal + current principal (+ finance leases).

    Fixes net-debt blindness to current/short-term debt. Components are summed
    without double-counting: the noncurrent side takes the first present of the
    principal representations; the current side prefers the DebtCurrent aggregate
    else sums its parts; finance (capital) leases are added only when the
    noncurrent tag did not already bundle them (consistent with the existing
    LongTermDebtAndCapitalLeaseObligations fallback). Operating leases are left
    OUT of debt (surfaced separately). Returns a component breakdown with
    per-side as_of dates so the caller can flag a stale (no-longer-reported)
    figure and carry it forward rather than zeroing it. None if no debt tag.
    """
    nc_val = nc_end = nc_tag = None
    for tag in ("LongTermDebtNoncurrent", "LongTermDebt",
                "LongTermDebtAndCapitalLeaseObligations"):
        v, e = _latest_instant(gaap, tag)
        if v is not None:
            nc_val, nc_end, nc_tag = v, e, tag
            break
    lt_tags = [nc_tag] if nc_tag else []

    cur_val = cur_end = None
    cur_tags: list = []
    v, e = _latest_instant(gaap, "DebtCurrent")
    if v is not None:
        cur_val, cur_end, cur_tags = v, e, ["DebtCurrent"]
    else:
        acc, latest_e, got = 0.0, "", False
        for tag in ("LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper"):
            vv, ee = _latest_instant(gaap, tag)
            if vv is not None:
                acc, got = acc + vv, True
                cur_tags.append(tag)
                if (ee or "") > latest_e:
                    latest_e = ee
        if got:
            cur_val, cur_end = acc, latest_e

    lease_nc = lease_cur = 0.0
    if nc_tag != "LongTermDebtAndCapitalLeaseObligations":
        v, e = _latest_instant(gaap, "FinanceLeaseLiabilityNoncurrent")
        if v is not None:
            lease_nc = v
            lt_tags.append("FinanceLeaseLiabilityNoncurrent")
            nc_end = nc_end or e
    v, e = _latest_instant(gaap, "FinanceLeaseLiabilityCurrent")
    if v is not None:
        lease_cur = v
        cur_tags.append("FinanceLeaseLiabilityCurrent")
        cur_end = cur_end or e

    long_term = (nc_val or 0.0) + lease_nc if (nc_val is not None or lease_nc) else None
    current = (cur_val or 0.0) + lease_cur if (cur_val is not None or lease_cur) else None
    if long_term is None and current is None:
        return None
    ends = [d for d in (nc_end, cur_end) if d]
    return {
        "long_term_debt": long_term,
        "current_debt": current,
        "total_debt": (long_term or 0.0) + (current or 0.0),
        "as_of": max(ends) if ends else None,
        "long_term_tags": lt_tags,
        "current_tags": cur_tags,
    }


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
    """improving / deteriorating / flat / unknown.

    "unknown" IS NOT "flat", AND THAT DISTINCTION IS THE WHOLE POINT (2026-07-23).
    This returned "flat" when delta was None — i.e. when no prior period existed to
    compare against — so a metric nobody measured was indistinguishable from one that
    genuinely did not move. Combined with ~26 hardcoded `"trend": "flat"` literals at
    the call sites, 17 of 20 ratios on a live name reported "flat" having compared
    nothing at all. A brain reading `operating_margin_ttm_pct: {value: 5.21, trend:
    "flat"}` concludes margins are stable; the truth was that margins were never
    checked. That is the same inverse-of-the-truth failure as an absent
    days_to_earnings reading as "no print imminent".

    Callers must treat "unknown" as "not measured" and say so, never as "no change".
    """
    if delta is None:
        return "unknown"
    if abs(delta) <= flat_band:
        return "flat"
    good = delta > 0 if higher_is_better else delta < 0
    return "improving" if good else "deteriorating"


def _margin_trend(ttm_num, ttm_den, prior_num, prior_den, *, flat_band: float = 0.5,
                  label: str = "margin") -> tuple:
    """(trend, note_suffix) for a TTM margin vs the SAME margin a year earlier.

    The prior-year TTM was already being computed by _ttm() and thrown away at every
    call site, which is why margin EXPANSION — the thing a multi-week swing thesis
    actually turns on — was never visible. Both legs must be present; a margin
    compared against a partial year is worse than no comparison.
    """
    try:
        if None in (ttm_num, prior_num) or not ttm_den or not prior_den:
            return "unknown", " (no prior-year TTM available — trend NOT measured)"
        now = 100.0 * ttm_num / ttm_den
        was = 100.0 * prior_num / prior_den
        delta = now - was
        return _trend(delta, flat_band), (
            f"; {label} {now:.2f}% vs {was:.2f}% a year ago ({delta:+.2f}pp)")
    except Exception:
        return "unknown", " (trend NOT measured)"


def _level_trend(now_v, prior_v, *, flat_band_pct: float = 5.0,
                 higher_is_better: bool = True, label: str = "") -> tuple:
    """(trend, note_suffix) for a TTM LEVEL (dollars, coverage x) vs a year earlier.

    flat_band is a PERCENT of the prior value, not an absolute — $50m of FCF drift
    means something very different on a $200m base than a $6bn one.
    """
    try:
        if now_v is None or prior_v in (None, 0):
            return "unknown", " (no prior-year TTM available — trend NOT measured)"
        pct = 100.0 * (now_v - prior_v) / abs(prior_v)
        return _trend(pct, flat_band_pct, higher_is_better), (
            f"; {label or 'level'} {pct:+.1f}% vs the prior TTM")
    except Exception:
        return "unknown", " (trend NOT measured)"


def _pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(100.0 * a / b, 2)


def _leverage_read(net_debt, ebitda) -> tuple:
    """Coarse net-debt/EBITDA leverage read. Returns (ratio, label, reason).

    * label set  -> a usable read (ratio may still be None for net-cash + a
      non-positive EBITDA, where the ratio is not meaningful but "net_cash" is).
    * label None + reason set -> not computable; the caller emits value:null +
      reason. A missing read must NEVER read as zero leverage.

    net_cash (net_debt < 0) is safe regardless of EBITDA sign. A name WITH net
    debt and a non-positive EBITDA cannot de-lever from operations, so it returns
    reason "ebitda_non_positive" (a high-leverage-risk flag), not a bogus ratio.
    Bands: net_cash / conservative <2x / moderate 2-3x / levered >3x.
    """
    if net_debt is not None and net_debt < 0:  # net cash — safe whatever EBITDA does
        ratio = round(net_debt / ebitda, 2) if (ebitda and ebitda > 0) else None
        return ratio, "net_cash", None
    if ebitda is None:
        return None, None, "ebitda_unavailable"
    if ebitda <= 0:
        return None, None, "ebitda_non_positive"
    if net_debt is None:
        return None, None, "net_debt_unavailable"
    ratio = round(net_debt / ebitda, 2)
    label = "conservative" if ratio < 2 else "moderate" if ratio <= 3 else "levered"
    return ratio, label, None


def _enterprise_value(market_cap, total_debt, cash):
    """EV = market cap + total debt - cash. None if market cap is unavailable;
    debt/cash default to 0 so a debt-free or cash-less name still computes."""
    if market_cap is None:
        return None
    return market_cap + (total_debt or 0.0) - (cash or 0.0)


def get_deep_fundamentals(ticker: str) -> dict:
    """Second-layer fundamentals + pre-computed quality ratios for one ticker."""
    from tools.sec_filings import ticker_to_cik, get_company_facts

    try:
        cik = ticker_to_cik(ticker)
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    if cik is None:
        return {"status": "error", "reason": f"no CIK found for {ticker}"}

    try:
        facts = get_company_facts(cik)  # shared TTL cache (see sec_filings)
    except Exception as e:
        return {"status": "error", "reason": f"companyfacts unavailable: {e}"}
    gaap = _merged_facts(facts)  # us-gaap + ifrs-full (foreign filers)

    quarterly = {}
    for concept, tags in CONCEPT_TAGS.items():
        rows = _series(gaap, tags, instant=concept in INSTANT_CONCEPTS, keep=6)
        if rows:
            quarterly[concept] = rows

    # ---- ratio inputs: same payload, longer history so YoY never starves ----
    # Umbrella concept first: `Revenues` is the GAAP top line; ASC-606 contract tags
    # are subsets that understate fintechs (MELI -31%) and misstate utilities with
    # derivatives (TLN). Banks use RevenuesNetOfInterestExpense. Strict-recency
    # selection in _series_tagged still lets a fresher subset tag win (ORCL).
    revenue = _quarterly(_series(gaap, [
        "Revenues", "RevenuesNetOfInterestExpense",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "Revenue", "RevenueFromContractsWithCustomers"], instant=False, keep=24))
    gross = _quarterly(_series(gaap, ["GrossProfit"], instant=False, keep=24))
    # SBC: prefer the income-statement tag (a 3-month context is usually filed
    # every quarter) over the cash-flow tag (YTD-only, which silently dropped
    # ~3 of 4 quarters). sbc_rows keeps YTD rows too, for a de-YTD fallback.
    sbc_rows, sbc_tag = _series_tagged(gaap, [
        "AllocatedShareBasedCompensationExpense", "ShareBasedCompensation"],
        instant=False, keep=24)
    sbc = _quarterly(sbc_rows)
    shares = _quarterly(_series(gaap, [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "AdjustedWeightedAverageShares"], instant=False, keep=24))
    cash = _series(gaap, [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalents"],
        instant=True, keep=8)
    ocf = _series(gaap, [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivities"],
        instant=False, keep=12)
    capex = _series(gaap, [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        instant=False, keep=12)
    op_inc_q = _quarterly(_series(gaap, [
        "OperatingIncomeLoss", "ProfitLossFromOperatingActivities"],
        instant=False, keep=24))

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

    # SBC as % of revenue. Prefer a single-quarter figure matching the latest
    # revenue quarter; if the issuer only reports SBC year-to-date on the cash
    # flow statement, surface the YTD value divided to a per-quarter estimate
    # (labeled with its span) instead of silently omitting it.
    try:
        sbc_match = _find_by_end(sbc, rev_l.get("period_end"))
        if sbc_match.get("value") is not None and rev_l.get("value"):
            v = _pct(sbc_match["value"], rev_l["value"])
            p = _pct(_find_by_end(sbc, rev_o.get("period_end")).get("value"), rev_o.get("value"))
            ratios["sbc_pct_of_revenue"] = {
                "value": v,
                "trend": _trend(None if p is None else v - p, 0.5, higher_is_better=False),
                "xbrl_tag": sbc_tag, "period_days": sbc_match.get("period_days"),
                "note": "SBC is %s%% of latest Q revenue (single-quarter, tag %s) - "
                        "haircut GAAP-adjusted earnings by this" % (v, sbc_tag),
            }
        else:
            latest_sbc = _latest(sbc_rows)
            days = latest_sbc.get("period_days")
            if latest_sbc.get("value") is not None and rev_l.get("value") and days:
                n_q = max(1, round(days / 91.0))
                v = _pct(latest_sbc["value"] / n_q, rev_l["value"])
                ratios["sbc_pct_of_revenue"] = {
                    "value": v, "trend": "unknown",
                    "xbrl_tag": sbc_tag, "period_days": days,
                    "note": "SBC ~%s%% of revenue, DERIVED from a %s-day YTD figure "
                            "(tag %s) divided to a per-quarter estimate - approximate"
                            % (v, days, sbc_tag),
                }
            else:
                omitted["sbc_pct_of_revenue"] = "no SBC matching the latest revenue quarter"
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

    # Net debt = TOTAL debt (long-term + current, incl. finance leases) - cash.
    # The old version used long-term debt only, understating leverage for names
    # with revolvers / commercial paper / current maturities. A no-longer-reported
    # debt figure is carried forward with a stale flag, never zeroed to "debt-free".
    try:
        c_l = _latest(cash)
        cash_val = c_l.get("value")
        debt = _compute_total_debt(gaap)
        if debt is None and cash_val is not None:
            ratios["net_debt_usd"] = {
                "value": -cash_val, "trend": "unknown",
                "note": "no debt reported in XBRL - treated as debt-free; value is "
                        "negative cash as of %s" % c_l.get("period_end"),
                "components": {"long_term_debt": 0.0, "current_debt": 0.0,
                               "total_debt": 0.0, "cash_and_equivalents": cash_val,
                               "as_of": c_l.get("period_end")},
            }
        elif debt is not None and cash_val is not None:
            total = debt["total_debt"]
            nd = total - cash_val
            stale = False
            try:
                stale = (date.fromisoformat(c_l["period_end"])
                         - date.fromisoformat(debt["as_of"])).days > STALE_DAYS
            except (KeyError, ValueError, TypeError):
                pass
            comp = {
                "long_term_debt": debt["long_term_debt"],
                "current_debt": debt["current_debt"],
                "total_debt": total,
                "cash_and_equivalents": cash_val,
                "as_of": debt["as_of"], "cash_as_of": c_l.get("period_end"),
                "long_term_tags": debt["long_term_tags"],
                "current_tags": debt["current_tags"],
            }
            note = ("net cash" if nd < 0 else "net debt") + \
                   " = total debt (long-term + current, incl. finance leases) minus " \
                   "cash, as of %s" % debt["as_of"]
            if stale:
                comp["stale"] = True
                note += (" (STALE: debt last reported %s, older than cash %s - carried "
                         "forward, NOT zeroed)" % (debt["as_of"], c_l.get("period_end")))
            ratios["net_debt_usd"] = {"value": nd, "trend": "unknown",
                                      "note": note, "components": comp}
            ratios["total_debt_usd"] = {
                "value": total, "trend": "unknown",
                "note": "long-term %s + current %s (finance leases in, operating "
                        "leases out); LT tags %s, current tags %s" % (
                            debt["long_term_debt"], debt["current_debt"],
                            debt["long_term_tags"], debt["current_tags"]),
            }
        else:
            omitted["net_debt_usd"] = "cash unavailable"
    except Exception as e:
        omitted["net_debt_usd"] = str(e)

    # Operating lease liabilities: debt-like commitment surfaced separately (NOT
    # folded into total_debt), so the brain can weigh asset-light / retail names.
    try:
        ol_nc, ol_nc_e = _latest_instant(gaap, "OperatingLeaseLiabilityNoncurrent")
        ol_c, ol_c_e = _latest_instant(gaap, "OperatingLeaseLiabilityCurrent")
        parts = [x for x in (ol_nc, ol_c) if x is not None]
        if parts:
            ratios["operating_lease_liability_usd"] = {
                "value": sum(parts), "trend": "unknown",
                "xbrl_tag": "OperatingLeaseLiabilityCurrent + Noncurrent",
                "note": "operating lease liabilities, NOT in total_debt (a debt-like "
                        "commitment for asset-light/retail names) as of %s"
                        % (ol_nc_e or ol_c_e),
            }
    except Exception as e:
        omitted["operating_lease_liability_usd"] = str(e)

    # Current ratio (liquidity): current assets / current liabilities, latest snapshot.
    try:
        ca = _latest(quarterly.get("total_current_assets", []))
        cl = _latest(quarterly.get("total_current_liabilities", []))
        if ca.get("value") and cl.get("value"):
            cr = round(ca["value"] / cl["value"], 2)
            ratios["current_ratio"] = {
                "value": cr, "trend": "unknown",
                "xbrl_tag": "AssetsCurrent / LiabilitiesCurrent",
                "note": "current assets / current liabilities = %sx as of %s%s" % (
                    cr, ca.get("period_end"),
                    " - under 1.0, liquidity watch" if cr < 1 else ""),
            }
        else:
            omitted["current_ratio"] = "current assets or liabilities not reported"
    except Exception as e:
        omitted["current_ratio"] = str(e)

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
                "trend": "unknown",
                "note": "OCF minus capex, period ending %s spanning %s days%s" % (
                    o_l.get("period_end"), fcf_days,
                    " (YTD-cumulative, NOT a single quarter)" if ytd else ""),
            }
        else:
            omitted["free_cash_flow_usd"] = "no capex row matching the latest OCF period span"
    except Exception as e:
        omitted["free_cash_flow_usd"] = str(e)

    # ---- TTM aggregates (trailing-twelve-month, de-YTD'd) for durable ratios ----
    # Single-quarter growth mixed with an averaged margin (the old rule-of-40)
    # over/under-states seasonal names; TTM removes seasonality on both legs.
    ttm_rev, ttm_rev_end, ttm_rev_prior = _ttm(revenue)
    ttm_gross, _, ttm_gross_prior = _ttm(gross)
    # Keep the prior-year TTM operating income: it is what turns
    # operating_margin_ttm_pct from a bare level into an EXPANSION/COMPRESSION read.
    # _ttm() has always returned it; every call site discarded it.
    ttm_op, _, ttm_op_prior = _ttm(op_inc_q)
    ocf_q = _quarterize_flow(ocf)
    capex_q = _quarterize_flow(capex)
    ttm_fcf, ttm_fcf_end = _ttm_fcf(ocf_q, capex_q)
    # Prior-year TTM FCF from the four quarters BEFORE the current four, computed the
    # same way (capex matched to OCF by period_end, never a partial year).
    ttm_fcf_prior, _ = _ttm_fcf(ocf_q[:-4], capex_q) if len(ocf_q) >= 8 else (None, None)

    # TTM FCF (proper 4-quarter, de-YTD'd) alongside the single-period FCF above.
    if ttm_fcf is not None:
        _fcf_tr, _fcf_sfx = _level_trend(ttm_fcf, ttm_fcf_prior, flat_band_pct=10.0,
                                         label="TTM FCF")
        ratios["free_cash_flow_ttm_usd"] = {
            "value": ttm_fcf, "trend": _fcf_tr,
            "note": "trailing-twelve-month FCF = OCF - capex over 4 de-YTD'd "
                    "quarters ending %s%s" % (ttm_fcf_end, _fcf_sfx),
        }

    # Rule of 40: TTM revenue growth % + TTM FCF margin % (a level, not a "trend").
    try:
        if ttm_rev and ttm_rev_prior and ttm_fcf is not None:
            growth = 100.0 * (ttm_rev - ttm_rev_prior) / ttm_rev_prior
            margin = 100.0 * ttm_fcf / ttm_rev
            r40 = round(growth + margin, 1)
            ratios["rule_of_40"] = {
                "value": r40,
                "rule_of_40_value": r40,
                "rule_of_40_pass": bool(r40 >= 40),
                "note": "TTM revenue growth %.1f%% + TTM FCF margin %.1f%% (software "
                        "heuristic; pass = >=40; TTM ending %s)" % (growth, margin, ttm_rev_end),
            }
        else:
            omitted["rule_of_40"] = "needs 8 quarters of revenue and a computable TTM FCF"
    except Exception as e:
        omitted["rule_of_40"] = str(e)

    # TTM gross margin + trend vs the prior TTM (seasonality-free margin direction).
    try:
        if ttm_gross is not None and ttm_rev:
            m = round(100.0 * ttm_gross / ttm_rev, 2)
            mp = (100.0 * ttm_gross_prior / ttm_rev_prior
                  if ttm_gross_prior is not None and ttm_rev_prior else None)
            ratios["gross_margin_ttm_pct"] = {
                "value": m,
                "trend": _trend(None if mp is None else m - mp, 0.5),
                "xbrl_tag": "GrossProfit / revenue", "period_days": 365,
                "note": "TTM gross margin %s%%%s" % (
                    m, " vs %.2f%% prior TTM" % mp if mp is not None else ""),
            }
    except Exception as e:
        omitted["gross_margin_ttm_pct"] = str(e)

    # TTM operating margin (durable operating-leverage read) + EXPANSION/COMPRESSION.
    # The trend was the literal string "flat" here, which read as "margins are stable"
    # when nothing had been compared. Operating leverage is the single most useful
    # fundamental direction for a 3-90 day swing, so it gets a real year-ago margin.
    try:
        if ttm_op is not None and ttm_rev:
            tr, suffix = _margin_trend(ttm_op, ttm_rev, ttm_op_prior, ttm_rev_prior,
                                       label="TTM operating margin")
            ratios["operating_margin_ttm_pct"] = {
                "value": round(100.0 * ttm_op / ttm_rev, 2), "trend": tr,
                "xbrl_tag": "OperatingIncomeLoss / revenue", "period_days": 365,
                "note": "TTM operating margin, ending %s%s" % (ttm_rev_end, suffix),
            }
    except Exception as e:
        omitted["operating_margin_ttm_pct"] = str(e)

    # Interest coverage = TTM EBIT (operating income) / TTM interest expense.
    try:
        int_q = _quarterize_flow(_series(gaap, CONCEPT_TAGS["interest_expense"],
                                         instant=False, keep=12))
        ttm_int, _, ttm_int_prior = _ttm(int_q)
        if ttm_op is not None and ttm_int:
            cov = round(ttm_op / abs(ttm_int), 2)
            # Coverage DIRECTION matters as much as the level: 3.2x falling from 6x is
            # a different company than 3.2x rising from 2x, and "flat" hid both.
            cov_prior = (ttm_op_prior / abs(ttm_int_prior)
                         if ttm_op_prior is not None and ttm_int_prior else None)
            _cov_tr, _cov_sfx = _level_trend(cov, cov_prior, flat_band_pct=10.0,
                                             label="coverage")
            ratios["interest_coverage"] = {
                "value": cov, "trend": _cov_tr,
                "xbrl_tag": "OperatingIncomeLoss / InterestExpense", "period_days": 365,
                "note": "TTM EBIT covers interest %sx%s%s" % (
                    cov, " - THIN (<3x)" if cov < 3 else "", _cov_sfx),
            }
        else:
            omitted["interest_coverage"] = "no TTM interest expense reported (may be debt-free)"
    except Exception as e:
        omitted["interest_coverage"] = str(e)

    # Buyback proxy: TTM common-stock repurchases (yield = this / market cap).
    try:
        bb_q = _quarterize_flow(_series(gaap, CONCEPT_TAGS["share_buybacks"],
                                        instant=False, keep=12))
        ttm_bb, bb_end, _ = _ttm(bb_q)
        if ttm_bb is not None:
            ratios["buyback_ttm_usd"] = {
                "value": ttm_bb, "trend": "unknown",
                "xbrl_tag": "PaymentsForRepurchaseOfCommonStock", "period_days": 365,
                "note": "TTM common-stock repurchases ending %s; buyback yield = this "
                        "/ market cap (divide by the market cap in your bundle)" % bb_end,
            }
        else:
            omitted["buyback_ttm_usd"] = "no repurchase cash flow reported (no buyback program)"
    except Exception as e:
        omitted["buyback_ttm_usd"] = str(e)

    # ==================== ENTERPRISE-VALUE / LEVERAGE LAYER ====================
    # The EV and leverage math a swing thesis actually turns on. net_debt/EBITDA
    # needs NO price and is the single highest-value line here (a 4x+ levered long
    # with a thin catalyst is a different trade than a net-cash compounder); the EV
    # and market-cap ratios need a live price, fetched fail-soft via yfinance. All
    # PRE-COMPUTED — the brain TRUSTS these and never recomputes. A missing input
    # emits value:null + a short reason label, never an omission that reads as zero.

    # -- TTM EBITDA = TTM operating income (EBIT) + TTM D&A add-back ------------
    ebitda_ttm = None
    try:
        da_rows, da_tag = _series_tagged(gaap, DA_TAGS, instant=False, keep=12)
        ttm_da, ttm_da_end, _ = _ttm(_quarterize_flow(da_rows))
        if ttm_op is None:
            ratios["ebitda_ttm_usd"] = {
                "value": None, "trend": "unknown",
                "reason": "operating_income_ttm_unavailable",
                "note": "EBITDA needs a TTM operating income (OperatingIncomeLoss); "
                        "fewer than 4 clean quarters were available",
            }
        elif ttm_da is None:
            ratios["ebitda_ttm_usd"] = {
                "value": None, "trend": "unknown", "reason": "d_and_a_unavailable",
                "note": "EBITDA not computed: no trailing-twelve-month D&A add-back "
                        "found (tried %s); TTM EBIT (operating income) was %s" % (
                            "/".join(DA_TAGS), ttm_op),
            }
        else:
            ebitda_ttm = ttm_op + ttm_da
            da_src = ("%s (depreciation-only — understates EBITDA by the "
                      "amortization it omits)" % da_tag) if da_tag == "Depreciation" else da_tag
            ratios["ebitda_ttm_usd"] = {
                "value": ebitda_ttm, "trend": "unknown",
                "xbrl_tags": "OperatingIncomeLoss + %s" % da_tag, "period_days": 365,
                "components": {"ttm_operating_income_usd": ttm_op,
                               "ttm_d_and_a_usd": ttm_da, "d_and_a_tag": da_tag},
                "note": "TTM EBITDA (USD) = TTM operating income %s + TTM D&A %s "
                        "[%s], ending %s" % (ttm_op, ttm_da, da_src,
                                             ttm_da_end or ttm_rev_end),
            }
    except Exception as e:
        ratios["ebitda_ttm_usd"] = {"value": None, "trend": "unknown",
                                    "reason": "error", "note": str(e)}

    # -- total debt / cash / net debt (all XBRL, NO price needed) ---------------
    debt_c = _compute_total_debt(gaap)
    total_debt_val = debt_c["total_debt"] if debt_c else 0.0
    cash_now = _latest(cash).get("value")
    net_debt_val = (total_debt_val - cash_now) if cash_now is not None else None

    # -- net_debt / EBITDA : highest-value leverage read, price-free -----------
    try:
        ratio, label, reason = _leverage_read(net_debt_val, ebitda_ttm)
        if label is None:
            note = ("net debt / TTM EBITDA not computed (%s): net debt = total debt "
                    "%s minus cash %s = %s; TTM EBITDA = %s" % (
                        reason, total_debt_val, cash_now, net_debt_val, ebitda_ttm))
            if reason == "ebitda_non_positive":
                note += (" — net debt against a non-positive EBITDA cannot be "
                         "de-levered from operations; treat as HIGH leverage risk, "
                         "NOT zero")
            ratios["net_debt_to_ebitda"] = {"value": None, "trend": "unknown",
                                            "reason": reason, "note": note}
        else:
            shown = ("%sx" % ratio if ratio is not None
                     else "n/m (net cash, EBITDA non-positive)")
            ratios["net_debt_to_ebitda"] = {
                "value": ratio, "trend": "unknown", "leverage_read": label,
                "xbrl_tags": "(total_debt - cash) / (OperatingIncomeLoss + D&A) TTM",
                "period_days": 365,
                "note": "net debt (total debt %s - cash %s = %s) / TTM EBITDA %s = %s "
                        "[read=%s; scale: net_cash / conservative <2x / moderate "
                        "2-3x / levered >3x]" % (total_debt_val, cash_now,
                                                 net_debt_val, ebitda_ttm, shown, label),
            }
    except Exception as e:
        ratios["net_debt_to_ebitda"] = {"value": None, "trend": "unknown",
                                        "reason": "error", "note": str(e)}

    # -- price-dependent: market cap, EV, EV/EBITDA, buyback yield --------------
    # yfinance is a CLOUD-only dependency (absent in the local/offline env); the
    # whole block degrades to reason "price_unavailable" rather than raising, and
    # the price-free net_debt_to_ebitda above still stands.
    _mc_keys = ("market_cap_usd", "enterprise_value_usd", "ev_to_ebitda",
                "buyback_yield_pct")
    try:
        latest_shares = _latest(shares).get("value")
        price = price_asof = None
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="5d")
            if hist is not None and not hist.empty:
                price = float(hist["Close"].iloc[-1])
                price_asof = str(hist.index[-1].date())
        except Exception:
            price = None

        market_cap = (price * latest_shares
                      if (price is not None and latest_shares) else None)
        if market_cap is None:
            why = "price_unavailable" if latest_shares else "diluted_shares_unavailable"
            for k in _mc_keys:
                ratios[k] = {
                    "value": None, "trend": "unknown", "reason": why,
                    "note": "needs price x diluted shares; %s (the price-free "
                            "net_debt_to_ebitda above is unaffected)" % why,
                }
        else:
            ratios["market_cap_usd"] = {
                "value": market_cap, "trend": "unknown",
                "xbrl_tags": "yfinance close x "
                             "WeightedAverageNumberOfDilutedSharesOutstanding",
                "note": "market cap (USD) = %s close %s (%s) x %s weighted-avg diluted "
                        "shares — approximate vs point-in-time shares outstanding" % (
                            ticker.upper(), round(price, 2), price_asof, latest_shares),
            }
            ev = _enterprise_value(market_cap, total_debt_val, cash_now)
            ratios["enterprise_value_usd"] = {
                "value": ev, "trend": "unknown",
                "xbrl_tags": "market_cap + total_debt - cash",
                "note": "EV (USD) = market cap %s + total debt %s - cash %s" % (
                    round(market_cap, 2), total_debt_val, cash_now),
            }
            if ebitda_ttm is not None and ebitda_ttm > 0:
                ev_ebitda = round(ev / ebitda_ttm, 2)
                ratios["ev_to_ebitda"] = {
                    "value": ev_ebitda, "trend": "unknown",
                    "xbrl_tags": "enterprise_value / (OperatingIncomeLoss + D&A) TTM",
                    "period_days": 365,
                    "note": "EV/EBITDA = %sx (EV %s / TTM EBITDA %s)" % (
                        ev_ebitda, round(ev, 2), ebitda_ttm),
                }
            else:
                why = ("ebitda_non_positive" if ebitda_ttm is not None
                       else "ebitda_unavailable")
                ratios["ev_to_ebitda"] = {
                    "value": None, "trend": "unknown", "reason": why,
                    "note": "EV/EBITDA not computable: TTM EBITDA is %s" % (
                        "non-positive" if ebitda_ttm is not None else "unavailable"),
                }
            bb = ratios.get("buyback_ttm_usd", {}).get("value")
            if bb is not None:
                by = round(100.0 * bb / market_cap, 2)
                ratios["buyback_yield_pct"] = {
                    "value": by, "trend": "unknown",
                    "xbrl_tags": "PaymentsForRepurchaseOfCommonStock TTM "
                                 "/ market_cap x 100",
                    "note": "buyback yield = TTM repurchases %s / market cap %s = %s%% "
                            "(cash returned via buybacks; add dividend yield for total "
                            "shareholder yield)" % (bb, round(market_cap, 2), by),
                }
            else:
                ratios["buyback_yield_pct"] = {
                    "value": None, "trend": "unknown", "reason": "no_buyback_ttm",
                    "note": "no TTM repurchase cash flow reported (no active buyback)",
                }
    except Exception as e:
        for k in _mc_keys:
            ratios.setdefault(k, {"value": None, "trend": "unknown",
                                  "reason": "error", "note": str(e)})

    quality = {"ratios": ratios}
    if omitted:
        quality["omitted"] = omitted

    # Non-USD reporters (TSM in TWD, ASML in EUR): the USD unit filter yields
    # empty series by design - LABEL it so blank ratios read as "not translatable",
    # never as "no data exists".
    currency_note = None
    ccys = _reporting_currencies(gaap)
    if ccys and "USD" not in ccys:
        currency_note = (f"filer reports in {sorted(ccys)}; USD-denominated deep "
                         f"fundamentals unavailable - do not fabricate USD figures")

    return {
        "status": "ok",
        "ticker": ticker.upper(),
        "cik": cik,
        "company": facts.get("entityName"),
        "currency_note": currency_note,
        "note": ("quarterly_facts: instant rows are balance-sheet snapshots; "
                 "duration rows include period_days - rows spanning >100 days are "
                 "YTD-cumulative, not single quarters. quality_ratios are "
                 "pre-computed - do not recompute them."),
        "quarterly_facts": quarterly,
        "quality_ratios": quality,
    }


def get_deep_fundamentals_bundle(tickers) -> dict:
    """Deep fundamentals for several names UNDER the uniform feed-status envelope.

    THE INCIDENT THIS PREVENTS: the bundle's `deep_fundamentals` key is built as
    a bare per-ticker dict (runlib/context_gather.py:255) with no top-level
    status, exactly like `sec_filings` was. get_deep_fundamentals() itself is
    honest - it returns {"status": "error", ...} when companyfacts is
    unreachable - but nothing ever aggregated those, so a total XBRL outage
    presented as a map of error stubs that no automated check inspected. The
    brain then saw every quality_ratio missing and had no way to tell
    "companyfacts is down" from "this company has no ratios".

    Backward compatible: the per-ticker entries stay at the top level under
    their ticker keys, so `deep_fundamentals["NVDA"]` keeps resolving.
    """
    from tools.freshness_audit import stamp_feed

    ts = [str(t).upper() for t in (tickers or []) if t]
    entries: dict = {}
    failures: dict = {}
    ok = 0
    for t in ts:
        try:
            e = get_deep_fundamentals(t)
        except Exception as exc:  # fail-soft by contract; never trust that
            e = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        entries[t] = e
        if e.get("status") == "ok":
            ok += 1
        else:
            failures[t] = str(e.get("reason") or e.get("status"))[:120]
    out: dict = dict(entries)
    out["entries"] = entries
    out["note"] = ("XBRL deep fundamentals. status/coverage describe the FETCH: "
                   "'error' means companyfacts was unreachable for every name, so "
                   "missing ratios are UNREAD, not absent. Never reason from a "
                   "blank balance sheet without checking this status.")
    return stamp_feed(out, len(ts), ok, len(ts) - ok,
                      failures=dict(list(failures.items())[:10]))


if __name__ == "__main__":
    import sys
    print(json.dumps(get_deep_fundamentals(sys.argv[1] if len(sys.argv) > 1 else "NOW"),
                     indent=2))
