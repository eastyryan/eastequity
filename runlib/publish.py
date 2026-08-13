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
    recent_rejected_ideas,
)

def _target_calibration(closed: list) -> dict:
    """Grade claimed RR vs realized RR over the closed book. Fail-soft: a
    learning-layer failure must never take the dashboard publish down with it."""
    try:
        from tools.exit_autopsy import build_target_calibration
        from tools.calibration_gate import _load_cfg_block
        return build_target_calibration(
            closed, min_trades_for_binding=_load_cfg_block()["min_trades_for_binding"])
    except Exception as e:
        print(f"  (target calibration unavailable: {str(e)[:120]})")
        return {"status": "unavailable", "binding": False, "phase": "anecdote",
                "n_gradeable": 0, "rr_inflation": None}


def _with_unrealized(pos: dict) -> dict:
    """Attach mark-to-market P/L to an open position for the dashboard.

    The site has always rendered an UNREALIZED column off `unrealized_pct`, but
    broker state carries only avg_cost / last_price / market_value_usd, so the
    field was never written and every open position displayed a dash. Derive it
    here from the same mark the equity curve uses. Price-only: dividends are
    realized cash and are reported separately as total_dividends_usd.
    """
    out = dict(pos)
    try:
        qty = float(pos.get("quantity") or 0.0)
        cost = float(pos.get("avg_cost") or 0.0)
        last = pos.get("last_price")
        if last in (None, "") and qty:
            mv = pos.get("market_value_usd")
            last = (float(mv) / qty) if mv not in (None, "") else None
        last = float(last) if last not in (None, "") else None
    except (TypeError, ValueError, ZeroDivisionError):
        return out
    if last is None or cost <= 0 or qty <= 0:
        return out
    out["last_price"] = round(last, 4)
    out["unrealized_pct"] = round((last / cost - 1) * 100, 2)
    out["unrealized_usd"] = round((last - cost) * qty, 2)
    return out


