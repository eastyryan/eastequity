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

from tools.envload import load_env

import journal
import validator
from execution import broker, corporate_actions
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

load_env()


def _should_mark_run_start(auto_depth: bool) -> bool:
    """Whether this --gather-only invocation should push a run_started breadcrumb.

    TRUE only for a cloud trading-ROUTINE gather. The hourly GitHub bundle-refresh
    Action ALSO runs `orchestrator.py --gather-only --auto-depth`, so --auto-depth alone
    cannot tell them apart — but GitHub Actions always sets GITHUB_ACTIONS in the
    environment and the CCR routine sandbox does not. Marking on every bundle refresh
    would scatter false "run started" records (~7/day) that no brain run backs, breaking
    the died-vs-missed distinction the breadcrumb exists to draw.
    """
    import os
    return bool(auto_depth) and not os.environ.get("GITHUB_ACTIONS")


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


# Feed statuses that mean "this feed did not deliver", regardless of shape.
DEAD_FEED_STATUSES = ("error", "unavailable", "degraded_all_feeds_empty")


def feed_is_alive(feed) -> bool:
    """Is this research feed actually alive?

    The predicate this replaces was `v.get("status") not in ("error","unavailable")`,
    written as a closure inside main() and therefore never unit-tested — which is how
    it stayed DEAD CODE for two of the three feeds it guarded:
      - `sec_filings` is a {TICKER: brief} map with NO top-level status, so
        .get("status") returned None, None is not in the tuple, and a total EDGAR
        outage read as healthy;
      - `news_and_catalysts` hardcodes {"status": "ok"} and never downgrades it no
        matter how many per-ticker calls fail.
    Only `insider_activity` ever reported failure. The system's single automated
    data-health check was one-third functional, and on the four holdings_watchlist
    slots — the primary trading slots — it was not consulted at all.

    This also inspects the per-ticker layer, so a feed advertising "ok" while every
    one of its entries carries an error is correctly called dead.

    Pure. Module-level specifically so it can be tested.
    """
    if not isinstance(feed, dict):
        return False
    if feed.get("status") in DEAD_FEED_STATUSES:
        return False
    entries = feed.get("tickers") if isinstance(feed.get("tickers"), dict) else {
        k: e for k, e in feed.items()
        if isinstance(e, dict) and k.isalpha() and k.upper() == k}
    if entries:
        bad = sum(1 for e in entries.values()
                  if e.get("error") or e.get("status") in DEAD_FEED_STATUSES)
        if bad == len(entries):
            return False   # every name failed — the top-level "ok" was a lie
    return True


def assess_bundle_health(context: dict, run_depth: str) -> dict:
    """How well was this bundle gathered? Flags only — no relay substitution.

    Extracted from the --gather-only branch on 2026-07-19. It had lived entirely
    inside `if args.gather_only:`, so the LIVE TRADING PATH set no data_quality
    label at all — and validator._check_data_quality opens with:

        dq = (market_context or {}).get("_data_quality") or {}
        if not dq:
            return   # nothing asserted, nothing to enforce

    So `data_quality_stale`, `data_quality_empty` and `fundamentals_stale:<TICKER>`
    — which CLAUDE.md advertises as "real rejections, not prose" — were unreachable
    on every real trading run. Measured across the last 12 runs: dq=None, 12/12.
    """
    scan_ctx = context.get("universe_scan") or {}
    expects_full_scan = run_depth in ("full", "weekly_market")
    scan_empty = (expects_full_scan and not scan_ctx.get("top_setups")
                  and scan_ctx.get("status") not in ("light", "holdings_watchlist"))
    macro_bad = (context.get("macro_regime") or {}).get("status") != "ok"
    degraded = bool(macro_bad or scan_empty)

    # Coverage is checked on EVERY depth that scans, not only full runs: the four
    # holdings_watchlist slots are the PRIMARY trading slots.
    scans = run_depth in ("full", "weekly_market", "holdings_watchlist")
    requested = scan_ctx.get("requested")
    low_coverage = bool(
        scans and not degraded
        and isinstance(requested, int) and requested > 0
        and scan_ctx.get("scanned", 0) < 0.8 * requested)
    dead_feeds = [k for k in ("news_and_catalysts", "sec_filings", "insider_activity")
                  if not feed_is_alive(context.get(k))]
    return {
        "degraded": degraded, "scan_empty": scan_empty, "macro_bad": macro_bad,
        "low_coverage": low_coverage, "dead_feeds": dead_feeds,
        "partial": (not degraded) and (low_coverage or bool(dead_feeds)),
        "scanned": scan_ctx.get("scanned"), "requested": requested,
    }


