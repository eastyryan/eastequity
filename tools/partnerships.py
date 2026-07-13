"""Strategic partnerships & material deals - the two-sided deal lens.

Surfaces, per focus name, the raw material for partnership analysis:
  1. 8-K MATERIAL-AGREEMENT filings from EDGAR submissions - Item 1.01 is literally
     "Entry into a Material Definitive Agreement" (the SEC's own deal feed); 2.01 is
     a completed acquisition; 8.01 (Other Events) is where many partnership press
     releases land (noisier - labeled so the brain weighs it accordingly).
  2. Partnership-flavored news headlines (yfinance) matched by keyword.

Deliberately shallow: it surfaces dated, linked artifacts; the BRAIN does the
two-sided analysis per CLAUDE.md (quantified or slideware? who needs whom? what
dependency/concentration risk does it create? whose socket was displaced?). A deal
can transform a company - or mark the weak side borrowing credibility.

CLI: python -m tools.partnerships NVDA NOW NBIS
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

# 8-K items worth surfacing, with honest labels for the brain.
ITEM_LABELS = {
    "1.01": "material definitive agreement",
    "2.01": "acquisition/disposition completed",
    "8.01": "other event (often a deal/partnership PR - verify substance)",
}

# Title matcher: tight enough to skip earnings noise, loose enough for real deals.
PARTNERSHIP_RE = re.compile(
    r"partner|alliance|collaborat|joint venture|agreement|multi-?year|design win"
    r"|supply deal|teams? up|strategic invest|expands? .*deal|selected by|contract",
    re.IGNORECASE)

NOTE = (
    "Raw deal artifacts - analyze BOTH sides per CLAUDE.md before treating any of it "
    "as a catalyst: is the value quantified anywhere (revenue, units, duration) or is "
    "it slideware; which side issued the announcement (the weak side borrows "
    "credibility, the strong side allocates); does it create dependency or customer "
    "concentration; whose incumbent socket got displaced; and did the market's "
    "reaction over- or under-price the substance. item 8.01 filings and headlines are "
    "NOISY - a headline is a lead, not a fact.")


def _match_partnership(title) -> bool:
    """Pure: does a headline look deal-related? (unit-testable)"""
    return bool(title) and bool(PARTNERSHIP_RE.search(str(title)))


def _agreement_8ks(recent: dict, cik: str, lookback_days: int = 120) -> list[dict]:
    """Pure-ish: filter a submissions `recent` block to deal-relevant 8-Ks within
    the window. Returns [{filed, items, labels, url}] newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    out = []
    items_list = recent.get("items", [""] * len(recent.get("form", [])))
    for form, fdate, items, accession, doc in zip(
            recent.get("form", []), recent.get("filingDate", []), items_list,
            recent.get("accessionNumber", []), recent.get("primaryDocument", [])):
        if fdate < cutoff:
            continue
        if form == "8-K":
            hit = sorted(set(str(items or "").split(",")) & set(ITEM_LABELS))
            if not hit:
                continue
            labels = [ITEM_LABELS[i] for i in hit]
        elif form == "6-K":
            # Foreign private issuers (NBIS, TSM, ARM...) have no 8-K item taxonomy;
            # their deal disclosures arrive as 6-Ks. Surface them, honestly labeled.
            hit, labels = ["6-K"], ["foreign-filer disclosure (may contain deals - read it)"]
        else:
            continue
        acc = accession.replace("-", "")
        out.append({
            "filed": fdate,
            "items": hit,
            "labels": labels,
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
        })
    return sorted(out, key=lambda r: r["filed"], reverse=True)[:6]


def get_partnerships(tickers: list, lookback_days: int = 120, max_news: int = 5) -> dict:
    """Per-ticker deal artifacts. Fail-soft per name and per source."""
    from tools.sec_filings import _get, ticker_to_cik

    out = {"status": "ok", "note": NOTE, "tickers": {}}
    for t in tickers:
        entry: dict = {"material_agreement_8ks": [], "partnership_headlines": []}
        try:
            cik = ticker_to_cik(t)
            if cik:
                sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json")
                entry["material_agreement_8ks"] = _agreement_8ks(
                    sub.get("filings", {}).get("recent", {}), cik, lookback_days)
        except Exception as e:
            entry["sec_error"] = str(e)[:120]
        try:
            import yfinance as yf
            for item in (yf.Ticker(t).news or []):
                content = item.get("content", item)
                title = content.get("title")
                if _match_partnership(title):
                    entry["partnership_headlines"].append({
                        "title": title,
                        "published": content.get("pubDate") or content.get("providerPublishTime"),
                        "url": (content.get("canonicalUrl") or {}).get("url") or content.get("link"),
                    })
                if len(entry["partnership_headlines"]) >= max_news:
                    break
        except Exception as e:
            entry["news_error"] = str(e)[:120]
        out["tickers"][t.upper()] = entry
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_partnerships(sys.argv[1:] or ["NVDA"]), indent=1, default=str))
