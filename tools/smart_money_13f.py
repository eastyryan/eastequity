"""13F Smart Money Tool — tracks what elite funds did last quarter in our universe.

Uses keyless EDGAR full-text + submissions APIs to pull the most recent 13F-HR
information-table XML for a curated list of respected managers, then reports
their positions in tickers we care about. 13Fs are ~45 days delayed — fine for
swing context (conviction/positioning), useless for timing; the brain is told so.

CLI: python -m tools.smart_money_13f NVDA VRT
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from xml.etree import ElementTree

import requests

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}

# Curated managers worth tracking for AI/semis positioning. CIKs are stable.
MANAGERS = {
    "Berkshire Hathaway": "0001067983",
    "Appaloosa (Tepper)": "0001656456",
    "Coatue Management": "0001135730",
    "Tiger Global": "0001167483",
    "D1 Capital": "0001747057",
    "Altimeter Capital": "0001541617",
    "Duquesne (Druckenmiller)": "0001536411",
    "Third Point (Loeb)": "0001040273",
    "ValueAct": "0001418814",
}


def _get(url: str) -> requests.Response:
    time.sleep(0.15)
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r


def _latest_13f_positions(cik: str) -> tuple[str, list[dict]]:
    """Return (period, positions) from a manager's latest 13F-HR info table."""
    sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    recent = sub["filings"]["recent"]
    idx = next((i for i, f in enumerate(recent["form"]) if f in ("13F-HR", "13F-HR/A")), None)
    if idx is None:
        return "", []
    acc = recent["accessionNumber"][idx].replace("-", "")
    period = recent["reportDate"][idx]
    listing = _get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/").text
    xml_files = re.findall(r'href="([^"]+\.xml)"', listing)
    info_xml = next((f for f in xml_files if "primary_doc" not in f.lower()), None)
    if info_xml is None:
        return period, []
    url = info_xml if info_xml.startswith("http") else \
        f"https://www.sec.gov{info_xml}" if info_xml.startswith("/") else \
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{info_xml}"
    root = ElementTree.fromstring(_get(url).content)
    ns = {"n": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    tag = "n:infoTable" if ns else "infoTable"
    positions = []
    for it in root.iterfind(f".//{tag}", ns):
        def txt(path: str) -> str:
            el = it.find(f"n:{path}" if ns else path, ns)
            return el.text if el is not None and el.text else ""
        positions.append({
            "issuer": txt("nameOfIssuer"),
            "value_usd": int(float(txt("value") or 0)),
            "shares": int(float(txt("shrsOrPrnAmt/sshPrnamt") or 0)),
            "put_call": txt("putCall"),
        })
    return period, positions


def get_smart_money(tickers: list[str], company_names: dict[str, str] | None = None) -> dict:
    """Report curated managers' holdings matching our tickers (by issuer-name match)."""
    from tools.sec_filings import ticker_to_cik  # reuse mapping to get names
    names = company_names or {}
    for t in tickers:
        if t not in names:
            cik = ticker_to_cik(t)
            if cik:
                try:
                    sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
                    names[t] = sub.get("name", t)
                except Exception:
                    names[t] = t

    out = {"status": "ok", "tickers": tickers,
           "note": "13F data is ~45 days stale. Use for conviction/positioning context only, never timing.",
           "holdings": []}
    for mgr, cik in MANAGERS.items():
        try:
            period, positions = _latest_13f_positions(cik)
            for t in tickers:
                name_words = set(re.sub(r"[^A-Z ]", "", names.get(t, t).upper()).split())
                for pos in positions:
                    issuer = pos["issuer"].upper()
                    if pos["put_call"]:
                        continue  # ignore options positions entirely
                    first_word = names.get(t, t).upper().split()[0]
                    if first_word in issuer and len(first_word) > 3 or issuer in name_words:
                        out["holdings"].append({"manager": mgr, "period": period,
                                                "ticker": t, **pos})
        except Exception as e:
            out["holdings"].append({"manager": mgr, "error": str(e)})
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_smart_money(sys.argv[1:] or ["NVDA"]), indent=2))
