"""Dashboard publish, X drafts, redeploy."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import journal
import validator
from tools.performance_breakdown import build_performance_breakdown
from runlib.core import ROOT, et_date, et_now, to_et_date, json_safe
from runlib.analytics import (
    compute_closed_trades, compute_performance_stats, compute_calibration,
    build_trade_events, build_position_charts,
    update_watchlist_outcomes, append_runs_index, recent_improvements,
    build_health, proposal_ev, sector_exposure, sector_map, benchmark_close,
)

def refresh_dashboard(context: dict, response: str, results: list, fills: list,
                      run_id: str, no_trade_reason: str | None = None,
                      commentary: str | None = None,
                      watchlist: list | None = None) -> None:
    dash = ROOT / "dashboard" / "data"
    dash.mkdir(parents=True, exist_ok=True)

    # Append today's equity + benchmark to the track-record series first (last point wins).
    hist_file = dash / "equity_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    today = et_date()  # market-timezone date, so evening runs don't stamp "tomorrow"
    # A benchmark value, once recorded for a date, must survive every later
    # rewrite of that day's entry - sandboxed runs can't refetch it.
    prev_today = next((h for h in hist if h.get("date") == today), {})
    bench = (context.get("benchmark_close") or benchmark_close()
             or prev_today.get("benchmark_close"))
    hist = [h for h in hist if h["date"] != today]
    hist.append({"date": today,
                 "equity": context["portfolio"].get("total_equity_usd"),
                 "cash": context["portfolio"].get("cash_usd"),
                 "benchmark_close": bench})
    hist_file.write_text(json.dumps(hist, indent=2))

    closed = compute_closed_trades()
    out = {
        "no_trade_reason": no_trade_reason,
        "commentary": commentary,
        "watchlist": watchlist or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": context["trading_mode"],
        "schedule_note": "Runs at 6am, 9am, 10am, 12pm, 2pm and 4pm ET, a 5:30pm "
                         "research review, plus overnight and weekend news checks",
        "portfolio": {
            "cash_usd": context["portfolio"].get("cash_usd"),
            "total_equity_usd": context["portfolio"].get("total_equity_usd"),
            # Attach sector to each position for the dashboard exposure view.
            "positions": [{**p, "sector": sector_map().get(p.get("ticker", "").upper())}
                          for p in context["portfolio"].get("positions", [])],
        },
        "as_of_et": et_date(),
        "macro_snapshot": {
            name: context["macro_regime"].get("indicators", {}).get(name)
            for name in ("cpi_yoy_pct", "ten_year_yield", "vix",
                         "yield_curve_10y2y", "hy_credit_spread")
        } if context["macro_regime"].get("status") == "ok" else None,
        "macro_regime_hint": context["macro_regime"].get("regime_hint"),
        "latest_reasoning": response,
        "proposals": [{"proposal": r.proposal, "approved": r.approved,
                       "reasons": r.reasons,
                       "scenario_ev": proposal_ev(r.proposal)} for r in results],
        "fills": fills,
        "closed_trades": closed[::-1],
        "performance": compute_performance_stats(closed, hist),
        "performance_breakdown": build_performance_breakdown(closed),
        "calibration": compute_calibration(closed),
        "position_risk": (context.get("position_stop_cushion") or {}).get("positions", {}),
        "sector_exposure": sector_exposure(context["portfolio"]),
        "sector_concentration_cap_pct": (
            validator.load_config().get("position_sizing", {})
            .get("max_sector_concentration_pct")),
        # Oldest per-position corporate-actions marker: an aging date here means
        # dividend/split processing is lagging (usually a cloud sandbox blocking
        # yfinance) - visibility only, the cutoff logic prevents double-credits.
        "corporate_actions_processed_through": min(
            (p.get("actions_processed_through")
             for p in context["portfolio"].get("positions", [])
             if p.get("actions_processed_through")), default=None),
        "data_quality": context.get("data_quality"),
        "stale_data_notice": context.get("stale_data_notice"),
        "risk_halts": context.get("risk_halts") or [],
        "universe_size": len(validator.load_universe()),
        "health": build_health(),
        "total_dividends_usd": round(sum(
            (pos.get("dividends_received_usd") or 0)
            for pos in context["portfolio"].get("positions", [])), 2),
        "forced_exits": context.get("forced_exits", []),
        "improvements": recent_improvements(),
        "trade_events": build_trade_events(),
    }
    dash = ROOT / "dashboard" / "data"
    dash.mkdir(parents=True, exist_ok=True)

    # Auxiliary data files (kept out of latest.json so it stays slim):
    positions = context["portfolio"].get("positions", [])
    prices = context.get("universe_scan", {}).get("prices", {})
    try:
        pos_charts = build_position_charts(positions)
        # On a blocked live feed build_position_charts returns {} for every held
        # name. Overwriting the file with {} wipes the per-position tape from the
        # dashboard (frozen-looking charts). Only rewrite when we either got fresh
        # bars OR there genuinely are no open positions; otherwise keep last-good.
        pc_file = dash / "position_charts.json"
        if pos_charts or not positions:
            pc_file.write_text(json.dumps(json_safe(pos_charts), indent=2))
        else:
            print("  (position_charts: live feed empty - keeping last-good file)")
    except Exception as e:
        print(f"  (position_charts write failed: {e})")
    try:
        out["watchlist_outcomes"] = update_watchlist_outcomes(watchlist or [], prices, positions)
    except Exception as e:
        print(f"  (watchlist outcomes failed: {e})")
    append_runs_index(run_id, context.get("trading_mode", "paper"), fills, commentary, no_trade_reason)

    # Full response lives in the per-run archive; latest.json stays slim for the site.
    # The archive's closed_trades holds ONLY closes executed THIS run - the run
    # page titles them "Closed on this run", and embedding the all-time list
    # made every historical close reappear on every later run's page.
    sell_tickers = {str(f.get("ticker", "")).upper() for f in (fills or [])
                    if str(f.get("action", "")).upper() == "SELL_TO_CLOSE"}
    run_out = {**out, "closed_trades": [
        t for t in out["closed_trades"]
        if t.get("closed_at") == today and t.get("ticker") in sell_tickers]}
    (dash / f"run_{run_id}.json").write_text(json.dumps(json_safe(run_out), indent=2, default=str))
    slim = {k: v for k, v in out.items() if k != "latest_reasoning"}
    (dash / "latest.json").write_text(json.dumps(json_safe(slim), indent=2, default=str))
    # NOTE: equity_history (with benchmark_close) was already written at the top of this
    # function. A second write here used to re-append today's point WITHOUT benchmark_close,
    # silently wiping the S&P comparison line every run - removed. Do not reintroduce it.


def redeploy_dashboard() -> None:
    """Publish fresh data by committing and pushing to GitHub; Vercel auto-deploys
    from main. Fail-soft: a failed push logs loudly so numbers never go silently stale.

    Persists the AUTHORITATIVE ledger (state/portfolio.json) and the kill switch so a
    cloud trade is durable and a kill switch reaches every node - previously these were
    never committed, so cloud fills were ephemeral. Retries on push races (the hourly
    relay + gather Action push concurrently) instead of wedging the node permanently."""
    import glob as _glob
    try:
        paths = ["dashboard/data", "journal", "state/portfolio.json"]
        paths += _glob.glob(str(ROOT / "state" / "x_draft_*.txt"))
        if (ROOT / "state" / "KILL_SWITCH").exists():
            paths.append("state/KILL_SWITCH")
        if (ROOT / "data" / "cusip_map.json").exists():
            paths.append("data/cusip_map.json")  # learned ticker->CUSIP, must persist in cloud
        if (ROOT / "data" / "ai_exposure.json").exists():
            paths.append("data/ai_exposure.json")  # business-reality labels, review-maintained
        # add -A so a REMOVED kill switch (all-clear) also propagates; ignore missing paths.
        for p in paths:
            subprocess.run(["git", "add", "-A", p], cwd=ROOT, capture_output=True, text=True)
        r = subprocess.run(["git", "commit", "-m", "Update dashboard data after trading run"],
                           cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            print("  (no data changes to publish)")
            return
        # Push with rebase-retry so a concurrent relay/Action push can't strand us.
        for attempt in range(3):
            r = subprocess.run(["git", "push", "origin", "main"],
                               cwd=ROOT, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                print("  data pushed - Vercel deploying")
                return
            print(f"  push rejected (attempt {attempt + 1}/3), rebasing...")
            rb = subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
            if rb.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], cwd=ROOT,
                               capture_output=True, text=True)
                print(f"  REBASE FAILED (site going stale): {rb.stderr[-300:]}")
                return
        print(f"  PUSH FAILED after retries (site going stale): {r.stderr[-300:]}")
    except Exception as e:
        print(f"  PUBLISH FAILED (site going stale): {e}")


def draft_x_summary(fills: list, results: list, context: dict, run_id: str,
                    x_post: str | None = None) -> None:
    # Trade drafts get a _trade filename suffix so the poster prioritizes them.
    suffix = "_trade" if fills else ""
    path = ROOT / "state" / f"x_draft_{run_id}{suffix}.txt"
    if x_post and x_post.strip():
        # The brain wrote its own post (fund-manager memo style). Publish verbatim.
        path.write_text(x_post.strip())
        return
    # Fallback terse draft. Plain tickers, no $cashtags (X rejects multi-cashtag posts).
    lines = [f"East Equity Agent swing update ({datetime.now():%b %d})"]
    if fills:
        for f in fills:
            lines.append(f"{'Opened' if f['action'] == 'BUY' else 'Closed'} "
                         f"{f['ticker']} @ {f['fill_price']}")
    else:
        lines.append("No new trades today — no setup met the bar.")
    eq = context["portfolio"].get("total_equity_usd")
    if eq:
        lines.append(f"Portfolio equity: ${eq:,.0f}")
    lines.append("Full reasoning on the dashboard. Long-only swing trades. Not financial advice.")
    path.write_text("\n".join(lines))
