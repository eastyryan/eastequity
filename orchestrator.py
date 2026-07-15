"""East Equity Agent — Orchestrator (thin entrypoint).

Deterministic scaffolding around a single Claude invocation. Heavy logic lives
in runlib/ so this file stays a stable import surface for tests, tools, and
scripts:

    1. Safety preflight (kill switch, mode, market day)
    2. Gather context (runlib.context_gather)
    3. Wake Claude once with a SLIM tiered context pack
    4. Parse proposals, process gates, shadow/runner marks
    5. Validate (validator.py) + execute (simulation by default)
    6. Journal, dashboard, X draft

Run:  python orchestrator.py
      python orchestrator.py --research-only
      python orchestrator.py --depth holdings_watchlist
      python orchestrator.py --gather-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import journal
import validator
from execution import corporate_actions, simulated_broker
from tools.portfolio_state import get_portfolio_state
from tools.guidance_ledger import record_guidance

from runlib.core import ROOT, json_safe, et_date, et_now, to_et_date, light_prices
from runlib.depths import (
    DEPTHS,
    depth_allows_new_buys,
    depth_description,
    earnings_deep_dive_session,
    resolve_depth,
    slot_depth_from_hhmm,
)
from runlib.preflight import preflight, acquire_run_lock, release_run_lock
from runlib.analytics import (
    expected_slots,
    build_health,
    build_volatility_context,
    build_stop_engineering,
    build_position_stop_cushion,
    compute_closed_trades,
    compute_performance_stats,
    compute_calibration,
    recent_improvements,
    proposal_ev,
    sector_map,
    sector_exposure,
    build_trade_events,
    build_position_charts,
    update_watchlist_outcomes,
    append_runs_index,
    benchmark_close,
    trade_plans,
)
from runlib.context_gather import gather_context
from runlib.brain_io import (
    apply_live_prices,
    apply_safety_layer,
    ask_claude,
    llm_settings,
    claude_cmd,
    run_claude,
    adversarial_review,
    parse_proposals,
    execute,
)
from runlib.publish import refresh_dashboard, redeploy_dashboard, draft_x_summary
from runlib.reviews import self_review, universe_review, run_freshness_audit
from runlib.context_tiers import (
    slim_context_for_brain,
    compact_learning_pack,
    write_tiered_context,
)

load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# Backward-compatible private aliases (tests + older tools)
# ---------------------------------------------------------------------------
_json_safe = json_safe
_et_date = et_date
_et_now = et_now
_to_et_date = to_et_date
_light_prices = light_prices
_claude_cmd = claude_cmd
_run_claude = run_claude
_llm_settings = llm_settings
_sector_map = sector_map
_sector_exposure = sector_exposure
_proposal_ev = proposal_ev
_benchmark_close = benchmark_close
_trade_plans = trade_plans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--research-only", action="store_true",
                    help="run steps 1-4 only; skip validation/execution")
    ap.add_argument("--self-review", action="store_true",
                    help="weekly audit of decisions vs outcomes; no trading")
    ap.add_argument("--universe-review", action="store_true",
                    help="weekly curation of the watchable universe; no trading")
    ap.add_argument("--freshness-audit", action="store_true",
                    help="audit fundamentals freshness for EVERY universe name, "
                         "publish dashboard/data/freshness_audit.json; no trading")
    ap.add_argument("--manual", action="store_true",
                    help="user-initiated run: exempt from (and not counted in) the daily run budget")
    ap.add_argument("--news-only", action="store_true",
                    help="markets-closed news review: update commentary/watchlist, no trading")
    ap.add_argument("--light", action="store_true",
                    help="light check (pre-market/overnight): position review + news; "
                         "exits allowed, new buys discarded; no full universe scan")
    ap.add_argument("--depth", metavar="DEPTH",
                    help="run depth: light | holdings_watchlist | full | weekly_market | "
                         "evening_review (overrides --light when set)")
    ap.add_argument("--auto-depth", action="store_true",
                    help="resolve run depth from the ET clock via schedule.slot_depths "
                         "(nearest slot; for cloud/scheduled runs — explicit --depth "
                         "and --news-only still win)")
    ap.add_argument("--weekly-market", action="store_true",
                    help="weekly multi-sector market check-in (no trading; breadth map)")
    ap.add_argument("--gather-only", action="store_true",
                    help="cloud mode step 1: write context bundle to state/ and exit")
    ap.add_argument("--act-on", metavar="RESPONSE_FILE",
                    help="cloud mode step 2: validate/execute/publish from a saved brain response")
    ap.add_argument("--context", metavar="CONTEXT_FILE",
                    help="context bundle path (required with --act-on)")
    ap.add_argument("--trigger-run", metavar="TICKERS",
                    help="event-driven run spawned by tools/trigger_watch when a "
                         "watchlist would_buy_at level confirmed on live prices "
                         "(comma-separated tickers; annotation only — all normal "
                         "gates still apply)")
    ap.add_argument("--learning-mark", action="store_true",
                    help="dedicated mark job: shadow book + post-exit runners + "
                         "news cache on mark days + lesson prune; no trading")
    args = ap.parse_args()

    cfg = validator.load_config()
    run_id = f"{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}"
    try:
        run_depth = resolve_depth(
            explicit=args.depth,
            light_flag=args.light,
            news_only=args.news_only,
            weekly_market=args.weekly_market,
            hhmm=f"{et_now():%H%M}" if args.auto_depth else None,
            cfg=cfg,
        )
    except ValueError as e:
        print(f"HALT: {e}")
        return 2

    # Earnings-driven full deep dive: when a universe name reports, escalate the
    # 9am (overnight/pre-market) and 5:30pm (afternoon after-hours) slots to a
    # full cycle and force the reporter(s) into deep research. Scheduled
    # (--auto-depth) runs only; explicit --depth full/weekly and act-on runs
    # (which read run_depth from a pre-gathered bundle) are untouched here.
    earnings_trigger: dict | None = None
    if args.auto_depth and run_depth not in ("full", "weekly_market"):
        try:
            session = earnings_deep_dive_session(f"{et_now():%H%M}", cfg)
            if session:
                from tools.earnings_calendar import earnings_reporters_for_slot
                trig = earnings_reporters_for_slot(session, cfg=cfg)
                if trig.get("reporters"):
                    print(f"  • EARNINGS deep-dive ({session}): "
                          f"{trig['reporters']} reported — escalating "
                          f"'{run_depth}' → 'full'")
                    run_depth = "full"
                    earnings_trigger = trig
        except Exception as e:
            print(f"  (earnings deep-dive check skipped: {e})")

    if args.learning_mark:
        print(f"=== East Equity Agent learning-mark {run_id} ===")
        from tools.learning_mark import run_learning_mark
        out = run_learning_mark(run_id=run_id)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("status") != "error" else 1

    if args.self_review:
        print(f"=== East Equity Agent self-review {run_id} ===")
        return self_review(run_id)
    if args.universe_review:
        print(f"=== East Equity Agent universe review {run_id} ===")
        return universe_review(run_id)
    if args.freshness_audit:
        print(f"=== East Equity Agent universe freshness audit {run_id} ===")
        return run_freshness_audit(run_id)
    print(f"=== East Equity Agent run {run_id} (mode: {cfg['mode']['trading_mode']}, "
          f"depth: {run_depth}) ===")

    # Idempotent migration: positions opened before plan persistence get their
    # journal-derived stop/target/horizon written onto the position record.
    try:
        from tools.portfolio_state import backfill_position_plans
        n = backfill_position_plans()
        if n:
            print(f"  (backfilled persisted plans onto {n} position(s))")
    except Exception as e:
        print(f"  (plan backfill skipped: {e})")

    if args.gather_only:
        # Pure data collection (used by the relay/Action): no preflight gates,
        # no lock, no budget - it must work on weekends and holidays too.
        if cfg["mode"]["trading_mode"] != "dry_run":
            try:
                ca = corporate_actions.apply_corporate_actions()
                if ca.get("events"):
                    print(f"  corporate actions on gather node: {len(ca['events'])} "
                          f"event(s), dividends ${ca.get('dividends_credited_usd', 0)}, "
                          f"splits {ca.get('splits_applied', 0)}")
                    print("CORPORATE_ACTIONS_CHANGED=1")
                if ca.get("errors"):
                    print(f"  (corporate-actions errors on gather: {len(ca['errors'])})")
            except Exception as e:
                print(f"  (gather corporate actions skipped: {e})")
        context = gather_context(cfg, light=args.light, depth=run_depth,
                                 earnings_trigger=earnings_trigger)
        expects_full_scan = run_depth in ("full", "weekly_market")
        scan_empty = expects_full_scan and not context["universe_scan"].get("top_setups") \
            and context["universe_scan"].get("status") not in ("light", "holdings_watchlist")
        macro_bad = context["macro_regime"].get("status") != "ok"
        degraded = macro_bad or scan_empty

        def _feed_ok(key: str) -> bool:
            v = context.get(key) or {}
            return isinstance(v, dict) and v.get("status") not in ("error", "unavailable")
        scan_ctx = context.get("universe_scan") or {}
        low_coverage = bool(
            expects_full_scan and not degraded
            and isinstance(scan_ctx.get("requested"), int) and scan_ctx["requested"] > 0
            and scan_ctx.get("scanned", 0) < 0.8 * scan_ctx["requested"])
        partial = (not degraded) and (low_coverage or not all(
            _feed_ok(k) for k in ("news_and_catalysts", "sec_filings", "insider_activity")))

        relay = ROOT / "data" / "cloud_context.json"
        if degraded and relay.exists():
            cached = json.loads(relay.read_text())
            try:
                age_h = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(cached["run_date"])).total_seconds() / 3600
            except Exception:
                age_h = 999.0
            cached["portfolio"] = context["portfolio"]
            cached["hard_limits"] = context["hard_limits"]
            # THIS RUN's slot depth governs gating and the brain's job — never
            # the depth the bundle happened to be gathered at. A bundle gathered
            # off-slot (throttled crons fire late) came stamped e.g. "light" and
            # was turning 9am/10am TRADING slots into no-BUY light runs; it also
            # erased the earnings-escalation depth. Keep the bundle's own depth
            # visible for the data_quality note only.
            bundle_depth = cached.get("run_depth")
            cached["run_depth"] = run_depth
            cached["run_depth_note"] = depth_description(run_depth)
            severity = "still fresh" if age_h < 4 else "STALE"
            cached["stale_data_notice"] = (
                f"Live data feeds unreachable here; using the relay bundle gathered "
                f"{age_h:.1f}h ago ({severity}). Prices and news may be up to that old - "
                f"lower confidence and avoid time-sensitive entries accordingly.")
            cached["data_quality"] = {"source": "relay_bundle", "age_hours": round(age_h, 1),
                                      "stale": age_h >= 4,
                                      "bundle_gathered_at_depth": bundle_depth}
            context = cached
            print(f"  (live feeds blocked - using relay bundle, {age_h:.1f}h old)")
        elif degraded:
            context["stale_data_notice"] = (
                "Live feeds failed AND no relay bundle is available - this context is "
                "largely EMPTY. Treat scan/macro data as missing; do not open new "
                "positions on absent data, and say so in commentary.")
            context["data_quality"] = {"source": "degraded_empty", "stale": True}
            print("  (degraded and no relay bundle - handing a labeled empty context)")
        elif partial:
            note = ("universe scan covered only "
                    f"{scan_ctx.get('scanned')}/{scan_ctx.get('requested')} names - "
                    "missing names have no fresh prices; treat absent setups as "
                    "unscanned, not unattractive"
                    if low_coverage else
                    "price scan OK but one or more research feeds "
                    "(news/filings/insiders) failed")
            context["data_quality"] = {"source": "live_partial", "note": note}
            print(f"  (partial degradation - {note[:80]}; labeled)")

        try:
            positions = (context.get("portfolio") or {}).get("positions", [])
            pcs = build_position_charts(positions)
            pc_file = ROOT / "dashboard" / "data" / "position_charts.json"
            pc_file.parent.mkdir(parents=True, exist_ok=True)
            if pcs or not positions:
                pc_file.write_text(json.dumps(json_safe(pcs), indent=2))
                print(f"  (gather: position_charts for {list(pcs.keys()) or 'no positions'})")
            else:
                print("  (gather: position_charts feed empty - keeping last-good file)")
        except Exception as e:
            print(f"  (gather position_charts skipped: {e})")

        out = ROOT / "state" / f"context_{run_id}.json"
        full_out = ROOT / "state" / f"context_full_{run_id}.json"
        try:
            write_tiered_context(context, out, full_out)
            print(f"CONTEXT_FILE={out}")
            print(f"CONTEXT_FULL_FILE={full_out}")
        except Exception as e:
            print(f"  (tiered gather write failed: {e}; writing full only)")
            out.write_text(json.dumps(json_safe(context), indent=2, default=str))
            print(f"CONTEXT_FILE={out}")
        return 0

    # Preflight weekend/lease: treat non-trading depths like news-only so Sunday
    # weekly check-ins are allowed. Brain proposals are suppressed separately.
    no_brain_orders = (
        args.news_only
        or run_depth in ("evening_review", "weekly_market")
        or not depth_allows_new_buys(run_depth)
    )
    # Safety layer (stops/horizons) still runs on weekly_market and focused depths.
    run_safety = run_depth not in ("evening_review",) and not args.news_only
    halt = preflight(
        cfg, run_id,
        news_only=args.news_only or run_depth in ("evening_review", "weekly_market"),
        manual=args.manual,
    )
    if halt:
        print(f"HALT: {halt}")
        journal.log_run_summary({"halted": halt, "run_depth": run_depth}, run_id)
        return 1

    forced_exit_fills = []
    if args.act_on:
        print("[1-2/5] Loading saved context + brain response (cloud mode)...")
        # Prefer full archive when act-on was given a slim path (same run_id).
        ctx_path = Path(args.context)
        full_sibling = ctx_path.with_name(ctx_path.name.replace("context_", "context_full_"))
        load_path = full_sibling if full_sibling.exists() else ctx_path
        if load_path != ctx_path:
            print(f"  (using full archive {load_path.name} for act-on)")
        context = json.loads(load_path.read_text())
        run_depth = context.get("run_depth") or run_depth
        # Re-derive the trading gates from the bundle's ACTUAL depth: acting on
        # a light/evening context without a matching --depth flag must not run
        # with full-depth gating (BUYs would survive a no-BUY slot).
        no_brain_orders = (
            args.news_only
            or run_depth in ("evening_review", "weekly_market")
            or not depth_allows_new_buys(run_depth)
        )
        run_safety = run_depth not in ("evening_review",) and not args.news_only
        # Overlay the frequent holdings/watchlist live feed onto the bundle's
        # daily-bar prices so an intraday stop breach is caught this run, not at
        # the next sparse full gather.
        apply_live_prices(context, cfg)
        bundle_prices = (context.get("universe_scan") or {}).get("prices") or {}
        if bundle_prices:
            try:
                simulated_broker.mark_to_market(bundle_prices)
            except Exception as e:
                print(f"  (mark-to-market from bundle failed: {e})")
        context["portfolio"] = get_portfolio_state()
        if run_safety:
            forced_exit_fills = apply_safety_layer(context, cfg, run_id)
        response = Path(args.act_on).read_text()
    else:
        print(f"[1/5] Gathering context (depth={run_depth})...")
        context = gather_context(cfg, light=args.light, depth=run_depth,
                                 earnings_trigger=earnings_trigger)
        apply_live_prices(context, cfg)
        if args.trigger_run:
            context["trigger_run_note"] = (
                f"EVENT-DRIVEN RUN: watchlist would_buy_at level(s) CONFIRMED on "
                f"live prices for {args.trigger_run} (two consecutive ~5-min ticks "
                f"inside the band). This run exists because your own published "
                f"trigger hit — deep-research those names FIRST and decide "
                f"drop / hold / promote-to-BUY explicitly. The fat-pitch bar and "
                f"all validator rules still apply; an unconvincing setup at the "
                f"level is a legitimate pass (say why).")
        if run_safety:
            forced_exit_fills = apply_safety_layer(context, cfg, run_id)
        print("[2/5] Waking the brain (Claude)...")
        response = ask_claude(
            context, run_id,
            news_only=args.news_only or run_depth in ("evening_review", "weekly_market"),
        )

    parsed = parse_proposals(response)
    proposals, no_trade_reason = parsed["proposals"], parsed["no_trade_reason"]

    # Process gates + learning marks (shadows, post-exit runners, news cache).
    try:
        from tools.process_gates import audit_brain_process
        top_scan = [
            r.get("ticker") for r in
            ((context.get("universe_scan") or {}).get("top_setups") or [])[:8]
            if r.get("ticker")
        ]
        audit = audit_brain_process(
            parsed, depth=run_depth, proposals=proposals,
            top_scan_tickers=top_scan, universe=validator.load_universe())
        parsed["watchlist"] = audit.get("watchlist") or parsed.get("watchlist") or []
        parsed["rejected_ideas"] = audit.get("rejected_ideas") or []
        parsed["process_audit"] = {
            "process_ok": audit.get("process_ok"),
            "full_run_no_trade_ok": audit.get("full_run_no_trade_ok"),
            "issues": audit.get("issues") or [],
        }
        if audit.get("issues"):
            print(f"      process gates: {audit['issues'][:6]}")
            journal.log_improvement(
                "Process gate: " + "; ".join(audit["issues"][:12]), run_id)
        if (run_depth == "full" and not proposals
                and audit.get("full_run_no_trade_ok") is False):
            tag = (" [process: full-run no-trade needs rejected_ideas "
                   "with >=2 {ticker, reason}]")
            if no_trade_reason and tag.strip() not in str(no_trade_reason):
                no_trade_reason = str(no_trade_reason) + tag
                parsed["no_trade_reason"] = no_trade_reason
        try:
            from tools.shadow_portfolio import (
                mark_shadows, record_from_rejected_ideas, record_from_watchlist,
            )
            prices = (context.get("universe_scan") or {}).get("prices") or {}
            n_rej = record_from_rejected_ideas(
                parsed.get("rejected_ideas") or [], prices, run_id=run_id)
            alerts = ((context.get("watchlist_trigger_alerts") or {}).get("alerts")
                      or [])
            n_wl = record_from_watchlist(
                parsed.get("watchlist") or [], prices, alerts, run_id=run_id)
            mark_shadows(prices)
            if n_rej or n_wl:
                print(f"      shadow book: +{n_rej} rejects, +{n_wl} watch entries")
            try:
                from tools.post_exit_runners import mark_post_exit_runners
                pr = mark_post_exit_runners(prices, cache_news_on_marks=True)
                if pr.get("completed_now"):
                    print(f"      post-exit runners completed: {pr['completed_now']}")
                if pr.get("news_cached"):
                    print(f"      post-exit news cached on marks: {pr['news_cached']}")
            except Exception as e:
                print(f"      (post-exit runners skipped: {e})")
            # Opportunistic lesson prune (cheap, fail-soft)
            try:
                from tools.learning_adopt import prune_adopted_lessons
                pruned = prune_adopted_lessons()
                if pruned.get("pruned"):
                    print(f"      lessons pruned: {pruned['pruned']}")
            except Exception as e:
                print(f"      (lesson prune skipped: {e})")
        except Exception as e:
            print(f"      (shadow portfolio skipped: {e})")
    except Exception as e:
        print(f"      (process gates skipped: {e})")

    if args.news_only or run_depth in ("evening_review", "weekly_market"):
        proposals = []  # commentary-only depths never trade
    elif no_brain_orders:
        dropped = [p for p in proposals if str(p.get("action", "")).upper() == "BUY"]
        for p in dropped:
            journal.log_rejection(p, [f"{run_depth}_run_no_new_buys"], run_id)
        proposals = [p for p in proposals if str(p.get("action", "")).upper() != "BUY"]
    print(f"      {len(proposals)} proposal(s). {no_trade_reason or ''}")

    for m in re.findall(r"Improvement note:(.+)", response):
        journal.log_improvement(m.strip(), run_id)

    if parsed.get("guidance_entries"):
        try:
            gsum = record_guidance(parsed["guidance_entries"])
            print(f"      guidance ledger: {gsum['recorded']} recorded, "
                  f"{gsum['replaced']} replaced, {len(gsum['invalid'])} invalid")
        except Exception as e:
            print(f"      (guidance ledger write failed: {e})")

    if args.research_only:
        print(json.dumps(proposals, indent=2))
        return 0

    if proposals and not (args.news_only or run_depth in ("evening_review", "weekly_market")):
        print("[2.5/5] Risk desk review...")
        ctx_path = args.context if args.act_on else str(ROOT / "state" / f"context_{run_id}.json")
        # Risk desk benefits from full archive if present
        full_p = Path(str(ctx_path).replace("context_", "context_full_"))
        if full_p.exists():
            ctx_path = str(full_p)
        proposals = adversarial_review(proposals, ctx_path, run_id)

    print("[3/5] Validating (pure Python)...")
    live_portfolio = get_portfolio_state()
    context["portfolio"] = live_portfolio

    risk_halts = []
    try:
        hist_file = ROOT / "dashboard" / "data" / "equity_history.json"
        equity_hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
        risk_halts = validator.risk_halt_reasons(
            live_portfolio.get("total_equity_usd"), equity_hist, cfg, today_et=et_date())
    except Exception as e:
        print(f"  (risk-halt check failed open: {e})")
    context["risk_halts"] = risk_halts
    if risk_halts:
        print(f"  RISK HALT ACTIVE: {risk_halts} - new BUYs blocked this run")
        for p in proposals:
            if str(p.get("action", "")).upper() == "BUY":
                journal.log_rejection(p, risk_halts, run_id)
        proposals = [p for p in proposals if str(p.get("action", "")).upper() != "BUY"]
    market_context = build_volatility_context(
        context.get("universe_scan") or {}, context.get("options_signals") or {})
    for p in proposals:
        if str(p.get("action", "")).upper() != "BUY":
            continue
        t = str(p.get("ticker", "")).upper()
        mcap = None
        try:
            mcap = (((context.get("deep_fundamentals") or {}).get(t) or {})
                    .get("quality_ratios", {}).get("ratios", {})
                    .get("market_cap_usd", {}) or {}).get("value")
        except Exception:
            mcap = None
        if not mcap:
            scan_ctx = context.get("universe_scan") or {}
            for r in (scan_ctx.get("top_setups") or []) + (scan_ctx.get("contrarian_setups") or []) \
                     + (scan_ctx.get("deep_value_200w") or []) + (scan_ctx.get("supplier_pullbacks") or []):
                if r.get("ticker") == t and r.get("market_cap_usd"):
                    mcap = r["market_cap_usd"]
                    break
        if not mcap:
            try:
                import yfinance as yf
                mcap = yf.Ticker(t).fast_info.get("marketCap")
            except Exception:
                mcap = None
        if mcap:
            market_context.setdefault(t, {})["market_cap_usd"] = float(mcap)
    results = validator.validate_proposals(proposals, live_portfolio, market_context)
    approved = [r for r in results if r.approved]
    for r in results:
        journal.log_proposal(r.proposal, run_id)
        if not r.approved:
            journal.log_rejection(r.proposal, r.reasons, run_id)
            print(f"      REJECTED {r.proposal.get('ticker')}: {r.reasons}")
        else:
            print(f"      APPROVED {r.proposal.get('ticker')} {r.proposal.get('action')}")

    print("[4/5] Executing...")
    fills = forced_exit_fills + execute(approved, context, cfg, run_id)

    print("[5/5] Journaling + dashboard + X draft...")
    publish_prices = (context.get("universe_scan") or {}).get("prices") or {}
    if publish_prices:
        try:
            simulated_broker.mark_to_market(publish_prices)
        except Exception as e:
            print(f"  (mark-to-market failed: {e})")
    context["portfolio"] = get_portfolio_state()
    refresh_dashboard(context, response, results, fills, run_id, no_trade_reason,
                      parsed["commentary"], parsed["watchlist"])
    draft_x_summary(fills, results, context, run_id, parsed.get("x_post"))
    journal.log_run_summary({
        "manual": args.manual,
        "trigger_run": args.trigger_run or None,
        "run_depth": run_depth,
        "proposals": len(proposals), "approved": len(approved),
        "fills": len(fills), "no_trade_reason": no_trade_reason,
        "process_audit": parsed.get("process_audit"),
        "rejected_ideas": parsed.get("rejected_ideas") or [],
    }, run_id)
    redeploy_dashboard()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
