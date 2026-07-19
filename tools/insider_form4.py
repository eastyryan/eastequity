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
from datetime import date, timedelta
from xml.etree import ElementTree

HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}

# Recency: mirror filing_text's 120-day gate so a cluster/selling verdict rests
# on CURRENT trades, not months-old ones presented as fresh.
WINDOW_DAYS = 120
# Only fetch filings filed within this window (bounds EDGAR calls). A Form 4 must
# be filed within 2 business days, so anything reporting an in-window trade was
# itself filed inside this range; the buffer covers late Form 4/5 reports.
SCAN_FILING_DAYS = 135
# Hard cap on Form 4/5 documents parsed per ticker (bounds EDGAR calls for
# prolific filers). Within 120 days this is rarely reached.
MAX_FILINGS_SCAN = 60

# BUNDLE WEIGHT CAP: the raw per-trade dump was >50% of the entire context
# bundle (497KB on 2026-07-14 - sponsor unwinds run to dozens of identical
# rows) and starved the cloud brain's context window until deep-review runs
# died mid-session. The classified summary carries the signal; only the most
# decision-relevant rows are kept for drill-down, with the omission disclosed.
MAX_TRANSACTIONS_IN_BUNDLE = 15


def _get(url: str):
    from tools.net import get_sec
    return get_sec(url)


# Whole-word entity markers. Matched as TOKENS (not naive substrings) so people
# like "RALPH ..." / "PHILIP ..." / "ALPHA ..." are not misread as institutions
# (the old bare-substring "LP" tagged them as entities and dropped them from the
# discretionary buy/sell tallies, suppressing real cluster-buy / selling signals).
_ENTITY_TOKENS = {
    "LP", "LLP", "LLC", "INC", "INCORPORATED", "CORP", "CORPORATION", "LTD",
    "LIMITED", "TRUST", "FUND", "FUNDS", "PARTNERS", "PARTNER", "PARTNERSHIP",
    "CAPITAL", "HOLDINGS", "HOLDING", "MANAGEMENT", "INVESTORS", "GROUP",
    "ASSOCIATES", "VENTURES", "ADVISORS", "ADVISERS", "PLC", "SICAV",
}


def _is_entity(name: str) -> bool:
    up = (name or "").upper()
    # Collapse dotted acronyms ("L.P." -> "LP", "L.L.C." -> "LLC", "L. P." -> "LP")
    # so they tokenize to a single marker word.
    up = re.sub(r"(?:[A-Z]\.\s*){2,}",
                lambda m: re.sub(r"[.\s]", "", m.group(0)), up)
    tokens = set(re.findall(r"[A-Z0-9&]+", up))
    return bool(tokens & _ENTITY_TOKENS)


def _parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except (ValueError, TypeError):
        return None


def _in_window(datestr: str | None, cutoff: date) -> bool:
    """Keep a transaction if its trade date is within the window (or, rarely,
    unparseable - the filing was already gated to the recent scan window)."""
    d = _parse_date(datestr)
    return d is None or d >= cutoff


