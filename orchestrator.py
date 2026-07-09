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

    return {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "trading_mode": cfg["mode"]["trading_mode"],
        "macro_regime": macro,
        "portfolio": portfolio,
        "universe_scan": scan,
        "sec_filings": filings,
        "smart_money_13f": smart_money,
        "news_and_catalysts": news,
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
    result = subprocess.run(
        ["claude", "-p", prompt, "--permission-mode", "plan"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:2000]}")
    return result.stdout


# ---------------------------------------------------------------------------
# Step 4 — Parse structured proposals out of the brain's response
# ---------------------------------------------------------------------------
def parse_proposals(response: str) -> tuple[list[dict], str | None]:
    blocks = re.findall(r"```json\s*(.*?)```", response, re.DOTALL)
    for block in reversed(blocks):  # last JSON block wins
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "proposals" in data:
                return data["proposals"], data.get("no_trade_reason")
        except json.JSONDecodeError:
            continue
    return [], "no parsable proposals block in response"


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

    for vr in approved[:max_orders]:
        p = vr.proposal
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
        print(f"  FILLED {fill['action']} {fill['ticker']} "
              f"{fill['quantity']} @ {fill['fill_price']}")
    return fills


# ---------------------------------------------------------------------------
# Step 7 — Dashboard data + X summary
# ---------------------------------------------------------------------------
def refresh_dashboard(context: dict, response: str, results: list, fills: list,
                      run_id: str) -> None:
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": context["trading_mode"],
        "portfolio": {k: context["portfolio"].get(k) for k in
                      ("cash_usd", "total_equity_usd", "positions")},
        "macro_regime_hint": context["macro_regime"].get("regime_hint"),
        "latest_reasoning": response,
        "proposals": [{"proposal": r.proposal, "approved": r.approved,
                       "reasons": r.reasons} for r in results],
        "fills": fills,
    }
    dash = ROOT / "dashboard" / "data"
    dash.mkdir(parents=True, exist_ok=True)
    (dash / "latest.json").write_text(json.dumps(out, indent=2, default=str))
    (dash / f"run_{run_id}.json").write_text(json.dumps(out, indent=2, default=str))


def draft_x_summary(fills: list, results: list, context: dict, run_id: str) -> None:
    lines = [f"East Equity Agent — daily swing update ({datetime.now():%b %d})"]
    if fills:
        for f in fills:
            lines.append(f"{'Opened' if f['action'] == 'BUY' else 'Closed'} "
                         f"${f['ticker']} @ {f['fill_price']}")
    else:
        lines.append("No new trades today — no setup met the bar.")
    eq = context["portfolio"].get("total_equity_usd")
    if eq:
        lines.append(f"Portfolio equity: ${eq:,.0f}")
    lines.append("Full reasoning on the dashboard. Long-only swing trades. Not financial advice.")
    (ROOT / "state" / f"x_draft_{run_id}.txt").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--research-only", action="store_true",
                    help="run steps 1-4 only; skip validation/execution")
    args = ap.parse_args()

    cfg = validator.load_config()
    run_id = f"{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}"
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
    proposals, no_trade_reason = parse_proposals(response)
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
    refresh_dashboard(context, response, results, fills, run_id)
    draft_x_summary(fills, results, context, run_id)
    journal.log_run_summary({
        "proposals": len(proposals), "approved": len(approved),
        "fills": len(fills), "no_trade_reason": no_trade_reason,
    }, run_id)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
