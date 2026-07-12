"""Full-universe fundamental screen - catches inflections before price moves.

Runs across ALL universe names (estimates only - cheap fields) in the 6am and
9am ET gathers; other runs reuse the cached snapshot. Keeping the previous
snapshot lets the 9am run show WHAT CHANGED since 6am - morning earnings and
guidance land exactly in that window. All data free (Yahoo); refresh is
time-gated so rate limits are never approached.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "fundamental_screen.json"
REFRESH_HOURS_LOCAL = {5, 6, 8, 9}  # pre-market windows (covers Action at 5:40/8:40 ET)
MIN_REFRESH_GAP_H = 1.5


def _fetch_row(ticker: str) -> dict | None:
    import yfinance as yf
    try:
        tk = yf.Ticker(ticker)
        row: dict = {"ticker": ticker}
        rev = tk.revenue_estimate
        if rev is not None and "growth" in rev and "0y" in rev.index:
            row["fwd_revenue_growth_pct"] = round(float(rev.loc["0y", "growth"]) * 100, 1)
        trend = tk.eps_trend
        if trend is not None and "0y" in trend.index:
            cur = float(trend.loc["0y", "current"])
            m30 = float(trend.loc["0y", "30daysAgo"])
            m7 = float(trend.loc["0y", "7daysAgo"])
            if m30:
                row["eps_revision_30d_pct"] = round((cur - m30) / abs(m30) * 100, 2)
            if m7:
                row["eps_revision_7d_pct"] = round((cur - m7) / abs(m7) * 100, 2)
            row["eps_estimate_current_yr"] = round(cur, 2)
        return row if len(row) > 1 else None
    except Exception:
        return None


def refresh_screen(tickers: list[str]) -> dict:
    rows = [r for t in tickers if (r := _fetch_row(t))]
    prev = {}
    if CACHE.exists():
        try:
            prev = json.loads(CACHE.read_text()).get("current", {})
        except Exception:
            prev = {}
    snapshot = {"as_of": datetime.now(timezone.utc).isoformat(),
                "rows": {r["ticker"]: r for r in rows}}
    CACHE.write_text(json.dumps({"previous": prev, "current": snapshot}, indent=1))
    return snapshot


def get_screen(tickers: list[str] | None = None, force: bool = False) -> dict:
    """Cached screen + deltas. Refreshes only in pre-market windows (or force)."""
    data = {}
    if CACHE.exists():
        try:
            data = json.loads(CACHE.read_text())
        except Exception:
            data = {}
    cur = data.get("current", {})
    age_h = 999.0
    if cur.get("as_of"):
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(cur["as_of"])).total_seconds() / 3600
    should_refresh = force or (tickers and age_h > MIN_REFRESH_GAP_H
                               and (datetime.now().hour in REFRESH_HOURS_LOCAL
                                    or age_h > 20))
    if should_refresh and tickers:
        cur = refresh_screen(tickers)
        data = json.loads(CACHE.read_text())

    rows = list(cur.get("rows", {}).values())
    prev_rows = data.get("previous", {}).get("rows", {})
    # Morning movers: estimate changes since the previous snapshot.
    deltas = []
    for r in rows:
        p = prev_rows.get(r["ticker"])
        if not p:
            continue
        d = round((r.get("eps_estimate_current_yr") or 0)
                  - (p.get("eps_estimate_current_yr") or 0), 3)
        if abs(d) > 0.005 and p.get("eps_estimate_current_yr"):
            deltas.append({"ticker": r["ticker"], "eps_estimate_change": d,
                           "pct": round(d / abs(p["eps_estimate_current_yr"]) * 100, 2)})
    deltas.sort(key=lambda x: abs(x["pct"]), reverse=True)

    top_revisions = sorted((r for r in rows if r.get("eps_revision_30d_pct") is not None),
                           key=lambda r: r["eps_revision_30d_pct"], reverse=True)
    return {
        "as_of": cur.get("as_of"), "age_hours": round(age_h, 1), "names_covered": len(rows),
        "note": ("Full-universe estimate screen, refreshed pre-market (6am/9am ET). "
                 "top_upward_revisions = fundamental inflections regardless of price - "
                 "candidates the momentum funnel may not surface. "
                 "estimate_changes_since_previous = what moved THIS MORNING (earnings/guidance)."),
        "top_upward_revisions": top_revisions[:10],
        "top_downward_revisions": top_revisions[-5:][::-1] if len(top_revisions) > 5 else [],
        "estimate_changes_since_previous": deltas[:10],
    }


if __name__ == "__main__":
    import sys
    from tools.universe_scanner import load_universe
    ts = sorted({t for v in load_universe().values() for t in v})
    if "--small" in sys.argv:
        ts = ts[:8]
    print(json.dumps(get_screen(ts, force=True), indent=1)[:1500])