def bundle_age_hours(context: dict) -> float | None:
    """Hours since this bundle's DATA was gathered, from its own run_date."""
    raw = (context or {}).get("run_date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        gathered = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if gathered.tzinfo is None:
        gathered = gathered.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - gathered).total_seconds() / 3600.0, 2)


def label_data_quality(context: dict, run_depth: str, *,
                       max_age_hours: float = 4.0) -> dict:
    """Stamp context['data_quality'] for a bundle we are about to ACT on.

    Covers two holes found 2026-07-19:
      * the live trading path never labelled anything (see assess_bundle_health);
      * --act-on never checked the bundle's AGE. Age was computed only in the
        gather-only relay-fallback branch, so a HEALTHY gather set no label, and
        a cloud routine acting on a 10-hour-old bundle passed every freshness
        gate. Live prices are overlaid for holdings/watchlist only — fundamentals,
        news, filings and setups in that bundle are arbitrarily old and were
        labelled fresh.

    Never DOWNGRADES an existing label: a relay/degraded stamp from the gathering
    node is more specific than anything we can infer here.
    """
    existing = context.get("data_quality") or {}
    health = assess_bundle_health(context, run_depth)
    age_h = bundle_age_hours(context)
    stale_by_age = age_h is not None and age_h >= max_age_hours

    if existing.get("source") in ("relay_bundle", "degraded_empty"):
        if stale_by_age and not existing.get("stale"):
            existing["stale"] = True
            existing["age_hours"] = age_h
        return existing

    label: dict = {}
    if health["degraded"]:
        label = {"source": "degraded_empty", "stale": True}
        context["stale_data_notice"] = (
            "Live feeds failed and this context is largely EMPTY. Treat scan/macro "
            "data as missing; do not open new positions on absent data.")
    elif health["partial"]:
        note = ("universe scan covered only "
                f"{health['scanned']}/{health['requested']} names - missing names "
                "have no fresh prices; treat absent setups as unscanned, not "
                "unattractive"
                if health["low_coverage"] else
                f"price scan OK but research feed(s) failed: {health['dead_feeds']}")
        label = {"source": "live_partial", "note": note}
    elif stale_by_age:
        label = {"source": "aged_bundle"}

    if age_h is not None:
        label.setdefault("source", "live")
        label["age_hours"] = age_h
        if stale_by_age:
            label["stale"] = True
            context["stale_data_notice"] = (
                f"This bundle's data was gathered {age_h:.1f}h ago. Live prices are "
                f"overlaid for holdings/watchlist only — fundamentals, news, filings "
                f"and scan setups are that old. Lower confidence and avoid "
                f"time-sensitive entries.")
    if label:
        context["data_quality"] = label
    return label


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
    ap.add_argument("--note", metavar="TEXT",
                    help="operator note injected into the brain context for this run "
                         "(e.g. a thesis or article to react to; annotation only — "
                         "all normal gates still apply)")
    ap.add_argument("--learning-mark", action="store_true",
                    help="dedicated mark job: shadow book + post-exit runners + "
                         "news cache on mark days + lesson prune; no trading")
    ap.add_argument("--study", action="store_true",
                    help="daily study session: research ONE curriculum topic "
                         "(web search) and write a durable lesson into the "
                         "knowledge base + dashboard learning journal; no trading")
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

    if args.study:
        print(f"=== East Equity Agent daily study {run_id} ===")
        from runlib.reviews import daily_study
        return daily_study(run_id)
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
        # Breadcrumb: mark that a trading-ROUTINE run has begun, BEFORE the heavy work.
        # A run recorded nothing durable until log_run_summary at the very end, so a
        # cloud session that died mid-run (context death, gather failure) left zero
        # trace and looked identical to a fire that never happened (2026-07-20). This
        # marks and pushes a run_started record so a death is visible and the watchdog
        # can re-run the slot. GATED so ONLY the cloud routines mark, not the hourly
        # GitHub bundle-refresh Action which also runs `--gather-only --auto-depth`:
        # GitHub always sets GITHUB_ACTIONS, the CCR routine sandbox does not. Entirely
        # fail-open — a breadcrumb must never be the reason a run does not run.
        if _should_mark_run_start(args.auto_depth):
            try:
                import subprocess as _sp
                _sp.run([sys.executable, str(ROOT / "scripts" / "mark_run_start.py")],
                        cwd=str(ROOT), capture_output=True, timeout=180)
            except Exception as e:
                print(f"  (run-start marker skipped: {e})")

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
        # Same assessment the live/act-on paths now use — one implementation so the
        # two cannot drift. Coverage is checked on EVERY depth that scans, not only
        # full runs: the four holdings_watchlist slots are the PRIMARY trading slots.
        _health = assess_bundle_health(context, run_depth)
        scan_empty = _health["scan_empty"]
        degraded = _health["degraded"]
        low_coverage = _health["low_coverage"]
        dead_feeds = _health["dead_feeds"]
        partial = _health["partial"]
        scan_ctx = context.get("universe_scan") or {}
        requested = _health["requested"]
        if dead_feeds:
            print(f"  DATA QUALITY: dead feed(s) {dead_feeds} - run marked partial")
        if low_coverage:
            print(f"  DATA QUALITY: low coverage "
                  f"{scan_ctx.get('scanned', 0)}/{requested} - run marked partial")

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

        # OVERLAY LIVE PRICES INTO THE BUNDLE THE BRAIN ACTUALLY READS (2026-07-23).
        #
        # This branch writes the context the cloud brain reasons from, and it was the
        # ONE path that never overlaid live prices. apply_live_prices() was called only
        # in the --act-on branch (and the local full-run branch) — and --act-on runs
        # AFTER the brain has already decided. So the overlay protected stop
        # enforcement while the brain itself was handed daily bars.
        #
        # In cloud mode that is every decision the system makes: the routine runs
        # gather -> brain -> act, so the brain read the previous session's closes all
        # day. The 2026-07-23 19:51 UTC bundle, built at 3:51pm ET mid-session, carried
        # all 186 tickers at price_as_of 2026-07-22. Given yesterday's price beside
        # today's news the brain reconciles the gap by inventing one: on 07-22 the risk
        # desk vetoed all three BUYs for exactly that, including a fabricated $311 JMP
        # target for DDOG that appears nowhere in the bundle.
        #
        # Placed AFTER the relay-bundle fallback on purpose: when live feeds are
        # blocked and `context = cached` is adopted, that bundle's prices are precisely
        # the ones most in need of refreshing. Fail-soft by construction —
        # apply_live_prices never raises, and it declines the overlay when the snapshot
        # is older than schedule.live_prices.max_age_min rather than clobbering a good
        # daily bar with a broken feed.
        try:
            _lp = apply_live_prices(context, cfg)
            if _lp.get("applied"):
                print(f"  (gather: live prices overlaid onto "
                      f"{len(_lp['applied'])} names, age {_lp.get('age_min')}m)")
            else:
                print(f"  (gather: NO live-price overlay - {_lp.get('status')}, "
                      f"age {_lp.get('age_min')}m; bundle ships daily bars)")
        except Exception as e:
            print(f"  (gather live-price overlay skipped: {e})")

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
        # A halt must stop the system OPENING risk, never stop it CLOSING risk.
        # This returned before apply_safety_layer ran, so flipping the kill switch —
        # or merely exhausting the daily run budget — silently disabled stop
        # enforcement on an open book. scripts/execute_order_intents.py already had
        # this right ("protective exits must never be blocked by a halt"); the main
        # path contradicted it. Protective exits are SELL_TO_CLOSE only and cannot
        # increase exposure, so running them under a halt is strictly risk-reducing.
        try:
            from scripts.stop_watch import run as stop_watch_run
            sw = stop_watch_run()
            if sw.get("fills"):
                print(f"  protective exits honored despite halt: "
                      f"{[f.get('ticker') for f in sw['fills']]}")
            journal.log_run_summary(
                {"halted": halt, "run_depth": run_depth,
                 "protective_exits_under_halt": sw.get("fills") or [],
                 "stop_watch_status": sw.get("status")}, run_id)
        except Exception as e:
            print(f"  (protective-exit sweep failed under halt: {e})")
            journal.log_run_summary(
                {"halted": halt, "run_depth": run_depth,
                 "protective_exit_sweep_error": str(e)}, run_id)
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
        # Age this bundle BEFORE anything acts on it. --act-on used to perform no
        # age check at all: a healthy gather wrote no data_quality label, so the
        # validator's staleness gate asserted nothing and a cloud routine could
        # trade a 10-hour-old bundle with every freshness gate green.
        _age = bundle_age_hours(context)
        if _age is not None:
            print(f"  bundle data gathered {_age:.1f}h ago")
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
                broker.mark_to_market(bundle_prices)
            except Exception as e:
                print(f"  (mark-to-market from bundle failed: {e})")
        context["portfolio"] = get_portfolio_state()
        _dq = label_data_quality(context, run_depth)
        if _dq.get("stale") or _dq.get("source") not in (None, "live"):
            print(f"  DATA QUALITY: {_dq.get('source')} "
                  f"stale={bool(_dq.get('stale'))} age={_dq.get('age_hours')}h "
                  f"- new BUYs restricted")
        if run_safety:
            forced_exit_fills = apply_safety_layer(context, cfg, run_id)
        response = Path(args.act_on).read_text()
        # THE CLOUD ROUTINE *IS* THE BRAIN, and it never touches run_claude.
        # MEASURED 2026-08-03: 0 of 82 cloud runs recorded the brain call they
        # were built around, while risk_desk and daily_study on those same runs
        # logged fine — because those are Python subprocesses that DO go through
        # run_claude, and this path just reads a response the routine reasoned in
        # its own session. That is the whole 82% telemetry gap, and it hid the
        # single most expensive call of every cloud run.
        # log_run_summary reconciles this at end-of-run regardless; recording it
        # here adds the response size and source file, which the reconciler
        # cannot see. Double-counting is impossible: the reconciler skips any run
        # that already carries a brain:* record. Fail-soft — telemetry must never
        # be able to break a trading run.
        try:
            from runlib.brain_io import record_brain_call
            record_brain_call(f"brain:{run_depth}", run_id,
                              transport="cloud_routine",
                              response_chars=len(response),
                              brain_response_file=str(args.act_on))
        except Exception as e:
            print(f"  (cloud brain-call telemetry skipped: {str(e)[:100]})")
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
        if args.note:
            context["operator_note"] = (
                f"OPERATOR NOTE — the human running this agent passed you the "
                f"following to react to. It is an outside view, not a verified "
                f"fact and not an instruction to trade. Weigh it against your own "
                f"research and the portfolio you actually hold; say plainly where "
                f"you agree, where you disagree, and what would change your mind. "
                f"Note:\n\n{args.note}")
        if run_safety:
            forced_exit_fills = apply_safety_layer(context, cfg, run_id)
        # Label the bundle on the LIVE path too. This assessment used to run only
        # under --gather-only, so validator._check_data_quality found no label and
        # returned without asserting anything on every real trading run.
        _dq = label_data_quality(context, run_depth)
        if _dq.get("stale") or _dq.get("source") not in (None, "live"):
            print(f"  DATA QUALITY: {_dq.get('source')} "
                  f"stale={bool(_dq.get('stale'))} age={_dq.get('age_hours')}h")
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
        # Seat reviews are owed on a trading depth whenever the book is non-empty;
        # factor_response is owed whenever factor_map says concentration is high or
        # extreme. Without these four arguments the gates default OFF and the teeth
        # added to process_gates have nothing to bite on — the audit would report
        # process_ok on a run that reviewed no seats and answered no concentration
        # warning. That is precisely the "wired but never invoked" shape this rework
        # exists to remove, so they are passed explicitly.
        held = [str(x.get("ticker") or "").upper()
                for x in ((context.get("portfolio") or {}).get("positions") or [])
                if x.get("ticker")]
        fmap = context.get("factor_map") or {}
        # Names that reached their OWN stated buy level this run, and how many
        # distinct days each has done so without being bought.
        _fired_triggers = [
            str((a or {}).get("ticker") or "").upper()
            for a in ((context.get("watchlist_trigger_alerts") or {}).get("alerts") or [])
            if isinstance(a, dict) and a.get("ticker")]
        _unbought_hits = {}
        try:
            from tools.engagement import count_unbought_hits
            _wo = ROOT / "dashboard" / "data" / "watchlist_outcomes.json"
            if _wo.exists():
                _unbought_hits = count_unbought_hits(json.loads(_wo.read_text()))
        except Exception as e:
            print(f"      (unbought-hit count skipped: {str(e)[:100]})")
        trading_depth = run_depth in ("full", "holdings_watchlist")
        audit = audit_brain_process(
            parsed, depth=run_depth, proposals=proposals,
            top_scan_tickers=top_scan, universe=validator.load_universe(),
            held_tickers=held,
            require_seat_reviews=bool(trading_depth and held),
            require_factor_response=bool(fmap.get("requires_factor_response")),
            factor_concentration_level=fmap.get("concentration_level"),
            # Lets the audit catch an unwind being used as a trading halt rather
            # than the 0.5x size haircut the config actually applies.
            momentum_status=(context.get("momentum_health") or {}).get("status"),
            # ENGAGEMENT. A fired trigger owes a resolution, a name that keeps
            # reaching its level unbought decays off the list, and a book that has
            # been flat for days owes a dated commitment.
            fired_triggers=_fired_triggers, unbought_hits=_unbought_hits,
            requires_commitment=bool(
                (context.get("engagement") or {}).get("requires_commitment")),
            max_unbought_hits=int(
                ((cfg.get("swing_rules") or {}).get("max_unbought_trigger_hits")) or 3))
        parsed["watchlist"] = audit.get("watchlist") or parsed.get("watchlist") or []
        parsed["rejected_ideas"] = audit.get("rejected_ideas") or []
        parsed["seat_reviews"] = audit.get("seat_reviews") or []
        parsed["factor_response"] = audit.get("factor_response")
        parsed["process_audit"] = {
            "process_ok": audit.get("process_ok"),
            "full_run_no_trade_ok": audit.get("full_run_no_trade_ok"),
            "issues": audit.get("issues") or [],
            "blocking_issues": audit.get("blocking_issues") or [],
            "blocks_new_buys": bool(audit.get("blocks_new_buys")),
            # Feeds forward into the next bundle's reasoning_process so a
            # dead-indicator veto or an unwind-as-halt is visible to the run that
            # comes after it, not just to a human reading the journal.
            "signal_discipline": audit.get("signal_discipline") or {},
            "engagement": audit.get("engagement") or {},
        }
        _eng = audit.get("engagement") or {}
        parsed["trigger_reviews"] = _eng.get("trigger_reviews") or []
        if _eng.get("waiting_for"):
            parsed["waiting_for"] = _eng["waiting_for"]
        for _d in (_eng.get("force_dropped") or [])[:5]:
            print(f"      watchlist FORCE-DROPPED {_d.get('ticker')}: reached its "
                  f"level on {_d.get('unbought_hits')} days unbought")
        if _fired_triggers:
            print(f"      triggers fired: {_fired_triggers} "
                  f"(resolutions: {len(_eng.get('trigger_reviews') or [])})")
        _sd = audit.get("signal_discipline") or {}
        if _sd.get("unwind_used_as_halt"):
            print("      SIGNAL DISCIPLINE: unwind cited as a halt on a no-trade "
                  "run — it is a 0.5x size haircut, not a gate")
        for _fix in (_sd.get("trigger_fixes") or [])[:5]:
            print(f"      trigger rewritten {_fix.get('ticker')}: "
                  f"{_fix.get('original')!r} -> {_fix.get('rewritten')!r}")
        for _dv in (_sd.get("dead_indicator_vetoes") or [])[:5]:
            print(f"      dead-indicator veto {_dv.get('ticker')}: "
                  f"{_dv.get('signals')}")
        if audit.get("issues"):
            print(f"      process gates: {audit['issues'][:6]}")
            journal.log_improvement(
                "Process gate: " + "; ".join(audit["issues"][:12]), run_id)
        # TEETH. Risk-relevant gates now BLOCK new BUYs instead of being journaled
        # and forgotten. factor_response is owed when factor_map flags high/extreme
        # concentration; seat_reviews are owed before capital opens a new seat. A
        # run that skips either does not get to add risk this cycle. Exits, holds
        # and trims are never blocked — the asymmetry the whole system runs on.
        if audit.get("blocks_new_buys"):
            blocking = audit.get("blocking_issues") or []
            dropped = [p for p in proposals
                       if str(p.get("action", "")).upper() == "BUY"]
            if dropped:
                print(f"      PROCESS GATE BLOCKS {len(dropped)} BUY(s): {blocking[:4]}")
                for p in dropped:
                    journal.log_rejection(
                        p, [f"process_gate_blocked:{'; '.join(blocking[:6])}"], run_id)
                proposals = [p for p in proposals
                             if str(p.get("action", "")).upper() != "BUY"]
                parsed["proposals"] = proposals
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

    try:  # knowledge-base lessons cited anywhere in the reasoning get credited
        from tools.knowledge_base import record_citations
        n_cited = record_citations(response, run_id)
        if n_cited:
            print(f"      knowledge base: {n_cited} lesson(s) cited this run")
    except Exception as e:
        print(f"      (kb citation scan failed: {e})")

    if parsed.get("guidance_entries"):
        try:
            gsum = record_guidance(parsed["guidance_entries"])
            print(f"      guidance ledger: {gsum['recorded']} recorded, "
                  f"{gsum['replaced']} replaced, {len(gsum['invalid'])} invalid")
        except Exception as e:
            print(f"      (guidance ledger write failed: {e})")

    if parsed.get("universe_candidates"):
        try:
            from runlib.reviews import apply_universe_candidates
            usum = apply_universe_candidates(parsed["universe_candidates"], run_id)
            print(f"      universe candidates: "
                  f"{[a['ticker'] for a in usum.get('accepted', [])]} accepted, "
                  f"{[(r['ticker'], r['why']) for r in usum.get('rejected', [])]} rejected"
                  + (f" (error: {usum['error']})" if usum.get("error") else ""))
        except Exception as e:
            print(f"      (universe candidates failed: {e})")

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
        # FAIL CLOSED. This used to print "failed open" and continue, which meant an
        # unreadable equity_history.json silently disabled BOTH account circuit
        # breakers (-3% daily, -15% drawdown) while the run went on to open new risk.
        # A breaker that skips itself on a read error is not a breaker. A MISSING
        # file is not an error (fresh book) — that path yields [] above.
        risk_halts = [f"risk_halt_check_unverifiable:{e}"]
        print(f"  RISK-HALT CHECK FAILED CLOSED: {e} - new BUYs blocked this run")
    # CAPABILITY AUDIT. Proves every load-bearing guard is wired AND fed on the
    # production path before this run is allowed to open risk. Three days of
    # hardening found the same shape of defect five separate times — code present,
    # unit tests green, production either never invoking it or feeding it an empty
    # map. Unit tests structurally cannot see that; they supply the inputs
    # themselves. A dead capability blocks new BUYs only: exits, holds and stop
    # enforcement are never blocked, because refusing to reduce risk is always the
    # wrong direction.
    try:
        from runlib.capabilities import audit_capabilities
        caps = audit_capabilities()
        context["capabilities"] = caps
        if not caps["ok"]:
            for name in caps["dead"]:
                print(f"  CAPABILITY DEAD: {name} — "
                      f"{caps['results'][name]['detail']}")
            journal.log_improvement(
                "Capability audit FAILED: " + "; ".join(
                    f"{n}: {caps['results'][n]['detail'][:160]}" for n in caps["dead"]),
                run_id)
            risk_halts = list(risk_halts) + [
                f"capability_dead:{','.join(caps['dead'])}"]
            print("  new BUYs blocked until every capability is live again")
    except Exception as e:
        # The audit failing IS a failure. An unverifiable guard and an absent one
        # are the same thing to the position it was meant to protect.
        risk_halts = list(risk_halts) + [f"capability_audit_unavailable:{e}"]
        print(f"  CAPABILITY AUDIT FAILED TO RUN: {e} - new BUYs blocked")
    context["risk_halts"] = risk_halts
    if risk_halts:
        print(f"  RISK HALT ACTIVE: {risk_halts} - new BUYs blocked this run")
        for p in proposals:
            if str(p.get("action", "")).upper() == "BUY":
                journal.log_rejection(p, risk_halts, run_id)
        proposals = [p for p in proposals if str(p.get("action", "")).upper() != "BUY"]
    market_context = build_volatility_context(
        context.get("universe_scan") or {}, context.get("options_signals") or {})
    # Regime signal for the validator's coded gate: SPY-vs-200DMA from the
    # scanner's benchmark read, momentum-factor health from the gauge. Both
    # fail open (missing keys -> gate inactive) so old bundles keep working.
    try:
        bt = (context.get("universe_scan") or {}).get("benchmark_trend") or {}
        above = bt.get("spy_above_200dma")
        mh = context.get("momentum_health") or {}
        market_context["_regime"] = {
            # None (fail-open) when the bundle lacks the read; the gate only
            # fires on an explicit False.
            "spy_below_200dma": (not above) if isinstance(above, bool) else None,
            "momentum_unwind": bool(mh.get("momentum_unwind")),
            "momentum_status": mh.get("status"),
        }
        if market_context["_regime"]["spy_below_200dma"] or mh.get("momentum_unwind"):
            print(f"  REGIME: spy_below_200dma="
                  f"{market_context['_regime']['spy_below_200dma']} "
                  f"momentum={mh.get('status')} - gate/risk-scale active")
    except Exception as e:
        print(f"  (regime context skipped: {e})")
    # Data-quality feed. The bundle has labelled itself stale/degraded since forever
    # and the validator never looked — CLAUDE.md's "HARD RULE" on fundamentals
    # freshness was enforced by nothing. Absent keys mean nothing is asserted.
    try:
        dq = context.get("data_quality") or {}
        fresh = context.get("fundamentals_freshness") or {}
        stale_tickers = fresh.get("stale_tickers") or []
        if dq or stale_tickers:
            market_context["_data_quality"] = {
                "source": dq.get("source"),
                "stale": bool(dq.get("stale")),
                "empty": dq.get("source") == "degraded_empty",
                "age_hours": dq.get("age_hours"),
                "stale_tickers": [str(t).upper() for t in stale_tickers],
            }
            if dq.get("stale") or stale_tickers:
                print(f"  DATA QUALITY GATE: source={dq.get('source')} "
                      f"stale={bool(dq.get('stale'))} "
                      f"stale_tickers={len(stale_tickers)} - new BUYs restricted")
    except Exception as e:
        print(f"  (data-quality context skipped: {e})")
    # Book-risk feed. tools/correlation.py has computed beta and pairwise
    # correlation since day one and NOTHING consumed it — it reached the brain as
    # prose and the validator not at all. These keys are what let the beta ceiling
    # and the gap-correlation term in portfolio heat actually bind.
    try:
        pr = context.get("portfolio_risk") or {}
        betas = {}
        if isinstance(pr, dict) and pr.get("status") == "ok":
            for group in ("holdings", "candidates"):
                for tkr, row in (pr.get(group) or {}).items():
                    b = (row or {}).get("beta")
                    if isinstance(b, (int, float)):
                        betas[str(tkr).upper()] = float(b)
            market_context["_book"] = {
                "portfolio_beta": pr.get("portfolio_beta"),
                "avg_pairwise_corr": pr.get("avg_pairwise_holding_corr"),
                "shared_left_tail": bool(pr.get("shared_left_tail")),
                "betas": betas,
            }
            if pr.get("shared_left_tail"):
                print(f"  BOOK RISK: shared_left_tail=True "
                      f"beta={pr.get('portfolio_beta')} "
                      f"avg_corr={pr.get('avg_pairwise_holding_corr')}")
    except Exception as e:
        # No _book key -> the beta ceiling stands down. Heat's gap term treats absent
        # correlation as fully correlated, so a missing feed cannot BUY a cheaper
        # risk number.
        print(f"  (book-risk context skipped: {e})")
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
    # EARNINGS FEED for the coded blackout gate (2026-08-03). Until now there was
    # NO earnings rule in the validator at all — `days_to_earnings` appeared zero
    # times in validator.py — so "trade around the binary" was pure LLM judgment
    # applied against a 45-day default horizon. In earnings season that excludes
    # essentially the whole universe: the 08-03 runs rejected MPC and AMGN (report
    # 08-04) and then NET and TEAM (08-06) and traded nothing. Replacing the vague
    # judgment with a narrow, checkable window is the point — the brain is told the
    # gate is 3 days and that anything outside it is explicitly tradeable, so it
    # stops inventing a wider exclusion than the risk actually warrants.
    #
    # earnings_week is authoritative (committed calendar, all ~184 names); scan-row
    # days_to_earnings covers only the ~30 rate-limited enrichment names and is used
    # only as a fallback. Absent ticker -> no assertion, gate stands down.
    try:
        from tools.earnings_window import build_earnings_map
        # data/earnings_calendar.json carries `last` AND `next` per name, so it
        # is the only source that can populate days_since_earnings — earnings_week
        # is a 7-day FORWARD window and structurally cannot see a print that has
        # already happened. Without it the post-print drift lane, which is the
        # preferred way to trade a reporter, would be undetectable.
        _cal = None
        try:
            _cal = (json.loads((ROOT / "data" / "earnings_calendar.json")
                               .read_text()) or {}).get("by_ticker")
        except Exception:
            _cal = None
        em = build_earnings_map(context, calendar=_cal,
                                today=to_et_date(datetime.now(timezone.utc).isoformat()))
        if em:
            market_context["_earnings"] = em
            soon = {t: v["days_to_earnings"] for t, v in em.items()
                    if isinstance(v.get("days_to_earnings"), int)
                    and v["days_to_earnings"] <= 3}
            if soon:
                print(f"  EARNINGS GATE: {len(em)} names dated, "
                      f"{len(soon)} inside the pre-print blackout {soon}")
    except Exception as e:
        print(f"  (earnings context skipped: {str(e)[:120]})")
    # The bundle rides along under a reserved key (same convention as _regime /
    # _data_quality / _book above) so _check_claim_grounding can verify that the
    # facts a thesis cites actually appear in the evidence the brain was handed.
    # market_context is otherwise a per-ticker volatility map and carries no news.
    market_context["_bundle"] = context
    results = validator.validate_proposals(proposals, live_portfolio, market_context)
    # Risk-desk vetoes never reached the validator, but they belong on the
    # public site: the brain's commentary may reference the killed trade, so
    # the proposals list must show it as rejected with the desk's grounds.
    from runlib import brain_io as _bio
    if _bio.LAST_RISK_DESK_VETOES:
        results = list(_bio.LAST_RISK_DESK_VETOES) + list(results)
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

    try:  # link KB lessons cited in an executed BUY's proposal to that trade,
        # so the eventual closed-trade outcome grades the lesson (system 6).
        # citations_for_trade, NOT CITATION_RE over the proposal JSON. MEASURED
        # 2026-08-03: `times_cited` works (65 citations across 11 lessons) because
        # record_citations scans the WHOLE brain response, but this path scanned
        # only the proposal object — and ZERO of the 19 proposals in
        # journal/proposals/ contains a KB- id. Every citation lands in prose
        # (latest_reasoning, watchlist[].thoughts, no_trade_reason,
        # rejected_ideas[].reason). So linked_trades stayed [] on all 11 lessons,
        # update_lesson_outcomes never had an input, and no lesson ever earned an
        # evidence_status. The grading half of the loop was reading the wrong text.
        from tools.knowledge_base import citations_for_trade, link_lessons_to_trade
        by_ticker = {str(p.get("ticker", "")).upper(): p
                     for r in results if r.approved
                     for p in [r.proposal]
                     if str(p.get("action", "")).upper() == "BUY"}
        for f in fills:
            t = str(f.get("ticker", "")).upper()
            if f.get("status") != "filled" or str(f.get("action", "")).upper() != "BUY":
                continue
            prop = by_ticker.get(t)
            if not prop:
                continue
            ids = citations_for_trade(prop, response)
            # trade_ref makes the link idempotent on (ticker, run_id, trade_ref):
            # blind appending double-counted on manual re-runs and on scale-in adds
            # (the validator permits 2 per position), which could push a lesson past
            # the 3-linked-trade grading threshold on the strength of ONE real trade.
            if ids and link_lessons_to_trade(
                    t, ids, run_id, linked_on=et_date(),
                    trade_ref=f.get("order_id") or f.get("proposal_id")):
                print(f"      kb: linked {ids} to {t} entry for outcome grading")
    except Exception as e:
        print(f"      (kb trade-link failed: {e})")

    print("[5/5] Journaling + dashboard + X draft...")
    publish_prices = (context.get("universe_scan") or {}).get("prices") or {}
    if publish_prices:
        try:
            broker.mark_to_market(publish_prices)
        except Exception as e:
            print(f"  (mark-to-market failed: {e})")
    context["portfolio"] = get_portfolio_state()
    refresh_dashboard(context, response, results, fills, run_id, no_trade_reason,
                      parsed["commentary"], parsed["watchlist"],
                      parsed.get("rejected_ideas"))
    draft_x_summary(fills, results, context, run_id, parsed.get("x_post"))
    journal.log_run_summary({
        "manual": args.manual,
        "trigger_run": args.trigger_run or None,
        "operator_note": args.note or None,
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
