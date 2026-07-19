"""Universe-wide 8-K sweep — ONE cheap EDGAR request instead of 185.

Per-ticker filing feeds answer "what did MY focus names file?"; this answers
"did ANYONE in the universe drop an 8-K today?" by fetching the EDGAR daily
form index (a single fixed-width .idx file listing every filing of the day),
filtering to forms starting "8-K", and mapping filer CIKs back to universe
tickers via the locally-cached company_tickers.json (tools.sec_filings
.ticker_to_cik — 185 lookups are local file reads, not network calls).

Weekends/holidays have no index; when today's file 404s we walk back up to
3 business days and report which index_date actually answered. Few or zero
8-K filers on a given day is a legitimate result, not a failure.

CLI: python -m tools.filings_sweep
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

INDEX_URL = ("https://www.sec.gov/Archives/edgar/daily-index/"
             "{year}/QTR{quarter}/form.{yyyymmdd}.idx")
ARCHIVES_BASE = "https://www.sec.gov/Archives/"
MAX_WALKBACK_BUSINESS_DAYS = 3

# A data row's five columns are separated by runs of >=2 spaces and rigidly
# typed at the tail: all-digit CIK, 8-digit date, edgar/... file path. Form
# types may contain SINGLE spaces ("1-A POS"); so may company names — the
# non-greedy groups + typed tail resolve the split correctly via backtracking.
# We deliberately do NOT slice by header offsets: the live header line is
# wrapped mid-"CIK" AND its label positions disagree with the actual data
# columns, so only the row shape itself is trustworthy.
_ROW_RE = re.compile(
    r"^(?P<form>\S(?:.*?\S)?)\s{2,}"
    r"(?P<company>\S(?:.*?\S)?)\s{2,}"
    r"(?P<cik>\d+)\s+"
    r"(?P<date>\d{8})\s+"
    r"(?P<path>edgar/\S+)\s*$")


def parse_form_idx(text: str) -> list[dict]:
    """EDGAR daily form.idx -> [{form, company, cik, date, path}].

    Rows are recognized by shape (see _ROW_RE), so the preamble, the wrapped/
    misaligned header, and the dashed separator are skipped naturally, as is
    any malformed row (missing path, non-numeric CIK, truncated line). Bad or
    empty input -> []. Pure function so tests can feed fixture strings."""
    if not text or not isinstance(text, str):
        return []
    rows = []
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if m:
            rows.append({"form": m["form"], "company": m["company"],
                         "cik": m["cik"], "date": m["date"], "path": m["path"]})
    return rows


def select_universe_8ks(rows: list[dict], cik_to_ticker: dict) -> list[dict]:
    """Filter parsed .idx rows to forms starting '8-K' (8-K, 8-K/A) filed by
    universe CIKs -> [{ticker, form, url}]. CIKs are compared numerically so
    zero-padding differences never matter. Pure."""
    filers = []
    for row in rows:
        if not str(row.get("form", "")).startswith("8-K"):
            continue
        try:
            cik_key = str(int(row["cik"]))
        except (KeyError, TypeError, ValueError):
            continue
        ticker = cik_to_ticker.get(cik_key)
        if ticker:
            filers.append({"ticker": ticker, "form": row["form"],
                           "url": ARCHIVES_BASE + str(row.get("path", "")).lstrip("/")})
    return filers


def _recent_business_days(start: date, count: int) -> list[date]:
    """Most recent `count` business days (Mon-Fri) at or before `start`,
    newest first. Holidays are handled by the 404 walk-back, not here. Pure."""
    out, d = [], start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _index_url(d: date) -> str:
    return INDEX_URL.format(year=d.year, quarter=(d.month - 1) // 3 + 1,
                            yyyymmdd=d.strftime("%Y%m%d"))


def _today_eastern() -> date:
    """EDGAR's daily index is stamped in US Eastern; fall back to UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def today_8ks(universe_tickers) -> dict:
    """Sweep today's EDGAR daily form index for 8-Ks filed by universe names.

    One network request for the whole universe (plus up to 3 business-day
    walk-backs when today's index doesn't exist yet). Fail-soft: any total
    failure returns status:error with empty filers, never raises."""
    from tools.net import get_sec
    from tools.sec_filings import ticker_to_cik_result

    now = datetime.now(timezone.utc)
    out = {"status": "ok", "as_of": now.isoformat(), "index_date": None, "filers": []}

    # cik -> ticker map from the locally-cached company_tickers.json.
    # Unmapped names used to vanish with no requested-vs-mapped count, so a
    # partly-unreachable ticker map narrowed the sweep silently: the 8-Ks of the
    # dropped names simply never appeared, and nothing said they were unscanned.
    cik_to_ticker = {}
    requested = sorted(set(str(t).upper() for t in (universe_tickers or [])))
    lookup_failed, not_listed = [], []
    for t in requested:
        try:
            cik, why = ticker_to_cik_result(t)
        except Exception:
            cik, why = None, "lookup_failed"
        if cik:
            cik_to_ticker[str(int(cik))] = t
        elif why == "lookup_failed":
            lookup_failed.append(t)
        else:
            not_listed.append(t)
    out["coverage"] = {
        "requested": len(requested), "mapped": len(cik_to_ticker),
        "lookup_failed": len(lookup_failed), "not_listed": len(not_listed),
        "unit": "ticker_to_cik",
    }
    if lookup_failed:
        # Not an outright failure — the sweep still covers what it mapped — but
        # these names are UNSCANNED, not clean, and the bundle must say so.
        out["coverage"]["lookup_failed_tickers"] = lookup_failed[:20]
        out["note_unscanned"] = (
            f"{len(lookup_failed)} universe names could not be mapped to a CIK "
            f"(SEC ticker map unreachable); their 8-Ks are UNSCANNED this run, "
            f"not absent.")
    if not cik_to_ticker:
        out["status"] = "error"
        out["error"] = ("could not map any universe tickers to CIKs "
                        f"({len(lookup_failed)} lookup failures, "
                        f"{len(not_listed)} not listed)")
        return out

    text, last_err = None, None
    for d in _recent_business_days(_today_eastern(), 1 + MAX_WALKBACK_BUSINESS_DAYS):
        try:
            text = get_sec(_index_url(d)).text
            out["index_date"] = d.isoformat()
            break
        except Exception as e:  # 404 (index not published) or blocked host
            last_err = e
    if text is None:
        out["status"] = "error"
        out["error"] = f"no daily form index reachable: {str(last_err)[:200]}"
        return out

    out["filers"] = select_universe_8ks(parse_form_idx(text), cik_to_ticker)
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from validator import load_universe

    print(json.dumps(today_8ks(sorted(load_universe())), indent=2, default=str))
