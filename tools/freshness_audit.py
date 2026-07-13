"""Fundamentals freshness audit - are we analyzing the LATEST reported numbers?

Born from the DELL incident: the XBRL extraction silently served Q1-FY2026 revenue
($23.4B) as "latest" while the filed Q1-FY2027 10-Q said $43.8B, because a
`frame is None` filter dropped every recently-reported (framed-only) fact. The
selection bug is fixed in sec_filings.dedupe_facts; THIS tool is the independent
watchdog that catches any recurrence of the whole CLASS of staleness bugs - and it
covers the ENTIRE trading universe, not just current holdings/focus names, because
any universe name can become a position at the next run.

Per ticker it reconciles three independent sources:
  1. submissions API `reportDate` - the authoritative period the newest periodic
     filing covers (10-Q/10-K, or 20-F/40-F for foreign private issuers);
  2. our extracted fundamentals - the newest quarterly revenue period_end + value;
  3. (optional) yfinance's quarterly income statement - a second opinion on value.

Filer classes:
  domestic_quarterly - 10-Q/10-K cadence; STALE when extraction lags the filing.
  foreign_annual     - 20-F/40-F cadence (TSM, ASML, ARM...); quarterly XBRL facts
                       are often absent from companyfacts - that is their normal
                       cadence, reported as annual_only, never falsely "stale".

CLI: python -m tools.freshness_audit DELL HPE NVDA      (specific names)
     python -m tools.freshness_audit --held             (current holdings)
     python -m tools.freshness_audit --universe --yf    (ALL universe names + cross-check)
Writes dashboard/data/freshness_audit.json on --universe (published with the site data).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "dashboard" / "data" / "freshness_audit.json"

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


def audit_ticker(ticker: str, cross_check: bool = False, evict: bool = False) -> dict:
    """Freshness verdict for one ticker. Fail-soft: errors become status fields.
    evict=True drops the multi-MB companyfacts payload from the in-memory cache
    afterwards - required for full-universe sweeps (~111 names) to bound RAM."""
    from tools.sec_filings import get_filing_brief, evict_facts
    out: dict = {"ticker": ticker.upper()}
    try:
        brief = get_filing_brief(ticker)
        if brief.get("status") != "ok":
            out["status"] = "error"
            out["reason"] = str(brief.get("reason", "brief failed"))[:120]
            return out

        latest = brief.get("latest_periodic_filing") or {}
        form = latest.get("form")
        filer = ("domestic_quarterly" if form in ("10-K", "10-Q")
                 else "foreign_annual" if form in ("20-F", "40-F")
                 else "unknown")
        rev = (brief.get("quarterly_fundamentals") or {}).get("revenue") or []
        current_through = brief.get("fundamentals_current_through")
        stale = bool(brief.get("stale_fundamentals_warning"))
        out.update({
            "status": "ok",
            "filer_type": filer,
            "latest_filing": latest,                  # form / filed / period_end
            "fundamentals_current_through": current_through,
            "latest_revenue_usd": rev[-1]["value_usd"] if rev else None,
            "stale": stale,
        })
        if stale:
            out["warning"] = brief.get("stale_fundamentals_warning")
        if filer == "foreign_annual" and not rev:
            out["note"] = "annual-cadence filer (20-F/40-F); quarterly XBRL absent by design"

        # Earnings clock: when the NEXT report lands (that is when the quarterly
        # cache invalidates and fundamentals deserve a fresh read).
        if cross_check:
            try:
                import yfinance as yf
                cal = yf.Ticker(ticker).calendar
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if dates:
                    out["next_earnings"] = str(dates[0])
            except Exception:
                pass
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
    finally:
        if evict:
            try:
                evict_facts(ticker)
            except Exception:
                pass


def audit_freshness(tickers: list, cross_check: bool = False, evict: bool = False,
                    progress: bool = False) -> dict:
    """Audit a list of tickers; summary counts stale/mismatched names up top."""
    results = []
    for i, t in enumerate(tickers, 1):
        r = audit_ticker(t, cross_check=cross_check, evict=evict)
        results.append(r)
        if progress:
            tag = ("STALE" if r.get("stale") else r.get("status")
                   if r.get("status") != "ok" else r.get("filer_type", "ok"))
            print(f"  [{i}/{len(tickers)}] {r['ticker']:6} {tag}", flush=True)
    stale = [r["ticker"] for r in results if r.get("stale")]
    errors = [r["ticker"] for r in results if r.get("status") == "error"]
    foreign = [r["ticker"] for r in results if r.get("filer_type") == "foreign_annual"]
    # Foreign annual filers are excluded from the headline mismatch list: their SEC
    # XBRL quarterlies lag by design (6-K press releases aren't XBRL-tagged), so the
    # yfinance comparison spans DIFFERENT periods - expected, not a value error. The
    # per-name yfinance_check stays in results for anyone who wants the detail.
    mismatched = [r["ticker"] for r in results
                  if r.get("yfinance_check") and not r["yfinance_check"]["agrees"]
                  and r.get("filer_type") != "foreign_annual"]
    fresh = [r["ticker"] for r in results
             if r.get("status") == "ok" and not r.get("stale")]
    return {
        "status": "ok",
        "note": "Fundamentals are FRESH only when the extracted series reaches the "
                "latest filed periodic report (submissions reportDate). stale=true "
                "means the numbers are NOT the latest reported quarter. foreign_annual "
                "filers (20-F/40-F) report on an annual cadence - absent quarterly XBRL "
                "is their normal shape, not staleness.",
        "audited": len(results),
        "fresh": len(fresh),
        "stale_tickers": stale,
        "error_tickers": errors,
        "foreign_annual_filers": foreign,
        "value_mismatch_vs_yfinance": mismatched,
        "results": results,
    }


def audit_universe(cross_check: bool = False, progress: bool = True,
                   write_artifact: bool = True) -> dict:
    """Audit EVERY name in data/universe.json and publish the result artifact.
    This is the scheduled weekly sweep (universe review) and the manual
    `--freshness-audit` path - proof that the whole stack is current, not just
    the names under analysis this week."""
    universe = json.loads((ROOT / "data" / "universe.json").read_text())
    tickers = sorted({t.upper() for ts in universe["sectors"].values() for t in ts})
    audit = audit_freshness(tickers, cross_check=cross_check, evict=True,
                            progress=progress)
    if write_artifact:
        try:
            from datetime import datetime, timezone
            try:
                from zoneinfo import ZoneInfo
                now_et = datetime.now(ZoneInfo("America/New_York"))
            except Exception:
                from datetime import timedelta
                now_et = datetime.now(timezone.utc) - timedelta(hours=4)
            audit["audited_at_et"] = now_et.isoformat(timespec="minutes")
            ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
            ARTIFACT.write_text(json.dumps(audit, indent=1))
        except Exception as e:
            audit["artifact_error"] = str(e)[:120]
    return audit


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    cross = "--yf" in argv
    names = [a for a in argv if not a.startswith("--")]
    if "--universe" in argv:
        a = audit_universe(cross_check=cross)
        slim = {k: v for k, v in a.items() if k != "results"}
        print(json.dumps(slim, indent=1))
    else:
        if "--held" in argv or not names:
            state = ROOT / "state" / "portfolio.json"
            held = []
            if state.exists():
                held = [p["ticker"] for p in json.loads(state.read_text()).get("positions", [])]
            names = names or held or ["DELL"]
        print(json.dumps(audit_freshness(names, cross_check=cross, evict=len(names) > 20,
                                         progress=len(names) > 10), indent=1))
