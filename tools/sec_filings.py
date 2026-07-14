"""SEC Filings Tool — keyless EDGAR access for 10-K / 10-Q swing research.

Uses SEC's free JSON APIs (no API key; just a User-Agent header, per SEC rules):
  * company_tickers.json           — ticker -> CIK mapping (cached)
  * submissions/CIK##########.json — recent filings index
  * companyfacts (XBRL)            — standardized fundamentals time series

get_filing_brief(ticker) returns links to the latest 10-K/10-Q plus a compact
fundamentals trend (revenue, net income, margins, capex) so Claude can spot
multi-week-relevant inflections without downloading 300-page documents. When a
deeper read is needed, the orchestrator can fetch the filing document itself
via `download_latest_filing()` and hand sections to Claude.

CLI: python -m tools.sec_filings NVDA
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}  # SEC requires contact info

# Shared companyfacts cache. The multi-MB companyfacts payload was otherwise
# fetched ~3x per focus ticker (get_filing_brief + fundamentals.get_deep_fundamentals
# + guidance_ledger.auto_grade); one TTL-cached fetch per CIK cuts EDGAR load 3x.
# Keyed by zero-padded CIK. Thread-safe enough for the single-process orchestrator
# (the network fetch happens outside the lock; a duplicate concurrent fetch is
# harmless). Errors are never cached, so a transient outage self-heals next call.
_FACTS_CACHE: dict = {}
_FACTS_LOCK = threading.Lock()
_FACTS_TTL_SECONDS = 12 * 60  # fresh within a run, expired between runs (~3h apart)


def _get(url: str) -> dict:
    from tools.net import get_sec
    return get_sec(url).json()


def ticker_to_cik(ticker: str) -> str | None:
    cache_file = CACHE / "company_tickers.json"
    try:
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 7 * 86400:
            data = json.loads(cache_file.read_text())
        else:
            data = _get("https://www.sec.gov/files/company_tickers.json")
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data))
    except Exception:
        return None
    for row in data.values():
        if row["ticker"].upper() == ticker.upper():
            return str(row["cik_str"]).zfill(10)
    return None


def get_company_facts(cik_or_ticker) -> dict:
    """Fetch + TTL-cache the EDGAR companyfacts JSON once per CIK.

    Shared entrypoint so sec_filings, fundamentals and guidance_ledger reuse a
    single fetch per focus ticker per run instead of three. Accepts a ticker
    symbol or a CIK (int or zero-padded str). Raises on a fetch/parse failure —
    callers keep their own fail-soft try/except and a failure is never cached.
    """
    raw = str(cik_or_ticker).strip()
    if raw.isdigit():
        cik = raw.zfill(10)
    else:
        cik = ticker_to_cik(raw)
        if cik is None:
            raise ValueError(f"no CIK found for {cik_or_ticker}")
    now = time.time()
    with _FACTS_LOCK:
        hit = _FACTS_CACHE.get(cik)
        if hit is not None and (now - hit[0]) < _FACTS_TTL_SECONDS:
            return hit[1]
    # Fetch outside the lock (multi-second network); store on success only.
    data = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    with _FACTS_LOCK:
        _FACTS_CACHE[cik] = (time.time(), data)
    return data


def merged_facts(facts: dict) -> dict:
    """One fact dict across taxonomies: us-gaap merged OVER ifrs-full (us-gaap
    wins tag-name collisions). Foreign private issuers (TSM/ASML/ARM) file
    under IFRS, so reading us-gaap alone made them invisible. Downstream unit
    selection stays USD-only — non-USD reporters yield empty series, labeled
    via reporting_currencies() instead of silently blank."""
    f = facts.get("facts", {})
    merged = dict(f.get("ifrs-full") or {})
    merged.update(f.get("us-gaap") or {})
    return merged


def reporting_currencies(fact_dict: dict) -> set:
    """3-letter currency units present across a company's monetary facts."""
    ccys: set = set()
    for tag_data in fact_dict.values():
        for unit in (tag_data.get("units") or {}):
            u = str(unit).split("/")[0]
            if len(u) == 3 and u.isalpha() and u.isupper():
                ccys.add(u)
    return ccys