def _parse_form4(xml_text: str) -> list[dict]:
    # Forms 4/5 XML (ownershipDocument) is namespace-free.
    root = ElementTree.fromstring(xml_text.encode())
    owner = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "?").title()
    role = "officer" if root.findtext(".//isOfficer") == "1" else \
           "director" if root.findtext(".//isDirector") == "1" else \
           "ten_pct_owner" if root.findtext(".//isTenPercentOwner") == "1" else "other"
    title = root.findtext(".//officerTitle") or role
    # Post-2023 checkbox: transaction made under a pre-scheduled 10b5-1 plan.
    plan_flag = (root.findtext(".//aff10b5One") or "").strip().lower() in ("1", "true")
    # Many filers disclose the plan ONLY in a free-text footnote/remark, not the
    # structured checkbox. Scan that prose too, or discretionary-sell counts are
    # overstated (a planned sale wrongly treated as a discretionary decision).
    free_text = " ".join((el.text or "") for el in root.iterfind(".//footnotes/footnote"))
    free_text += " " + (root.findtext(".//remarks") or "")
    plan_10b5_1 = plan_flag or bool(re.search(r"10\s*B5[\-\s]?1", free_text.upper()))
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

    out = {"note": "Forms 4/5 open-market trades (codes P/S) from the LAST 120 DAYS only, "
                   "classified. THE SIGNAL FIELD "
                   "IS PRE-COMPUTED: bullish_cluster_buying (2+ officers/directors buying "
                   "discretionarily) is the strongest positive; routine_or_sponsor_selling_only "
                   "is usually noise - do not treat it as bearish by itself. "
                   "notable_discretionary_selling (>$1M non-plan officer sales) deserves a "
                   "sentence in your risk map. Form 3 count = new insiders registering. "
                   "Entity filers (funds/LPs) and 10b5-1 plan trades (structured flag OR "
                   "footnote text) are excluded from the discretionary tallies. "
                   "DRILL DOWN via by_ticker[T] for the compact signal+summary "
                   "(distinct_discretionary_buyers etc.) WITHOUT paging the raw Form-4 "
                   "dump; tickers[T].transactions holds the full per-trade detail.",
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
            scan_cutoff = date.today() - timedelta(days=SCAN_FILING_DAYS)
            tx_cutoff = date.today() - timedelta(days=WINDOW_DAYS)
            count = 0
            fetch_errors = 0  # Form-4 XMLs we tried and failed to fetch
            for form, filed, accession, doc in zip(
                    recent["form"], recent["filingDate"],
                    recent["accessionNumber"], recent["primaryDocument"]):
                filed_date = _parse_date(filed)
                # recent[] is newest-first: once past the scan window, all the
                # rest are older too - stop. (Recency window, not a fixed count:
                # a real BUY just outside the old 6-filing window is now caught.)
                if filed_date is not None and filed_date < scan_cutoff:
                    break
                if form == "3":
                    entry["new_insider_form3_filings"] = entry.get(
                        "new_insider_form3_filings", 0) + 1
                    continue
                if form not in ("4", "5"):
                    continue
                if count >= MAX_FILINGS_SCAN:
                    break
                count += 1
                acc = accession.replace("-", "")
                # primaryDocument may be an .html wrapper; the XML sits alongside it.
                xml_doc = re.sub(r"\.html?$", ".xml", doc) if not doc.endswith(".xml") else doc
                xml_doc = xml_doc.split("/")[-1]
                try:
                    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{xml_doc}"
                    entry["transactions"].extend(_parse_form4(_get(url).text))
                except Exception:
                    fetch_errors += 1  # throttled/blocked archive - MUST NOT read as "no activity"
                    continue
            # Keep only trades whose TRANSACTION date falls in the recency window;
            # open-market P/S (already the only codes parsed) are never crowded
            # out by grants because grants (codes A/M) are dropped in _parse_form4.
            entry["transactions"] = [
                x for x in entry["transactions"] if _in_window(x.get("date"), tx_cutoff)]
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
            elif fetch_errors:
                # Filings exist but every XML fetch failed: the truth is "we could
                # not read the filings", never "insiders did nothing" - the two
                # mean opposite things to a thesis leaning on insider quiet.
                signal = "data_unavailable"
            else:
                signal = "no_open_market_activity"
            entry["summary"] = {
                "signal": signal,
                "window_days": WINDOW_DAYS,
                "filings_unfetched": fetch_errors,
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
            # Truncate AFTER the summary is computed from the full list, keeping
            # discretionary officer/director trades first, then largest notional.
            full = entry["transactions"]
            if len(full) > MAX_TRANSACTIONS_IN_BUNDLE:
                keep = sorted(
                    full,
                    key=lambda x: (not (x.get("role") in ("officer", "director")
                                        and not x.get("is_10b5_1_plan")
                                        and not x.get("is_institutional_entity")),
                                   -(x.get("notional_usd") or 0)))
                entry["transactions"] = keep[:MAX_TRANSACTIONS_IN_BUNDLE]
                entry["transactions_omitted"] = len(full) - MAX_TRANSACTIONS_IN_BUNDLE
                entry["transactions_note"] = (
                    "raw rows truncated for bundle weight - the summary above is "
                    "computed from ALL rows; omitted rows are routine plan/sponsor flow")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        out["tickers"][t.upper()] = entry

    # Compact per-ticker view at the key the digest points to. Same signal +
    # summary the brain needs to weight a name, minus the (large) raw transaction
    # list - so a drill-down on insider_activity.by_ticker[T] resolves without
    # paging the full Form-4 feed. distinct_discretionary_buyers is surfaced at
    # the top level too (a single $55M buyer is NOT a 2+ cluster - the digest was
    # ambiguous about this on 2026-07-14).
    out["by_ticker"] = {}
    for tk, e in out["tickers"].items():
        summ = e.get("summary") or {}
        out["by_ticker"][tk] = {
            "signal": summ.get("signal"),
            "distinct_discretionary_buyers": summ.get("distinct_discretionary_buyers"),
            "new_insider_form3_filings": e.get("new_insider_form3_filings", 0),
            "summary": summ or None,
            "error": e.get("error"),
        }
    # An all-tickers failure is a FEED outage, not a quiet market: degrade the
    # top-level status so the orchestrator's partial-data label actually fires
    # (it keys on this status) instead of the brain reading a clean empty.
    #
    # COVERAGE (uniform contract, tools/freshness_audit.feed_status): counts are
    # now disclosed even when SOME names came back, so a half-throttled EDGAR no
    # longer looks identical to a clean run. Two distinct failure grades exist
    # here and both are counted:
    #   * `error` on the entry  - the submissions index itself was unreachable;
    #   * signal `data_unavailable` - the index answered but every Form-4 XML
    #     fetch failed, so "we could not read the filings" (NOT "insiders did
    #     nothing" - the two mean opposite things to a thesis leaning on quiet).
    from tools.freshness_audit import stamp_feed

    requested = len(out["tickers"])
    failed = sum(1 for e in out["tickers"].values() if e.get("error"))
    unreadable = sum(1 for e in out["tickers"].values()
                     if (e.get("summary") or {}).get("signal") == "data_unavailable")
    with_activity = sum(
        1 for e in out["tickers"].values()
        if (e.get("summary") or {}).get("signal") not in
        (None, "no_open_market_activity", "data_unavailable"))
    failures = {tk: str(e["error"])[:120]
                for tk, e in out["tickers"].items() if e.get("error")}
    stamp_feed(out, requested, requested - failed, failed,
               found=with_activity,
               failures=dict(list(failures.items())[:10]),
               filings_unreadable_tickers=unreadable or None)
    if out["status"] == "error":
        out["error"] = "insider feed unreachable for every requested ticker"
    # Index reachable everywhere but no Form-4 XML readable anywhere = the
    # archive layer is down even though the submissions layer answered. That is
    # an outage wearing a healthy costume; call it what it is.
    elif requested and unreadable == requested:
        out["status"] = "error"
        out["error"] = ("submissions index answered for every ticker but EVERY "
                        "Form-4 XML fetch failed - insider quiet here is UNREAD, "
                        "not observed")
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_insider_activity(sys.argv[1:] or ["DELL"]), indent=2))
