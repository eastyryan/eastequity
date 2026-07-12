"""Insider Transactions Tool — SEC Form 4 open-market buys/sells, keyless via EDGAR.

Clustered open-market buying by executives is one of the few reliable public
signals at swing horizons; routine sales mostly are not. This tool parses the
last few Form 4 XMLs per ticker and summarizes only open-market transactions
(code P = purchase, S = sale), ignoring option exercises, grants, and gifts.

CLI: python -m tools.insider_form4 DELL
"""

from __future__ import annotations

import json
import re
import time
from xml.etree import ElementTree

import requests

HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}
MAX_FILINGS_PER_TICKER = 6


def _get(url: str) -> requests.Response:
    from tools.net import get_sec
    return get_sec(url)


ENTITY_MARKERS = ("L.P.", "LP", "LLC", "L.L.C", "FUND", "CAPITAL", "PARTNERS",
                  "HOLDINGS", "TRUST", "MANAGEMENT", "INVESTORS", "GROUP INC")


def _is_entity(name: str) -> bool:
    up = name.upper()
    return any(m in up for m in ENTITY_MARKERS)


def _parse_form4(xml_text: str) -> list[dict]:
    # Forms 4/5 XML (ownershipDocument) is namespace-free.
    root = ElementTree.fromstring(xml_text.encode())
    owner = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "?").title()
    role = "officer" if root.findtext(".//isOfficer") == "1" else \
           "director" if root.findtext(".//isDirector") == "1" else \
           "ten_pct_owner" if root.findtext(".//isTenPercentOwner") == "1" else "other"
    title = root.findtext(".//officerTitle") or role
    # Post-2023 checkbox: transaction made under a pre-scheduled 10b5-1 plan.
    plan_10b5_1 = (root.findtext(".//aff10b5One") or "").strip().lower() in ("1", "true")
    out = []
    for tx in root.iterfind(".//nonDerivativeTransaction"):
        code = tx.findtext(".//transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue  # only open-market purchases/sales
        try:
            shares = float(tx.findtext(".//transactionShares/value") or 0)
            price = float(tx.findtext(".//transactionPricePerShare/value") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "insider": owner, "title": title, "role": role,
            "type": "BUY" if code == "P" else "SELL",
            "shares": shares, "price": price,
            "notional_usd": round(shares * price, 2),
            "date": tx.findtext(".//transactionDate/value"),
            "is_10b5_1_plan": plan_10b5_1,
            "is_institutional_entity": _is_entity(owner),
        })
    return out


def get_insider_activity(tickers: list[str]) -> dict:
    from tools.sec_filings import ticker_to_cik

    out = {"status": "ok",
           "note": "Forms 4/5 open-market trades (codes P/S), classified. THE SIGNAL FIELD "
                   "IS PRE-COMPUTED: bullish_cluster_buying (2+ officers/directors buying "
                   "discretionarily) is the strongest positive; routine_or_sponsor_selling_only "
                   "is usually noise - do not treat it as bearish by itself. "
                   "notable_discretionary_selling (>$1M non-plan officer sales) deserves a "
                   "sentence in your risk map. Form 3 count = new insiders registering.",
           "tickers": {}}
    for t in tickers:
        cik = ticker_to_cik(t)
        entry: dict = {"transactions": [], "summary": None}
        if cik is None:
            entry["error"] = "no CIK"
            out["tickers"][t.upper()] = entry
            continue
        try:
            sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
            recent = sub["filings"]["recent"]
            count = 0
            for form, accession, doc in zip(recent["form"], recent["accessionNumber"],
                                            recent["primaryDocument"]):
                if form not in ("4", "5") or count >= MAX_FILINGS_PER_TICKER:
                    if form == "3":
                        entry["new_insider_form3_filings"] = entry.get(
                            "new_insider_form3_filings", 0) + 1
                    continue
                count += 1
                acc = accession.replace("-", "")
                # primaryDocument may be an .html wrapper; the XML sits alongside it.
                xml_doc = re.sub(r"\.html?$", ".xml", doc) if not doc.endswith(".xml") else doc
                xml_doc = xml_doc.split("/")[-1]
                try:
                    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{xml_doc}"
                    entry["transactions"].extend(_parse_form4(_get(url).text))
                except Exception:
                    continue
            buys = [x for x in entry["transactions"] if x["type"] == "BUY"]
            sells = [x for x in entry["transactions"] if x["type"] == "SELL"]
            # The signal that matters: discretionary (non-plan) trades by actual
            # people who run the company - not sponsor distributions, not
            # pre-scheduled plan sales.
            disc_buys = [x for x in buys if not x["is_10b5_1_plan"]
                         and not x["is_institutional_entity"]
                         and x["role"] in ("officer", "director")]
            disc_sells = [x for x in sells if not x["is_10b5_1_plan"]
                          and not x["is_institutional_entity"]
                          and x["role"] in ("officer", "director")]
            distinct_disc_buyers = len({x["insider"] for x in disc_buys})
            if distinct_disc_buyers >= 2:
                signal = "bullish_cluster_buying"
            elif disc_buys:
                signal = "insider_buying"
            elif disc_sells and sum(x["notional_usd"] for x in disc_sells) > 1_000_000:
                signal = "notable_discretionary_selling"
            elif sells:
                signal = "routine_or_sponsor_selling_only"
            else:
                signal = "no_open_market_activity"
            entry["summary"] = {
                "signal": signal,
                "open_market_buys": len(buys),
                "open_market_sells": len(sells),
                "buy_notional_usd": round(sum(x["notional_usd"] for x in buys), 2),
                "sell_notional_usd": round(sum(x["notional_usd"] for x in sells), 2),
                "discretionary_officer_dir_buys": len(disc_buys),
                "discretionary_officer_dir_sells": len(disc_sells),
                "distinct_discretionary_buyers": distinct_disc_buyers,
                "plan_10b5_1_trades": sum(1 for x in entry["transactions"] if x["is_10b5_1_plan"]),
                "institutional_entity_trades": sum(
                    1 for x in entry["transactions"] if x["is_institutional_entity"]),
            }
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        out["tickers"][t.upper()] = entry
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_insider_activity(sys.argv[1:] or ["DELL"]), indent=2))