def evict_facts(cik_or_ticker) -> None:
    """Drop one company's facts from the in-memory cache. Full-universe sweeps
    (~111 multi-MB payloads) would otherwise hold gigabytes in RAM."""
    raw = str(cik_or_ticker).strip()
    cik = raw.zfill(10) if raw.isdigit() else ticker_to_cik(raw)
    if cik:
        with _FACTS_LOCK:
            _FACTS_CACHE.pop(cik, None)


def dedupe_facts(units: list[dict],
                 forms=("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F", "6-K"),
                 min_span: int | None = None, max_span: int | None = None) -> list[dict]:
    """Canonical XBRL fact selection: filter by form and (optionally) duration span,
    dedupe by (start, end) PREFERRING THE LATEST-FILED entry, sorted newest-last.

    CRITICAL: never filter on the `frame` field. SEC frame annotations are NOT
    duplicates - for recently reported quarters the frame-annotated entry is often
    the ONLY copy (older quarters get unframed twins later, as comparatives in
    subsequent filings). A `frame is None` filter silently serves YEAR-OLD numbers
    for any company that stopped double-reporting discrete quarter rows - exactly
    how DELL's latest revenue showed $23.4B (Q1 FY2026) when the filed 10-Q said
    $43.8B (Q1 FY2027). Dedup by period handles genuine duplicates; latest-filed
    wins so restatements/amendments take precedence."""
    from datetime import date

    def span_days(u: dict) -> int | None:
        try:
            return (date.fromisoformat(u["end"]) - date.fromisoformat(u["start"])).days
        except (KeyError, ValueError, TypeError):
            return None

    by_period: dict = {}
    for u in units:
        if u.get("form") not in forms:
            continue
        if min_span is not None or max_span is not None:
            d = span_days(u)
            if d is None:
                continue
            if min_span is not None and d < min_span:
                continue
            if max_span is not None and d > max_span:
                continue
        key = (u.get("start"), u.get("end"))
        prev = by_period.get(key)
        if prev is None or (u.get("filed") or "") > (prev.get("filed") or ""):
            by_period[key] = u
    return sorted(by_period.values(), key=lambda u: (u.get("end", ""), u.get("start") or ""))


def _annualish(units: list[dict], quarterly: bool) -> list[dict]:
    """Single-quarter (not YTD-cumulative) or annual rows, newest last, via dedupe_facts.
    Annual accepts 20-F/40-F so foreign private issuers (TSM, ASML, ARM...) aren't blank."""
    if quarterly:
        return dedupe_facts(units, forms=("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F", "6-K"),
                            min_span=80, max_span=100)[-6:]
    return dedupe_facts(units, forms=("10-K", "10-K/A", "20-F", "40-F"), min_span=300)[-6:]


