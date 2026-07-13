"""Fundamentals freshness audit - are we analyzing the LATEST reported quarter?

Born from the DELL incident: the XBRL extraction silently served Q1-FY2026 revenue
($23.4B) as "latest" while the filed Q1-FY2027 10-Q said $43.8B, because a
`frame is None` filter dropped every recently-reported (framed-only) fact. The
selection bug is fixed in sec_filings.dedupe_facts; THIS tool is the independent
watchdog that catches any recurrence of the whole CLASS of staleness bugs:

For each ticker it compares three independent sources:
  1. submissions API `reportDate` - the authoritative period the newest 10-Q/10-K covers;
  2. our extracted fundamentals - the newest quarterly revenue period_end + value;
  3. (optional) yfinance's quarterly income statement - a second opinion on the value.

A ticker is FRESH only when our extraction reaches the latest filed period. A value
mismatch vs yfinance beyond tolerance is flagged for human eyes (different fiscal
labels/units can explain small gaps; a 2x gap means someone is wrong).

CLI: python -m tools.freshness_audit DELL HPE NVDA          (add --yf to cross-check)
     python -m tools.freshness_audit --held                 (audit current holdings)
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VALUE_TOLERANCE = 0.20  # |ours - yfinance| / yfinance beyond this is flagged


def _yf_latest_quarterly_revenue(ticker: str):
    """(revenue_usd, period_end_iso) from yfinance's quarterly income statement, or
    (None, None). Independent of EDGAR, so it cross-checks our extraction."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).quarterly_income_stmt
        if df is None or df.empty:
            return None, None
        col = sorted(df.columns)[-1]  # newest quarter
        for row_name in ("Total Revenue", "Operating Revenue"):
            if row_name in df.index:
                v = df.loc[row_name, col]
                try:
                    return float(v), str(col.date() if hasattr(col, "date") else col)[:10]
                except (TypeError, ValueError):
                    return None, None
        return None, None
    except Exception:
        return None, None


def audit_ticker(ticker: str, cross_check: bool = False) -> dict:
    """Freshness verdict for one ticker. Fail-soft: errors become status fields."""
    from tools.sec_filings import get_filing_brief
    out: dict = {"ticker": ticker.upper()}
    brief = get_filing_brief(ticker)
    if brief.get("status") != "ok":
        out["status"] = "error"
        out["reason"] = brief.get("reason", "brief failed")
        return out

    latest = brief.get("latest_periodic_filing") or {}
    rev = (brief.get("quarterly_fundamentals") or {}).get("revenue") or []
    current_through = brief.get("fundamentals_current_through")
    out.update({
        "status": "ok",
        "latest_filing": latest,                      # form / filed / period_end
        "fundamentals_current_through": current_through,
        "latest_revenue_usd": rev[-1]["value_usd"] if rev else None,
        "stale": bool(brief.get("stale_fundamentals_warning")),
        "warning": brief.get("stale_fundamentals_warning"),
    })

    if cross_check and rev:
        yf_rev, yf_period = _yf_latest_quarterly_revenue(ticker)
        if yf_rev:
            ours = rev[-1]["value_usd"]
            gap = abs(ours - yf_rev) / yf_rev if yf_rev else None
            out["yfinance_check"] = {
                "revenue_usd": yf_rev, "period_end": yf_period,
                "gap_pct": round(gap * 100, 1) if gap is not None else None,
                "agrees": bool(gap is not None and gap <= VALUE_TOLERANCE),
            }
    return out


def audit_freshness(tickers: list, cross_check: bool = False) -> dict:
    """Audit a list of tickers; summary counts stale/mismatched names up top."""
    results = [audit_ticker(t, cross_check) for t in tickers]
    stale = [r["ticker"] for r in results if r.get("stale")]
    mismatched = [r["ticker"] for r in results
                  if r.get("yfinance_check") and not r["yfinance_check"]["agrees"]]
    return {
        "status": "ok",
        "note": "Fundamentals are FRESH only when the extracted series reaches the "
                "latest filed 10-Q/10-K period (submissions reportDate). stale=true "
                "means the numbers are NOT the latest reported quarter.",
        "stale_tickers": stale,
        "value_mismatch_vs_yfinance": mismatched,
        "results": results,
    }


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:]]
    cross = "--yf" in args
    args = [a for a in args if not a.startswith("--")]
    if "--held" in sys.argv or not args:
        state = ROOT / "state" / "portfolio.json"
        held = []
        if state.exists():
            held = [p["ticker"] for p in json.loads(state.read_text()).get("positions", [])]
        args = args or held or ["DELL"]
    print(json.dumps(audit_freshness(args, cross_check=cross), indent=1))
