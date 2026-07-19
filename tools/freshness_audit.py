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


# --------------------------------------------------------------------------- #
# UNIFORM FEED-STATUS CONTRACT (shared by every research feed under tools/)
# --------------------------------------------------------------------------- #
# Lives HERE, in the data-integrity watchdog, for two reasons: this module is
# already where the repo puts "is the data actually real?" logic, and it is a
# true leaf (only json + pathlib at module scope, every heavier import lazy), so
# every feed can import the contract without dragging in EDGAR, pandas or
# yfinance - and without breaking when a test swaps a stub into sys.modules.
#
# THE INCIDENT THIS PREVENTS
# --------------------------
# orchestrator.py's `feed_is_alive` (orchestrator.py:112) is the system's ONLY
# automated check that the research feeds actually delivered, and for two of the
# three feeds it guarded it was DEAD CODE:
#   * news_catalysts.py:199 hardcoded {"status": "ok"} and never downgraded it,
#     so a run in which every yfinance call raised still reported a healthy feed;
#   * the sec_filings bundle is a bare {TICKER: brief} map with NO top-level
#     status, so .get("status") returned None, None is not in the dead-status
#     tuple, and a TOTAL EDGAR OUTAGE read as healthy.
# Four incompatible status shapes existed across tools/. These helpers make the
# repo's own standard - absence of data must never render as a substantive market
# finding - mechanical instead of per-file folklore.
#
# THE CONTRACT
# ------------
#   status      meaning                                        feed_is_alive
#   ---------   --------------------------------------------   -------------
#   "ok"        every attempted name succeeded                  alive
#   "degraded"  some succeeded, some FAILED (counts disclosed)  alive
#   "empty"     every name succeeded, nothing qualified         alive
#   "error"     every attempted name FAILED - the fetch died    DEAD
#   "skipped"   nothing was asked for (no focus names)          alive
#
#   coverage = {"requested": n, "succeeded": n, "failed": n, ...}
#
# "empty" vs "error" is the load-bearing distinction, and it is the same one
# insider_form4's `data_unavailable` signal exists to make: "no insider bought
# anything" and "we could not read the filings" mean OPPOSITE things to a thesis.
# A tool whose per-name calls ALL failed must report "error" - never "ok", never
# "empty".
FEED_OK = "ok"
FEED_DEGRADED = "degraded"
FEED_EMPTY = "empty"
FEED_ERROR = "error"
FEED_SKIPPED = "skipped"


def feed_status(requested: int, succeeded: int, failed: int,
                *, found: int | None = None) -> str:
    """Resolve the uniform top-level status from coverage counters. Pure.

    `found` (optional) is the number of names that produced a SUBSTANTIVE result
    - headlines, transactions, deals. When every name was reachable but none
    produced anything, the status is "empty" (a real, honest market state) rather
    than "ok". When every name FAILED the status is "error" regardless of
    `found`, because a dead fetch must never wear the same label as a quiet tape.
    """
    requested = int(requested or 0)
    succeeded = int(succeeded or 0)
    failed = int(failed or 0)
    if requested <= 0:
        return FEED_SKIPPED
    if succeeded <= 0:
        return FEED_ERROR
    if failed > 0:
        return FEED_DEGRADED
    if found is not None and int(found) <= 0:
        return FEED_EMPTY
    return FEED_OK


def coverage_block(requested: int, succeeded: int, failed: int, **extra) -> dict:
    """The uniform `coverage` counter block. Pure.

    Every feed exposes this so a caller can tell "nothing qualified" from "the
    fetch died" WITHOUT reverse-engineering per-ticker sub-keys.
    universe_scanner already proved the value of this on its bulk pass
    (`requested` / `scan_failures`); this generalizes it to every feed.
    """
    out = {"requested": int(requested or 0),
           "succeeded": int(succeeded or 0),
           "failed": int(failed or 0)}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def stamp_feed(out: dict, requested: int, succeeded: int, failed: int,
               *, found: int | None = None, failures: dict | None = None,
               **extra) -> dict:
    """Stamp `status` + `coverage` onto a feed result in place; return it.

    `failures` is an optional {TICKER: reason} map (truncate at the call site) so
    a degraded run says WHICH names died and why, not merely how many.
    """
    out["status"] = feed_status(requested, succeeded, failed, found=found)
    out["coverage"] = coverage_block(requested, succeeded, failed, **extra)
    if failures:
        out["coverage"]["failures"] = failures
    return out


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
    # STATUS CONTRACT (feed_status above). audit_universe already refuses to
    # PERSIST an all-error audit (the guard below - it would destroy the
    # last-good artifact and feed the bundle a "whole universe errored"
    # summary). But the RETURNED dict still said "status": "ok"
    # unconditionally, so an in-memory caller that never touches the artifact -
    # the bundle's fundamentals_freshness block - read a blocked EDGAR as a clean
    # audit in which zero names were stale. "No name is stale" and "we could not
    # check any name" are opposite findings.
    out = {
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
    return stamp_feed(out, len(results), len(results) - len(errors), len(errors),
                      failures={t: "audit_ticker returned status=error"
                                for t in errors[:10]},
                      stale=len(stale), mismatched=len(mismatched))


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
    # NEVER overwrite the published artifact with an all-error audit: on a
    # blocked/throttled EDGAR every name errors, and persisting that (with a
    # fresh timestamp) would destroy the last-good audit and feed the bundle a
    # "whole universe errored" summary that defeats the 8-day staleness guard.
    if write_artifact and audit.get("audited", 0) > 0 and audit.get("fresh", 0) == 0 \
            and len(audit.get("error_tickers", [])) == audit.get("audited"):
        audit["artifact_error"] = ("not persisted: every name errored - feed almost "
                                   "certainly down; keeping the last-good artifact")
        print(f"  freshness audit NOT persisted ({audit['artifact_error']})")
        write_artifact = False
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