def refresh_dashboard(context: dict, response: str, results: list, fills: list,
                      run_id: str, no_trade_reason: str | None = None,
                      commentary: str | None = None,
                      watchlist: list | None = None,
                      rejected_ideas: list | None = None) -> None:
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
    # "Why it didn't buy" surface. On an empty book the formal proposals list is blank
    # almost every run, so the real signal is rejected_ideas: names the agent researched
    # and consciously passed on, with reasons. rejected_recent aggregates them across the
    # last ~25 runs, one freshest row per ticker, for the front page. The CURRENT run is
    # not journaled until AFTER this function runs, so fold it in by hand here so the page
    # reflects the run that just finished, not only the prior history.
    _rej_by_tkr: dict = {r["ticker"]: dict(r) for r in recent_rejected_ideas()}
    _today_et = et_date()
    for _idea in (rejected_ideas or []):
        _tk = str((_idea or {}).get("ticker", "")).upper().strip()
        if not _tk:
            continue
        _reason = str((_idea or {}).get("reason", "")).strip()
        _prev = _rej_by_tkr.get(_tk)
        if _prev is None:
            _rej_by_tkr[_tk] = {"ticker": _tk, "reason": _reason,
                                "last_seen": _today_et, "count": 1}
        else:
            _prev["reason"] = _reason  # current run's reason is the freshest
            _prev["last_seen"] = _today_et
            _prev["count"] = _prev.get("count", 0) + 1
    rejected_recent = sorted(
        _rej_by_tkr.values(),
        key=lambda r: (r.get("last_seen") or "", r.get("count", 0)),
        reverse=True)[:12]
    out = {
        "no_trade_reason": no_trade_reason,
        "commentary": commentary,
        "watchlist": watchlist or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": context["trading_mode"],
        "schedule_note": "Runs at 6am, 8:45am, 10:30am, 12pm, 2pm and 3:30pm ET, a "
                         "5:30pm research review, plus overnight and weekend news checks",
        "portfolio": {
            "cash_usd": context["portfolio"].get("cash_usd"),
            "total_equity_usd": context["portfolio"].get("total_equity_usd"),
            # Attach sector (exposure view) and live P/L (positions table) to each
            # position. Both are display-only derivations - broker state stays the
            # source of truth for quantity, cost and mark.
            "positions": [_with_unrealized(
                              {**p, "sector": sector_map().get(p.get("ticker", "").upper())})
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
        # Ideas the agent researched and passed on THIS run (ticker + reason). Distinct
        # from proposals: these never reached the validator - the agent skipped them.
        "rejected_ideas": rejected_ideas or [],
        # Same, aggregated across recent runs for the front page (one row per ticker).
        "rejected_recent": rejected_recent,
        "fills": fills,
        "closed_trades": closed[::-1],
        "performance": compute_performance_stats(closed, hist),
        "performance_breakdown": build_performance_breakdown(closed),
        "calibration": compute_calibration(closed),
        # The key tools/calibration_gate.load_target_calibration() reads back.
        # Without it published here the gate reads nothing and the 2:1 rule stays
        # ungraded — which is the state it was in.
        "target_calibration": _target_calibration(closed),
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
    append_runs_index(run_id, context.get("trading_mode", "paper"), fills, commentary,
                      no_trade_reason, rejected_ideas)

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
        # MATERIALIZE THE KNOWLEDGE BASE BEFORE the .exists()-gated path list
        # below. Listing data/knowledge_base.json there was purely decorative:
        # every learning-store entry is gated on the file existing, and that file
        # had NEVER existed in this repo (untracked, and not gitignored) while its
        # derived public view dashboard/data/learning_journal.json was committed
        # and carried a real lesson. So the store was recreated empty on every
        # ephemeral runner, the git-add skipped it for not existing, and the loop
        # stayed dead. ensure_store() rebuilds it from the published journal, which
        # is what turns that path entry from decoration into persistence.
        try:
            from tools.knowledge_base import ensure_store
            kb = ensure_store()
            if kb.get("status") == "rebuilt_from_published":
                print(f"  knowledge base rebuilt from published journal: {kb.get('ids')}")
        except Exception as e:
            print(f"  (knowledge base ensure_store failed: {str(e)[:120]})")

        paths = ["dashboard/data", "journal", "state/portfolio.json",
                 # queued broker orders from cloud runs — the push of this file
                 # is what TRIGGERS the Actions executor (execute-orders.yml)
                 "state/order_intents.json",
                 # UNCONDITIONAL, and that is the fix. This entry used to be
                 # gated on `if (ROOT / "state" / "KILL_SWITCH").exists()`, which
                 # made the "add -A so a REMOVED kill switch (all-clear) also
                 # propagates" comment below unachievable by construction: on the
                 # run where an operator REMOVES the switch, the file does not
                 # exist, so the path was never in this list, so `git add -A` was
                 # never called for it and the deletion never staged. Engaging a
                 # halt propagated; lifting one did not.
                 #
                 # It fails safe — the cloud stays halted — but it misleads in the
                 # worst direction available to an operator: the local switch is
                 # gone, the publish reports success, and every remote node is
                 # still refusing to run. Absent must not read as all-clear.
                 # `git add -A` on a path that is both missing and untracked is a
                 # no-op whose error the loop below already swallows.
                 "state/KILL_SWITCH"]
        paths += _glob.glob(str(ROOT / "state" / "x_draft_*.txt"))
        if (ROOT / "data" / "cusip_map.json").exists():
            paths.append("data/cusip_map.json")  # learned ticker->CUSIP, must persist in cloud
        if (ROOT / "data" / "ai_exposure.json").exists():
            paths.append("data/ai_exposure.json")  # business-reality labels, review-maintained
        # LEARNING STORES. These were never committed, so on an ephemeral cloud
        # runner every one of them was reconstructed empty each run. The knowledge
        # base is the clearest casualty: data/knowledge_base.json has never existed
        # in this repo (untracked, and NOT gitignored) while its derived public view
        # dashboard/data/learning_journal.json IS tracked and carries a lesson. So a
        # study session ran, published the journal, and the source of truth was lost
        # — after which record_citations / link_lessons_to_trade / update_lesson_outcomes
        # all iterate an empty list and return 0, silently, forever.
        for learning_file in ("knowledge_base.json", "adopted_lessons.json",
                              "learning_proposals.json", "shadow_portfolio.json",
                              "post_exit_runners.json", "binding_exit_lessons.json"):
            if (ROOT / "data" / learning_file).exists():
                paths.append(f"data/{learning_file}")
        if (ROOT / "data" / "concept_memory").is_dir():
            paths.append("data/concept_memory")
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
            # --autostash is REQUIRED, not a nicety. A trading run writes several
            # tracked files that are deliberately NOT in the add-list above
            # (data/universe.json, data/earnings_calendar.json,
            # data/discovery_screen.json, data/fundamental_screen.json,
            # data/universe_history.jsonl, data/cache/*), so the working tree is
            # ALWAYS dirty at this point. Plain `git pull --rebase` refuses outright
            # with "cannot pull with rebase: You have unstaged changes", the abort
            # below fires, and the dashboard silently goes stale — every single time
            # origin has moved, which with the relay and Actions pushing is most
            # runs. Reproduced on the 2026-07-19 dry run.
            #
            # Note this is autostash WITHOUT the `git reset --hard` fallback that
            # scripts/relay_data.sh used to carry. The reset is what destroyed
            # uncommitted work; stashing and popping around a rebase is the ordinary,
            # safe remedy and is what lets a dirty tree rebase at all.
            rb = subprocess.run(["git", "pull", "--rebase", "--autostash",
                                 "origin", "main"],
                                cwd=ROOT, capture_output=True, text=True, timeout=120)
            if rb.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], cwd=ROOT,
                               capture_output=True, text=True)
                # A local commit exists that was never pushed. Say so plainly: the
                # stale site is the visible symptom, the unpushed commit is the
                # thing that will surprise the next run.
                print(f"  REBASE FAILED — local commit is UNPUSHED and the site is "
                      f"stale; resolve by hand: {rb.stderr[-300:]}")
                return
        print(f"  PUSH FAILED after retries (site going stale): {r.stderr[-300:]}")
    except Exception as e:
        print(f"  PUBLISH FAILED (site going stale): {e}")


def _fill_facts(fills: list, context: dict) -> list[dict]:
    """Compact per-fill facts for the memo: entry/exit, P&L, hold time, plan,
    and whether the safety layer (not the brain) forced the exit."""
    forced_by = {str(fx.get("ticker", "")).upper(): fx.get("reason", "forced exit")
                 for fx in (context.get("forced_exits") or [])}
    facts = []
    for f in fills:
        t = str(f.get("ticker", "")).upper()
        fact = {"ticker": t, "action": f.get("action"),
                "fill_price": f.get("fill_price"), "quantity": f.get("quantity")}
        if str(f.get("action", "")).upper() != "BUY":
            entry = f.get("avg_cost")
            if entry:
                fact["entry_price"] = entry
                try:
                    fact["pnl_pct"] = round((float(f["fill_price"]) - float(entry))
                                            / float(entry) * 100, 1)
                except Exception:
                    pass
            fact["realized_pnl_usd"] = f.get("realized_pnl_usd")
            if f.get("position_opened_at"):
                try:
                    opened = datetime.fromisoformat(str(f["position_opened_at"]))
                    fact["held_days"] = (datetime.now(timezone.utc) - opened).days
                except Exception:
                    pass
            if f.get("sell_fraction") and float(f["sell_fraction"]) < 1.0:
                fact["partial"] = f["sell_fraction"]
            if t in forced_by:
                fact["forced_exit_reason"] = forced_by[t]
        plan = f.get("entry_plan") or {}
        for src, key in ((f, "stop_loss"), (f, "target_price"), (plan, "stop_loss"),
                         (plan, "target_price"), (plan, "holding_horizon_days")):
            v = src.get(key) if isinstance(src, dict) else None
            if v and key not in fact:
                fact[key] = v
        facts.append(fact)
    return facts


def _brain_trade_memo(fills: list, context: dict) -> str | None:
    """Have the brain write the trade-day X memo at act time. Needed because
    forced exits execute AFTER the brain wrote its response (it cannot narrate
    an exit it never saw), and cloud brains sometimes omit x_post. Fail-soft:
    returns None and the deterministic fallback below publishes instead."""
    try:
        from runlib.brain_io import run_claude
        facts = {
            "date_et": et_date(),
            "fills": _fill_facts(fills, context),
            "portfolio_equity_usd": (context.get("portfolio") or {}).get("total_equity_usd"),
            "run_commentary": (context.get("latest_reasoning") or {}).get("commentary")
                              or context.get("commentary"),
        }
        prompt = (
            "Write the trade-day journal post for East Equity Agent's public X account, "
            "first person, in the voice of a sharp fund manager writing a trade memo — "
            "not a bot alert. Use ONLY the numbers in the FACTS JSON below; never invent "
            "prices, percentages, or dates.\n\n"
            "Structure, 3-6 short paragraphs: what was done and at what price (entry vs "
            "exit, P&L, holding period); WHY — a forced_exit_reason means the coded "
            "safety layer enforced a pre-committed stop, own the discipline; what the "
            "lesson or read is; what the book looks like now and what you are watching. "
            "Plain English, no jargon, no hedging boilerplate, no hashtags, no title "
            "line.\n\n"
            "FORMATTING (exact): every company mention is a bolded name followed by a "
            "plain cashtag - **Dell** $DELL - every time it appears; no other markdown. "
            "End with: \"This is a paper-trading experiment running in public, not "
            "advice.\"\n\n"
            f"FACTS:\n{json.dumps(json_safe(facts), indent=1, default=str)}\n\n"
            "Return ONLY the post text."
        )
        memo = (run_claude(prompt, call="trade_memo") or "").strip()
        # Sanity: long enough to be a memo, short enough for one long-form post.
        if 200 <= len(memo) <= 20000:
            return memo
        print(f"  (brain memo rejected: {len(memo)} chars)")
    except Exception as e:
        print(f"  (brain trade memo unavailable: {str(e)[:120]})")
    return None


def draft_x_summary(fills: list, results: list, context: dict, run_id: str,
                    x_post: str | None = None) -> None:
    # Trade drafts get a _trade filename suffix so the poster prioritizes them.
    suffix = "_trade" if fills else ""
    path = ROOT / "state" / f"x_draft_{run_id}{suffix}.txt"
    if x_post and x_post.strip():
        # The brain wrote its own post (fund-manager memo style). Publish verbatim.
        path.write_text(x_post.strip())
        return
    if fills:
        memo = _brain_trade_memo(fills, context)
        if memo:
            path.write_text(memo)
            return
    # Deterministic fallback. Plain tickers, no $cashtags (X rejects multi-cashtag
    # posts); carries entry/exit/P&L so even the fallback reads like a journal.
    lines = [f"East Equity Agent swing update ({datetime.now():%b %d})"]
    if fills:
        for fact in _fill_facts(fills, context):
            if str(fact.get("action", "")).upper() == "BUY":
                bits = [f"Opened {fact['ticker']} @ {fact['fill_price']}"]
                if fact.get("stop_loss") and fact.get("target_price"):
                    bits.append(f"(stop {fact['stop_loss']}, target {fact['target_price']})")
                lines.append(" ".join(bits))
            else:
                bits = [f"Closed {fact['ticker']} @ {fact['fill_price']}"]
                detail = []
                if fact.get("entry_price"):
                    detail.append(f"entry {fact['entry_price']}")
                if fact.get("pnl_pct") is not None:
                    detail.append(f"{fact['pnl_pct']:+.1f}%")
                if fact.get("held_days") is not None:
                    detail.append(f"{fact['held_days']}d hold")
                if detail:
                    bits.append("(" + ", ".join(detail) + ")")
                if fact.get("forced_exit_reason"):
                    bits.append(f"- safety layer: {fact['forced_exit_reason']}")
                lines.append(" ".join(bits))
    else:
        lines.append("No new trades today — no setup met the bar.")
    eq = context["portfolio"].get("total_equity_usd")
    if eq:
        lines.append(f"Portfolio equity: ${eq:,.0f}")
    lines.append("Full reasoning on the dashboard. Long-only swing trades. Not financial advice.")
    path.write_text("\n".join(lines))
