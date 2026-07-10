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
    time.sleep(0.15)
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r


def _parse_form4(xml_text: str) -> list[dict]:
    # Form 4 XML (ownershipDocument) is namespace-free.
    root = ElementTree.fromstring(xml_text.encode())
    owner = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "?").title()
    role = "officer" if root.findtext(".//isOfficer") == "1" else \
           "director" if root.findtext(".//isDirector") == "1" else "other"
    title = root.findtext(".//officerTitle") or role
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
            "insider": owner, "title": title,
            "type": "BUY" if code == "P" else "SELL",
            "shares": shares, "price": price,
            "notional_usd": round(shares * price, 2),
            "date": tx.findtext(".//transactionDate/value"),
        })
    return out


def get_insider_activity(tickers: list[str]) -> dict:
    from tools.sec_filings import ticker_to_cik

    out = {"status": "ok",
           "note": "Open-market Form 4 transactions only (codes P/S). Clustered officer "
                   "buying is a strong signal; routine selling usually is not.",
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
                if form != "4" or count >= MAX_FILINGS_PER_TICKER:
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
            entry["summary"] = {
                "open_market_buys": len(buys),
                "open_market_sells": len(sells),
                "buy_notional_usd": round(sum(x["notional_usd"] for x in buys), 2),
                "sell_notional_usd": round(sum(x["notional_usd"] for x in sells), 2),
                "distinct_buyers": len({x["insider"] for x in buys}),
            }
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        out["tickers"][t.upper()] = entry
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_insider_activity(sys.argv[1:] or ["DELL"]), indent=2))
