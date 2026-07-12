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
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}  # SEC requires contact info


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


def _annualish(units: list[dict], quarterly: bool) -> list[dict]:
    """Filter XBRL facts to single-quarter (not YTD-cumulative) or annual rows, newest last."""
    from datetime import date

    def span_days(u: dict) -> int:
        try:
            return (date.fromisoformat(u["end"]) - date.fromisoformat(u["start"])).days
        except (KeyError, ValueError):
            return -1

    if quarterly:
        rows = [u for u in units if u.get("form") in ("10-Q", "10-K")
                and u.get("frame") is None and 80 <= span_days(u) <= 100]
    else:
        rows = [u for u in units if u.get("form") == "10-K"
                and u.get("frame") is None and span_days(u) > 300]
    seen, out = set(), []
    for u in sorted(rows, key=lambda u: u.get("end", "")):
        key = (u.get("start"), u.get("end"))
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out[-6:]


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
        for form, date, accession, doc in zip(recent["form"], recent["filingDate"],
                                              recent["accessionNumber"], recent["primaryDocument"]):
            if form in ("10-K", "10-Q", "8-K") and len(filings) < 8:
                acc = accession.replace("-", "")
                filings.append({
                    "form": form, "filed": date,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
                })

        facts_out = {}
        try:
            facts = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
            gaap = facts.get("facts", {}).get("us-gaap", {})
            for label, tags in {
                "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
                "net_income": ["NetIncomeLoss"],
                "gross_profit": ["GrossProfit"],
                "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
            }.items():
                for tag in tags:
                    units = gaap.get(tag, {}).get("units", {}).get("USD")
                    if units:
                        facts_out[label] = [
                            {"period_end": u["end"], "value_usd": u["val"], "form": u["form"]}
                            for u in _annualish(units, quarterly=True)
                        ]
                        break
            # Balance sheet, cash flow, dilution: the "is this business sound"
            # layer. Instant facts have no start date; flow facts may be YTD -
            # period_days lets the brain interpret them correctly.
            for label, tags in {
                "cash_and_equivalents": ["CashAndCashEquivalentsAtCarryingValue"],
                "long_term_debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
                "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
                "stock_based_compensation": ["ShareBasedCompensation"],
                "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
            }.items():
                for tag in tags:
                    unit_map = gaap.get(tag, {}).get("units", {})
                    units = unit_map.get("USD") or unit_map.get("shares")
                    if units:
                        rows = [u for u in units if u.get("form") in ("10-Q", "10-K")
                                and u.get("frame") is None]
                        seen, out_rows = set(), []
                        for u in sorted(rows, key=lambda u: u.get("end", "")):
                            key = (u.get("start"), u.get("end"))
                            if key in seen:
                                continue
                            seen.add(key)
                            row = {"period_end": u["end"], "value": u["val"], "form": u["form"]}
                            if u.get("start"):
                                from datetime import date
                                try:
                                    row["period_days"] = (date.fromisoformat(u["end"])
                                                          - date.fromisoformat(u["start"])).days
                                except ValueError:
                                    pass
                            out_rows.append(row)
                        if out_rows:
                            facts_out[label] = out_rows[-6:]
                        break
        except Exception as e:
            facts_out = {"error": f"companyfacts unavailable: {e}"}

        return {"status": "ok", "ticker": ticker.upper(), "cik": cik,
                "company": sub.get("name"), "recent_filings": filings,
                "quarterly_fundamentals": facts_out}
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
