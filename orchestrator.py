"""East Equity Agent — Orchestrator (the supervisor loop).

This is the only entry point for a trading run. It is deterministic scaffolding
around a single Claude invocation:

    1.  Safety preflight (kill switch, mode, market day)
    2.  Gather context with Python tools (macro, portfolio, scan, filings, 13F, news)
    3.  Wake Claude ONCE with the full context bundle + CLAUDE.md rules
    4.  Parse structured JSON proposals from Claude's response
    5.  Validate every proposal with the pure-Python validator (validator.py)
    6.  Execute approved proposals (simulation by default) + broker readback
    7.  Journal everything, refresh dashboard data, draft X summary, log improvements

Claude never touches the broker. The validator never calls Claude. Swing bias
and long-only rules live in autonomy_config.json and are enforced in step 5
regardless of what the brain says.

Run:  python orchestrator.py            (full run, honors trading_mode in config)
      python orchestrator.py --research-only   (steps 1-4 only; no validate/execute)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import journal
import validator
from execution import simulated_broker
from tools.macro_regime import get_macro_snapshot
from tools.news_catalysts import get_news_and_catalysts
from tools.portfolio_state import get_portfolio_state
from tools.insider_form4 import get_insider_activity
from tools.sec_filings import get_filing_brief
from tools.smart_money_13f import get_smart_money
from tools.universe_scanner import scan_universe

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


# ---------------------------------------------------------------------------
# Step 1 — Safety preflight
# ---------------------------------------------------------------------------
def preflight(cfg: dict, run_id: str) -> str | None:
    """Return a halt reason string, or None if clear to proceed."""
    if validator.kill_switch_active(cfg):
        return "KILL_SWITCH file present — no runs until it is removed"
    if datetime.now().strftime("%a") not in cfg["schedule"]["run_days"]:
        return "not a configured run day (weekend/holiday guard)"
    return None


# ---------------------------------------------------------------------------
# Steps 2 — Context gathering (all deterministic Python, all fail-soft)
# ---------------------------------------------------------------------------
def gather_context(cfg: dict) -> dict:
    print("  • macro regime...")
    macro = get_macro_snapshot()
    print("  • portfolio state...")
    portfolio = get_portfolio_state()
    print("  • universe scan...")
    scan = scan_universe(top_n=15)

    # Deep research on: current holdings + top-5 scanner candidates.
    held = [p["ticker"] for p in portfolio.get("positions", [])]
    candidates = [r["ticker"] for r in scan.get("top_setups", [])[:5]]
    focus = list(dict.fromkeys(held + candidates))

    print(f"  • deep research on {focus}...")
    filings = {t: get_filing_brief(t) for t in focus}
    smart_money = get_smart_money(focus) if focus else {"status": "skipped"}
    news = get_news_and_catalysts(focus) if focus else {"status": "skipped"}
    insiders = get_insider_activity(focus) if focus else {"status": "skipped"}

    # The agent's own track record: what it predicted vs. what actually happened.
    closed = compute_closed_trades()
    hist_file = ROOT / "dashboard" / "data" / "equity_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    track_record = {
        "note": "Your own past trades. Study what worked and what did not before proposing.",
        "closed_trades": closed[-20:],
        "performance": compute_performance_stats(closed, hist),
    }

    return {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "trading_mode": cfg["mode"]["trading_mode"],
        # Hard limits the validator will enforce - size within them or be rejected.
        "hard_limits": {
            "position_sizing": cfg["position_sizing"],
            "quality": cfg["trade_quality_requirements"],
            "swing": {k: cfg["swing_rules"][k] for k in
                      ("min_holding_horizon_days", "max_holding_horizon_days",
                       "max_new_positions_per_day")},
        },
        "macro_regime": macro,
        "portfolio": portfolio,
        "universe_scan": scan,
        "sec_filings": filings,
        "smart_money_13f": smart_money,
        "news_and_catalysts": news,
        "insider_activity": insiders,
        "track_record": track_record,
    }


# ---------------------------------------------------------------------------
# Step 3 — Wake the brain (single Claude invocation via Claude Code CLI)
# ---------------------------------------------------------------------------
def ask_claude(context: dict, run_id: str) -> str:
    """Invoke Claude Code headlessly with CLAUDE.md rules + the context bundle."""
    context_file = ROOT / "state" / f"context_{run_id}.json"
    context_file.write_text(json.dumps(context, indent=2, default=str))

    prompt = (
        "You are running a scheduled East Equity Agent trading cycle. "
        f"Read your system rules in CLAUDE.md, then read the full market/portfolio "
        f"context bundle at {context_file}. Follow the Required Process exactly: "
        "macro check, portfolio review (HOLD or SELL_TO_CLOSE each position against its "
        "original plan), candidate analysis, then output your trade proposals in the "
        "exact JSON schema from CLAUDE.md inside a ```json fenced block. "
        "Long-only equities, swing horizon 3-90 days, high-conviction only. "
        "Proposing nothing is acceptable — include no_trade_reason if so. "
        "End with an 'Improvement note:' line."
    )
    import time
    last_err = ""
    for attempt in (1, 2):  # one retry: overnight runs can hit transient/usage errors
        result = subprocess.run(
            ["claude", "-p", prompt, "--permission-mode", "plan"],
            cwd=ROOT, capture_output=True, text=True, timeout=1800,
        )
        if result.returncode == 0:
            return result.stdout
        last_err = (f"exit={result.returncode} stderr={result.stderr[:1000]} "
                    f"stdout={result.stdout[:1000]}")
        print(f"  claude attempt {attempt} failed: {last_err[:300]}")
        if attempt == 1:
            time.sleep(90)
    raise RuntimeError(f"claude CLI failed after retry: {last_err}")


# ---------------------------------------------------------------------------
# Step 4 — Parse structured proposals out of the brain's response
# ---------------------------------------------------------------------------
def parse_proposals(response: str) -> dict:
    """Extract the structured output block: proposals, commentary, watchlist."""
    blocks = re.findall(r"```json\s*(.*?)```", response, re.DOTALL)
    for block in reversed(blocks):  # last JSON block wins
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "proposals" in data:
                return {
                    "proposals": data["proposals"],
                    "no_trade_reason": data.get("no_trade_reason"),
                    "commentary": data.get("commentary"),
                    "watchlist": (data.get("watchlist") or [])[:10],
                }
        except json.JSONDecodeError:
            continue
    return {"proposals": [], "no_trade_reason": "no parsable proposals block in response",
            "commentary": None, "watchlist": []}


# ---------------------------------------------------------------------------
# Step 6 — Execution (approved proposals only; broker readback required)
# ---------------------------------------------------------------------------
def execute(approved: list[validator.ValidationResult], context: dict,
            cfg: dict, run_id: str) -> list[dict]:
    fills = []
    prices = context["universe_scan"].get("prices", {})
    max_orders = cfg["risk_controls"]["max_orders_per_run"]

    if cfg["mode"]["trading_mode"] == "dry_run":
        print("  DRY RUN — approved proposals journaled, no orders placed.")
        return fills

    # Per-DAY new-position cap across multiple intraday runs (validator only sees one batch).
    today = datetime.now(timezone.utc).date().isoformat()
    trades_file = ROOT / "journal" / "trades" / f"{today}.jsonl"
    buys_today = 0
    if trades_file.exists():
        for line in trades_file.read_text().splitlines():
            if json.loads(line).get("fill", {}).get("action") == "BUY":
                buys_today += 1
    max_buys_per_day = cfg["swing_rules"]["max_new_positions_per_day"]

    for vr in approved[:max_orders]:
        p = vr.proposal
        if p["action"] == "BUY" and buys_today >= max_buys_per_day:
            journal.log_rejection(p, [f"max_new_positions_per_day_reached:{buys_today}"], run_id)
            continue
        ref = prices.get(p["ticker"].upper())
        if ref is None:
            journal.log_rejection(p, ["no_reference_price_available"], run_id)
            continue
        if p["action"] == "BUY" and ref > float(p["entry_price_max"]):
            journal.log_rejection(p, [f"price_above_entry_max:{ref}>{p['entry_price_max']}"], run_id)
            continue
        order = simulated_broker.place_order({
            "ticker": p["ticker"], "action": p["action"],
            "position_size_usd": p.get("position_size_usd"),
            "reference_price": ref, "proposal_id": run_id,
        })
        fill = simulated_broker.readback(order["order_id"])  # mandatory readback
        if fill is None or fill.get("status") != "filled":
            journal.log_rejection(p, [f"fill_failed:{(fill or {}).get('status')}"], run_id)
            continue
        journal.log_trade(order, fill, run_id)
        fills.append(fill)
        if fill["action"] == "BUY":
            buys_today += 1
        print(f"  FILLED {fill['action']} {fill['ticker']} "
              f"{fill['quantity']} @ {fill['fill_price']}")
    return fills


# ---------------------------------------------------------------------------
# Step 7 — Dashboard data + X summary
# ---------------------------------------------------------------------------
def _benchmark_close(ticker: str = "SPY") -> float | None:
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="5d")
        return round(float(h["Close"].iloc[-1]), 2)
    except Exception:
        return None


def _trade_plans() -> dict:
    """Latest BUY proposal per ticker from the journal (thesis, stop, target, horizon)."""
    plans: dict = {}
    for f in sorted((ROOT / "journal" / "proposals").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            p = rec.get("proposal", {})
            if str(p.get("action", "")).upper() == "BUY" and p.get("ticker"):
                plans[p["ticker"].upper()] = p
    return plans


def compute_closed_trades() -> list[dict]:
    """Closed trades with a thesis verdict, from the broker history + trade plans."""
    state_file = ROOT / "state" / "portfolio.json"
    if not state_file.exists():
        return []
    history = json.loads(state_file.read_text()).get("history", [])
    plans = _trade_plans()
    opens: dict[str, dict] = {}
    closed = []
    for fill in history:
        if fill.get("status") != "filled":
            continue
        t = fill["ticker"].upper()
        if fill["action"] == "BUY":
            opens[t] = fill
        elif fill["action"] == "SELL_TO_CLOSE" and t in opens:
            entry_fill = opens.pop(t)
            plan = plans.get(t, {})
            entry = float(entry_fill["fill_price"])
            exit_px = float(fill["fill_price"])
            stop = float(plan.get("stop_loss") or 0)
            target = float(plan.get("target_price") or 0)
            days_held = max((datetime.fromisoformat(fill["filled_at"])
                             - datetime.fromisoformat(entry_fill["filled_at"])).days, 0)
            horizon = plan.get("holding_horizon_days")
            if target and exit_px >= target * 0.995:
                verdict = "Hit target"
            elif stop and exit_px <= stop * 1.005:
                verdict = "Stopped out"
            elif horizon and days_held >= float(horizon):
                verdict = "Time-limit exit"
            else:
                verdict = "Thesis exit"
            r_multiple = round((exit_px - entry) / (entry - stop), 2) if stop and entry > stop else None
            closed.append({
                "ticker": t, "entry_price": entry, "exit_price": exit_px,
                "opened_at": entry_fill["filled_at"][:10], "closed_at": fill["filled_at"][:10],
                "days_held": days_held, "pnl_usd": fill.get("realized_pnl_usd"),
                "r_multiple": r_multiple, "verdict": verdict,
                "thesis": plan.get("thesis"),
            })
    return closed


def compute_performance_stats(closed: list[dict], equity_hist: list[dict]) -> dict | None:
    if not closed:
        return None
    pnls = [t["pnl_usd"] or 0 for t in closed]
    wins = [p for p in pnls if p > 0]
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    peak, max_dd = 0.0, 0.0
    for h in equity_hist:
        peak = max(peak, h["equity"])
        if peak:
            max_dd = max(max_dd, (peak - h["equity"]) / peak)
    return {
        "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
        "realized_pnl_usd": round(sum(pnls), 2),
        "avg_r_multiple": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_days_held": round(sum(t["days_held"] for t in closed) / len(closed), 1),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }


def recent_improvements(limit: int = 10) -> list[dict]:
    notes = []
    for f in sorted((ROOT / "journal" / "improvements").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            notes.append({"date": rec["ts"][:10], "note": rec["note"]})
    return notes[-limit:][::-1]


def refresh_dashboard(context: dict, response: str, results: list, fills: list,
                      run_id: str, no_trade_reason: str | None = None,
                      commentary: str | None = None,
                      watchlist: list | None = None) -> None:
    dash = ROOT / "dashboard" / "data"
    dash.mkdir(parents=True, exist_ok=True)

    # Append today's equity + benchmark to the track-record series first (last point wins).
    hist_file = dash / "equity_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    today = datetime.now(timezone.utc).date().isoformat()
    hist = [h for h in hist if h["date"] != today]
    hist.append({"date": today,
                 "equity": context["portfolio"].get("total_equity_usd"),
                 "cash": context["portfolio"].get("cash_usd"),
                 "benchmark_close": _benchmark_close()})
    hist_file.write_text(json.dumps(hist, indent=2))

    closed = compute_closed_trades()
    out = {
        "no_trade_reason": no_trade_reason,
        "commentary": commentary,
        "watchlist": watchlist or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": context["trading_mode"],
        "schedule_note": "Runs every 3 hours on weekdays",
        "portfolio": {k: context["portfolio"].get(k) for k in
                      ("cash_usd", "total_equity_usd", "positions")},
        "macro_snapshot": {
            name: context["macro_regime"].get("indicators", {}).get(name)
            for name in ("cpi_yoy_pct", "ten_year_yield", "vix")
        } if context["macro_regime"].get("status") == "ok" else None,
        "macro_regime_hint": context["macro_regime"].get("regime_hint"),
        "latest_reasoning": response,
        "proposals": [{"proposal": r.proposal, "approved": r.approved,
                       "reasons": r.reasons} for r in results],
        "fills": fills,
        "closed_trades": closed[::-1],
        "performance": compute_performance_stats(closed, hist),
        "improvements": recent_improvements(),
    }
    dash = ROOT / "dashboard" / "data"
    dash.mkdir(parents=True, exist_ok=True)
    # Full response lives in the per-run archive; latest.json stays slim for the site.
    (dash / f"run_{run_id}.json").write_text(json.dumps(out, indent=2, default=str))
    slim = {k: v for k, v in out.items() if k != "latest_reasoning"}
    (dash / "latest.json").write_text(json.dumps(slim, indent=2, default=str))

    # Append today's equity to the track-record series (one point per day, last wins).
    hist_file = dash / "equity_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    today = datetime.now(timezone.utc).date().isoformat()
    hist = [h for h in hist if h["date"] != today]
    hist.append({"date": today,
                 "equity": context["portfolio"].get("total_equity_usd"),
                 "cash": context["portfolio"].get("cash_usd")})
    hist_file.write_text(json.dumps(hist, indent=2))


def redeploy_dashboard() -> None:
    """Publish fresh data by committing and pushing to GitHub; Vercel auto-deploys
    from main. Fail-soft: a failed push logs loudly so numbers never go silently stale."""
    try:
        subprocess.run(["git", "add", "dashboard/data", "journal", "state/x_draft_*.txt"],
                       cwd=ROOT, capture_output=True, text=True)
        r = subprocess.run(["git", "commit", "-m", "Update dashboard data after trading run"],
                           cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            print("  (no data changes to publish)")
            return
        r = subprocess.run(["git", "push", "origin", "main"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        print("  data pushed - Vercel deploying" if r.returncode == 0
              else f"  PUSH FAILED (site going stale): {r.stderr[-300:]}")
    except Exception as e:
        print(f"  PUBLISH FAILED (site going stale): {e}")


def draft_x_summary(fills: list, results: list, context: dict, run_id: str) -> None:
    # Plain tickers, no $cashtags: X rejects multi-cashtag posts (Hermes lesson).
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
    (ROOT / "state" / f"x_draft_{run_id}.txt").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Weekly self-review: audit the week's decisions against outcomes. No trading.
# ---------------------------------------------------------------------------
def self_review(run_id: str) -> int:
    closed = compute_closed_trades()
    portfolio = get_portfolio_state()
    runs = []
    for f in sorted((ROOT / "journal" / "runs").glob("*.jsonl"))[-5:]:
        runs.extend(json.loads(line) for line in f.read_text().splitlines())
    bundle = {"closed_trades": closed, "portfolio": portfolio,
              "recent_run_summaries": runs[-40:]}
    review_file = ROOT / "state" / f"review_{run_id}.json"
    review_file.write_text(json.dumps(bundle, indent=2, default=str))

    prompt = (
        "Weekly self-review for East Equity Agent (no trading this run). Read CLAUDE.md, "
        f"then the audit bundle at {review_file}. Audit your week honestly: for each open "
        "position, is the original thesis tracking or drifting? For each closed trade, was "
        "the exit right in hindsight? Which of your predictions were wrong and WHY - bad "
        "data, bad reasoning, or bad luck? What pattern should change next week? "
        "End with a section titled 'Self-review:' containing 4-8 plain-English sentences "
        "for the public dashboard summarizing what you got right, what you got wrong, and "
        "the one change you are making."
    )
    result = subprocess.run(["claude", "-p", prompt, "--permission-mode", "plan"],
                            cwd=ROOT, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        print(f"self-review failed: {result.stderr[:500]}{result.stdout[:500]}")
        return 1
    m = re.search(r"Self-review:\s*(.+)", result.stdout, re.DOTALL)
    summary = (m.group(1).strip() if m else result.stdout.strip())[:2000]
    journal.log_improvement(f"Weekly self-review: {summary}", run_id)
    _write_review_to_dashboard = ROOT / "dashboard" / "data" / "latest.json"
    if _write_review_to_dashboard.exists():
        d = json.loads(_write_review_to_dashboard.read_text())
        d["improvements"] = ([{"date": datetime.now(timezone.utc).date().isoformat(),
                               "note": f"Weekly self-review: {summary}"}]
                             + d.get("improvements", []))[:10]
        _write_review_to_dashboard.write_text(json.dumps(d, indent=2, default=str))
    redeploy_dashboard()
    print("self-review complete and published")
    return 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--research-only", action="store_true",
                    help="run steps 1-4 only; skip validation/execution")
    ap.add_argument("--self-review", action="store_true",
                    help="weekly audit of decisions vs outcomes; no trading")
    args = ap.parse_args()

    cfg = validator.load_config()
    run_id = f"{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}"
    if args.self_review:
        print(f"=== East Equity Agent self-review {run_id} ===")
        return self_review(run_id)
    print(f"=== East Equity Agent run {run_id} (mode: {cfg['mode']['trading_mode']}) ===")

    halt = preflight(cfg, run_id)
    if halt:
        print(f"HALT: {halt}")
        journal.log_run_summary({"halted": halt}, run_id)
        return 1

    print("[1/5] Gathering context...")
    context = gather_context(cfg)

    print("[2/5] Waking the brain (Claude)...")
    response = ask_claude(context, run_id)
    parsed = parse_proposals(response)
    proposals, no_trade_reason = parsed["proposals"], parsed["no_trade_reason"]
    print(f"      {len(proposals)} proposal(s). {no_trade_reason or ''}")

    for m in re.findall(r"Improvement note:(.+)", response):
        journal.log_improvement(m.strip(), run_id)

    if args.research_only:
        print(json.dumps(proposals, indent=2))
        return 0

    print("[3/5] Validating (pure Python)...")
    results = validator.validate_proposals(proposals, context["portfolio"])
    approved = [r for r in results if r.approved]
    for r in results:
        journal.log_proposal(r.proposal, run_id)
        if not r.approved:
            journal.log_rejection(r.proposal, r.reasons, run_id)
            print(f"      REJECTED {r.proposal.get('ticker')}: {r.reasons}")
        else:
            print(f"      APPROVED {r.proposal.get('ticker')} {r.proposal.get('action')}")

    print("[4/5] Executing...")
    fills = execute(approved, context, cfg, run_id)

    print("[5/5] Journaling + dashboard + X draft...")
    refresh_dashboard(context, response, results, fills, run_id, no_trade_reason,
                      parsed["commentary"], parsed["watchlist"])
    redeploy_dashboard()
    draft_x_summary(fills, results, context, run_id)
    journal.log_run_summary({
        "proposals": len(proposals), "approved": len(approved),
        "fills": len(fills), "no_trade_reason": no_trade_reason,
    }, run_id)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
