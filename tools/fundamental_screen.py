"""Full-universe fundamental screen - catches inflections before price moves.

Runs across ALL universe names (estimates only - cheap fields) in the 6am and
9am ET gathers; other runs reuse the cached snapshot. Keeping the previous
snapshot lets the 9am run show WHAT CHANGED since 6am - morning earnings and
guidance land exactly in that window. All data free (Yahoo); refresh is
time-gated so rate limits are never approached.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "fundamental_screen.json"
# Pre-market ET windows: the 6am and 9am gathers (5/8 buffer the cron's ~5:40/
# 8:40 ET fires). These are EASTERN hours - see _eastern_now(); the cloud host
# runs UTC, so comparing datetime.now().hour directly would miss the window.
REFRESH_HOURS_ET = {5, 6, 8, 9}
MIN_REFRESH_GAP_H = 1.5


def _us_eastern_offset_hours(d: date) -> int:
    """UTC offset (hours) for US Eastern on date d: 4 during DST else 5. DST runs
    2nd Sunday of March to 1st Sunday of November; day-granularity is fine here
    because the 2am switch is far from the 6/9am windows we gate on."""
    mar1 = date(d.year, 3, 1)
    second_sun_mar = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = date(d.year, 11, 1)
    first_sun_nov = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return 4 if second_sun_mar <= d < first_sun_nov else 5


def _eastern_now(now_utc: datetime | None = None) -> datetime:
    """Current time in America/New_York. Prefers zoneinfo; falls back to a pure
    US-Eastern DST rule when tz data is absent (minimal cloud images)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return now_utc - timedelta(hours=_us_eastern_offset_hours(now_utc.date()))


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
            # Labeled direction of the latest revision (30d, else 7d) - a cheap
            # read on which way estimates are drifting.
            rev = row.get("eps_revision_30d_pct")
            rev = row.get("eps_revision_7d_pct") if rev is None else rev
            if rev is not None:
                row["revision_direction"] = ("up" if rev > 0.1 else
                                             "down" if rev < -0.1 else "flat")
        # Forward EPS growth (next fiscal year), from the same estimate calls -
        # lets the brain judge whether a multiple is deserved (PEG-ish) without
        # a separate price fetch. Isolated so a miss never nulls the whole row.
        try:
            est = tk.earnings_estimate
            if est is not None and "+1y" in getattr(est, "index", []) and "growth" in est:
                g = est.loc["+1y", "growth"]
                if g is not None and g == g:  # g == g rejects NaN
                    row["eps_growth_next_yr_pct"] = round(float(g) * 100, 1)
        except Exception:
            pass
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
    et_hour = _eastern_now().hour  # explicit America/New_York, not UTC host time
    should_refresh = force or (tickers and age_h > MIN_REFRESH_GAP_H
                               and (et_hour in REFRESH_HOURS_ET or age_h > 20))
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
        "note": ("Full-universe estimate screen, refreshed pre-market (6am/9am ET, "
                 "gated on true America/New_York time). "
                 "top_upward_revisions = fundamental inflections regardless of price - "
                 "candidates the momentum funnel may not surface. "
                 "estimate_changes_since_previous = what moved THIS MORNING (earnings/guidance). "
                 "Each row may carry revision_direction (up/down/flat) and "
                 "eps_growth_next_yr_pct (forward EPS growth) - judge a multiple against "
                 "growth, not news tone."),
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