def get_filing_brief(ticker: str) -> dict:
    try:
        cik = ticker_to_cik(ticker)
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    if cik is None:
        return {"status": "error", "reason": f"no CIK found for {ticker}"}
    try:
        sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        recent = sub["filings"]["recent"]
        filings = []
        latest_report = None  # newest 10-Q/10-K: (form, filed, period reportDate)
        report_dates = recent.get("reportDate", [""] * len(recent["form"]))
        for form, fdate, rdate, accession, doc in zip(
                recent["form"], recent["filingDate"], report_dates,
                recent["accessionNumber"], recent["primaryDocument"]):
            # 20-F/40-F: foreign private issuers' annual report (their "10-K").
            if form in ("10-K", "10-Q", "20-F", "40-F") and latest_report is None:
                latest_report = {"form": form, "filed": fdate, "period_end": rdate}
            if form in ("10-K", "10-Q", "8-K", "20-F", "6-K") and len(filings) < 8:
                acc = accession.replace("-", "")
                filings.append({
                    "form": form, "filed": fdate,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
                })

        facts_out = {}
        try:
            facts = get_company_facts(cik)  # shared TTL cache (see get_company_facts)
            gaap = merged_facts(facts)  # us-gaap + ifrs-full (foreign filers)
            # Umbrella concepts FIRST: `Revenues` is the GAAP top line; the ASC-606
            # contract tags are SUBSETS that understate fintechs (MELI: interest income
            # outside contract revenue, -31%) and misstate utilities (TLN: derivative
            # effects). Banks report RevenuesNetOfInterestExpense (JPM's `Revenues` tag
            # died in 2014). Ties on recency go to the earlier (more inclusive) tag;
            # a strictly newer subset tag still wins (ORCL's `Revenues` died in 2022).
            for label, tags in {
                # IFRS synonyms LAST: ties on recency prefer the us-gaap tag;
                # strict recency still lets the IFRS tag win when it is the only
                # live series (foreign private issuers).
                "revenue": ["Revenues", "RevenuesNetOfInterestExpense",
                            "RevenueFromContractWithCustomerExcludingAssessedTax",
                            "RevenueFromContractWithCustomerIncludingAssessedTax",
                            "Revenue", "RevenueFromContractsWithCustomers"],
                "net_income": ["NetIncomeLoss", "ProfitLoss",
                               "ProfitLossAttributableToOwnersOfParent"],
                "gross_profit": ["GrossProfit"],
                "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
                          "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
            }.items():
                # Recency-aware tag choice: a company can switch tags; the first tag
                # with data may have gone stale. Pick the tag whose series is newest;
                # ties go to the earlier (umbrella) tag in the list.
                best_rows, best_end = None, ""
                best_annual, best_annual_end = None, ""
                for tag in tags:
                    units = gaap.get(tag, {}).get("units", {}).get("USD")
                    if not units:
                        continue
                    rows = _annualish(units, quarterly=True)
                    if rows and rows[-1].get("end", "") > best_end:
                        best_rows, best_end = rows, rows[-1].get("end", "")
                    if label == "revenue":  # annual (FY) revenue: freshness + FY context
                        arows = _annualish(units, quarterly=False)
                        if arows and arows[-1].get("end", "") > best_annual_end:
                            best_annual, best_annual_end = arows, arows[-1].get("end", "")
                if best_rows:
                    facts_out[label] = [
                        {"period_end": u["end"], "value_usd": u["val"], "form": u["form"]}
                        for u in best_rows
                    ]
                if label == "revenue" and best_annual:
                    facts_out["annual_revenue"] = [
                        {"period_end": u["end"], "value_usd": u["val"], "form": u["form"]}
                        for u in best_annual[-4:]
                    ]
            # Balance sheet, cash flow, dilution: the "is this business sound"
            # layer. Instant facts have no start date; flow facts may be YTD -
            # period_days lets the brain interpret them correctly.
            from datetime import date as _date
            for label, tags in {
                "cash_and_equivalents": ["CashAndCashEquivalentsAtCarryingValue",
                                         "CashAndCashEquivalents"],
                "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
                "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities",
                                        "CashFlowsFromUsedInOperatingActivities"],
                "stock_based_compensation": ["ShareBasedCompensation"],
                "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding",
                                   "AdjustedWeightedAverageShares"],
            }.items():
                best_rows, best_end = None, ""
                for tag in tags:
                    unit_map = gaap.get(tag, {}).get("units", {})
                    units = unit_map.get("USD") or unit_map.get("shares")
                    if not units:
                        continue
                    rows = dedupe_facts(units)
                    if rows and rows[-1].get("end", "") > best_end:
                        best_rows, best_end = rows, rows[-1].get("end", "")
                if best_rows:
                    out_rows = []
                    for u in best_rows[-6:]:
                        row = {"period_end": u["end"], "value": u["val"], "form": u["form"]}
                        if u.get("start"):
                            try:
                                row["period_days"] = (_date.fromisoformat(u["end"])
                                                      - _date.fromisoformat(u["start"])).days
                            except ValueError:
                                pass
                        out_rows.append(row)
                    facts_out[label] = out_rows
            # Non-USD reporters (TSM in TWD, ASML in EUR): the USD unit filter
            # correctly yields nothing - SAY so instead of looking silently blank.
            ccys = reporting_currencies(gaap)
            if ccys and "USD" not in ccys:
                facts_out["currency_note"] = (
                    f"filer reports in {sorted(ccys)}; USD-denominated fundamentals "
                    f"unavailable - reason from filings prose/estimates, never "
                    f"fabricate USD figures")
        except Exception as e:
            facts_out = {"error": f"companyfacts unavailable: {e}"}

        out = {"status": "ok", "ticker": ticker.upper(), "cik": cik,
               "company": sub.get("name"), "recent_filings": filings,
               "latest_periodic_filing": latest_report,
               "quarterly_fundamentals": facts_out}
        # A facts fetch that failed while submissions succeeded must NOT read as
        # "fresh with no numbers": current_through stays None below, the stale
        # cross-check never fires, and the freshness audit would count the name
        # fresh. Label it explicitly so the bundle and the audit see the truth.
        if "error" in facts_out:
            out["fundamentals_unavailable"] = True
            out["stale_fundamentals_warning"] = (
                "Companyfacts fetch FAILED this run - fundamentals below are "
                "absent, not absent-because-none-exist. Do not treat missing "
                "numbers as data; reason from price/news and say the feed failed.")
        # Freshness cross-check: the submissions index is authoritative about WHICH
        # period the newest 10-Q/10-K covers. If our extracted fundamentals end
        # before that period, something upstream is stale - say so LOUDLY rather
        # than letting year-old numbers pass as current (the DELL $23.4B incident).
        rev = facts_out.get("revenue") or []
        arev = facts_out.get("annual_revenue") or []
        # Current-through considers BOTH cadences: a fiscal-year-end company's latest
        # filing is a 10-K whose discrete Q4 is never filed as a quarterly row (Q4 =
        # FY - Q1 - Q2 - Q3), so the ANNUAL series is what reaches the reportDate
        # (ORCL/CRDO). Quarterly-only would false-alarm every such company.
        q_end = rev[-1]["period_end"] if rev else None
        a_end = arev[-1]["period_end"] if arev else None
        current_through = max(filter(None, [q_end, a_end]), default=None)
        out["fundamentals_current_through"] = current_through
        # Warning is scoped to domestic quarterly filers; foreign private issuers
        # (20-F/40-F) report annually, so a quarterly-series lag is their normal
        # cadence, not a staleness bug - the audit classifies them separately.
        if (latest_report and latest_report.get("form") in ("10-K", "10-Q")
                and latest_report.get("period_end") and current_through):
            if current_through < latest_report["period_end"]:
                out["stale_fundamentals_warning"] = (
                    f"Extracted fundamentals end {current_through} but the latest "
                    f"{latest_report['form']} (filed {latest_report['filed']}) covers the period "
                    f"ending {latest_report['period_end']}. Numbers below are NOT the latest "
                    f"reported quarter - do not present them as current.")
        return out
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}


def download_latest_filing(ticker: str, form: str = "10-Q") -> dict:
    """Download the latest filing document text (truncated) for deep reading."""
    brief = get_filing_brief(ticker)
    if brief.get("status") != "ok":
        return brief
    match = next((f for f in brief["recent_filings"] if f["form"] == form), None)
    if match is None:
        return {"status": "error", "reason": f"no recent {form} for {ticker}"}
    time.sleep(0.15)
    r = requests.get(match["url"], headers=HEADERS, timeout=60)
    r.raise_for_status()
    path = ROOT / "data" / "filings" / f"{ticker.upper()}_{form}_{match['filed']}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r.text)
    return {"status": "ok", "saved_to": str(path), "filed": match["filed"], "url": match["url"]}


if __name__ == "__main__":
    import sys
    print(json.dumps(get_filing_brief(sys.argv[1] if len(sys.argv) > 1 else "NVDA"), indent=2))
