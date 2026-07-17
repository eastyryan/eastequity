"""Context gathering for East Equity runs."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import journal
import validator
from execution import broker, corporate_actions, exit_guard
from tools.macro_regime import get_macro_snapshot
from tools.news_catalysts import get_news_and_catalysts
from tools.portfolio_state import get_portfolio_state
from tools.position_history import get_position_histories
from tools.watchlist_triggers import (TRIGGER_TOLERANCE_PCT, check_watchlist_triggers,
                                      parse_price_level)
from tools.insider_form4 import get_insider_activity
from tools.performance_breakdown import build_performance_breakdown
from tools.fundamentals import get_deep_fundamentals
from tools.filing_text import get_mdna_excerpt, get_latest_earnings_release
from tools.guidance_ledger import auto_grade, get_ledger_summary, record_guidance
from tools.options_signal import get_options_signals
from tools.sec_filings import get_filing_brief
from tools.smart_money_13f import get_smart_money
from tools.universe_scanner import scan_universe
from runlib.core import ROOT, et_date, et_now, to_et_date, json_safe, light_prices
from runlib.depths import depth_description, depth_allows_new_buys, focus_ticker_budget
from runlib.analytics import (
    build_volatility_context, build_stop_engineering, build_position_stop_cushion,
    compute_closed_trades, compute_performance_stats, compute_calibration,
    recent_improvements, benchmark_close,
)

def universe_audit_summary() -> dict | None:
    """Compact summary of the latest ALL-universe freshness audit for the bundle.
    None when no audit exists or it is too old to trust (>8 days)."""
    f = ROOT / "dashboard" / "data" / "freshness_audit.json"
    try:
        a = json.loads(f.read_text())
        audited_at = a.get("audited_at_et", "")
        age_days = None
        try:
            age_days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(audited_at)).days
        except Exception:
            pass
        if age_days is not None and age_days > 8:
            return None
        return {"audited_at_et": audited_at, "audited": a.get("audited"),
                "fresh": a.get("fresh"), "stale_tickers": a.get("stale_tickers", []),
                "error_tickers": a.get("error_tickers", []),
                "foreign_annual_filers": a.get("foreign_annual_filers", [])}
    except Exception:
        return None

def prev_watchlist() -> list[dict]:
    prev_file = ROOT / "dashboard" / "data" / "latest.json"
    if not prev_file.exists():
        return []
    try:
        return json.loads(prev_file.read_text()).get("watchlist") or []
    except Exception:
        return []


def stack_cards_for_focus(focus: list[str]) -> dict:
    try:
        from tools.stack_cards import build_stack_cards
        return build_stack_cards(focus)
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "by_ticker": {}}


def financial_checklists_for_focus(focus: list[str]) -> dict:
    try:
        from tools.financial_checklists import build_financial_checklists
        return build_financial_checklists(focus)
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "by_ticker": {}}


def concept_memory_for_focus(focus: list[str]) -> dict:
    try:
        from tools.concept_memory import (
            brain_facing_concept_memory, update_many_from_focus,
        )
        # Refresh memory from latest stack/checklist before serving
        try:
            from tools.stack_cards import build_stack_cards
            from tools.financial_checklists import build_financial_checklists
            update_many_from_focus(
                focus,
                stack_cards=build_stack_cards(focus),
                checklists=build_financial_checklists(focus),
            )
        except Exception:
            pass
        return brain_facing_concept_memory(focus)
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "by_ticker": {}}


def brain_reasoning_bundle(portfolio: dict, depth: str,
                            scan: dict | None = None) -> dict:
    """Watchlist opportunity cost, exit lessons, theme map, process checklist."""
    out: dict = {"run_depth": depth}
    try:
        from tools.research_feedback import (
            brain_facing_process_checklist,
            brain_facing_watchlist_feedback,
        )
        out["process_checklist"] = brain_facing_process_checklist()
        out["watchlist_feedback"] = brain_facing_watchlist_feedback()
    except Exception as e:
        out["watchlist_feedback"] = {"status": "error", "reason": str(e)[:150]}
    try:
        from tools.exit_autopsy import brain_facing_exit_lessons
        out["exit_lessons"] = brain_facing_exit_lessons()
    except Exception as e:
        out["exit_lessons"] = {"status": "error", "reason": str(e)[:150]}
    try:
        from tools.demand_drivers import build_driver_map, theme_exposure
        dmap = build_driver_map()
        out["demand_driver_map_note"] = (
            "Primary economic bet per ticker (not GICS). Use these labels in "
            "proposal.demand_driver. Theme cap is separate from sector cap.")
        out["demand_driver_map"] = dmap
        out["theme_exposure"] = theme_exposure(
            portfolio.get("positions") or [], dmap,
            equity=portfolio.get("total_equity_usd"))
        try:
            cap = validator.load_config().get("theme_risk", {}).get(
                "max_demand_driver_concentration_pct", 0.35)
            out["theme_concentration_cap_pct"] = cap
        except Exception:
            out["theme_concentration_cap_pct"] = 0.35
    except Exception as e:
        out["demand_driver_map"] = {"status": "error", "reason": str(e)[:150]}
    pf: dict = {
        "bundle_as_of_et": et_date(),
        "bundle_run_utc": datetime.now(timezone.utc).isoformat(),
        "note": "Prefer universe_scan.prices_meta[T].price_as_of (bar session date) "
                "over bundle age alone. On catalyst days, verify live before chasing "
                "a trigger on a lagging close.",
    }
    if isinstance(scan, dict):
        pf["scan_fetched_at_utc"] = scan.get("scan_fetched_at_utc")
        meta = scan.get("prices_meta") or {}
        if isinstance(meta, dict) and meta:
            pf["prices_meta_sample"] = {t: meta[t] for t in list(meta)[:12]}
            pf["n_priced"] = len(meta)
            today = et_date()
            pf["bars_older_than_et_today"] = sum(
                1 for v in meta.values()
                if isinstance(v, dict) and v.get("price_as_of")
                and str(v.get("price_as_of"))[:10] < today)
    out["price_freshness"] = pf
    out["watchlist_status_required"] = ["drop", "hold", "buy"]
    out["full_run_no_trade_rule"] = (
        "On full depth with empty proposals, include rejected_ideas: "
        "[{ticker, reason}, ...] with at least 2 scan ideas you passed on.")
    # --- Self-improvement loops ---
    try:
        from tools.shadow_portfolio import brain_facing_shadow_learning
        out["shadow_learning"] = brain_facing_shadow_learning()
    except Exception as e:
        out["shadow_learning"] = {"status": "error", "reason": str(e)[:120]}
    try:
        from tools.learning_adopt import brain_facing_adopted_lessons
        out["adopted_lessons"] = brain_facing_adopted_lessons()
    except Exception as e:
        out["adopted_lessons"] = {"status": "error", "reason": str(e)[:120]}
    try:
        from tools.calibration_gate import brain_facing_calibration_status
        out["calibration_status"] = brain_facing_calibration_status()
    except Exception as e:
        out["calibration_status"] = {"status": "error", "reason": str(e)[:120]}
    try:
        from tools.knowledge_base import brain_facing_knowledge_base
        out["knowledge_base"] = brain_facing_knowledge_base()
    except Exception as e:
        out["knowledge_base"] = {"status": "error", "reason": str(e)[:120]}
    return out


def fetch_market_news() -> dict:
    try:
        from tools.market_news import get_market_news
        return get_market_news()
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150]}


def fetch_universe_8ks() -> dict:
    try:
        from tools.filings_sweep import today_8ks
        return today_8ks(sorted(validator.load_universe()))
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "filers": []}


def tape_and_8k_promotions(focus: list[str], market_news: dict | None,
                            todays_8ks: dict | None, max_extra: int,
                            market_events: dict | None = None) -> tuple[list[str], list[dict]]:
    """Universe names mentioned on the tape or filing 8-Ks, not already in focus."""
    if max_extra <= 0:
        return [], []
    try:
        from tools.tape_focus import promote_focus_from_tape
        headlines = []
        if isinstance(market_news, dict):
            headlines.extend(market_news.get("headlines") or [])
        if isinstance(market_events, dict):
            for h in (market_events.get("flagged_headlines") or []):
                if isinstance(h, dict) and h.get("title"):
                    headlines.append(h)
        add, recs = promote_focus_from_tape(
            focus,
            headlines=headlines,
            todays_8ks=todays_8ks,
            universe=validator.load_universe(),
            max_total_extra=max_extra,
        )
        return add, recs
    except Exception as e:
        print(f"  (tape/8k promote skipped: {e})")
        return [], []


def deep_research_bundle(focus: list[str]) -> tuple[dict, dict, dict]:
    """Filings + deep fundamentals + filing prose for focus names (quarterly-cached)."""
    filings = {t: get_filing_brief(t) for t in focus}
    deep_fundamentals, filing_texts = {}, {}
    cache_dir = ROOT / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    reused, refreshed = [], []
    for t in focus:
        filed_key = ((filings.get(t) or {}).get("latest_periodic_filing") or {}).get("filed", "")
        cache_f = cache_dir / f"quarterly_{t.upper()}.json"
        cached = None
        try:
            if filed_key and cache_f.exists():
                blob = json.loads(cache_f.read_text())
                if blob.get("filed_key") == filed_key:
                    cached = blob
        except Exception:
            cached = None
        if cached:
            deep_fundamentals[t] = cached["deep"]
            if cached.get("texts") is not None:
                filing_texts[t] = cached["texts"]
            reused.append(t)
        else:
            deep_fundamentals[t] = get_deep_fundamentals(t)
            texts = None
            if len(filing_texts) < 5:
                texts = {"mdna": get_mdna_excerpt(t, max_chars=10000),
                         "earnings_release": get_latest_earnings_release(t, max_chars=8000)}
                filing_texts[t] = texts
            refreshed.append(t)
            try:
                if filed_key and (deep_fundamentals[t] or {}).get("status") == "ok":
                    cache_f.write_text(json.dumps(json_safe(
                        {"filed_key": filed_key, "cached_at": et_date(),
                         "deep": deep_fundamentals[t], "texts": texts}), default=str))
            except Exception:
                pass
    print(f"    quarterly cache: {len(reused)} reused, {len(refreshed)} refreshed"
          + (f" ({refreshed})" if refreshed else ""))
    return filings, deep_fundamentals, filing_texts


def gather_context(cfg: dict, light: bool = False, depth: str | None = None,
                   earnings_trigger: dict | None = None) -> dict:
    """Build the brain's context bundle.

    depth (preferred) selects how much work to do:
      light | holdings_watchlist | full | weekly_market | evening_review
    `light=True` is kept for backward compatibility (== depth light).

    earnings_trigger (optional): when a universe name reported earnings this
    slot, the orchestrator escalates depth to full and passes the trigger here;
    its reporters are force-promoted into deep-research focus and surfaced in the
    bundle so the brain prioritizes them.
    """
    if depth is None:
        depth = "light" if light else "full"
    budget = focus_ticker_budget(depth)
    print(f"  • run depth: {depth} — {depth_description(depth)}")

    # Reporters that forced this deep dive (filtered to the universe).
    universe_set = validator.load_universe()
    earnings_reporters = [
        t for t in
        dict.fromkeys(str(x).upper() for x in ((earnings_trigger or {}).get("reporters") or []) if x)
        if t in universe_set
    ]
    if earnings_reporters:
        print(f"  • earnings deep-dive reporters forced into focus: {earnings_reporters}")

    print("  • macro regime...")
    macro = get_macro_snapshot()
    print("  • portfolio state...")
    portfolio = get_portfolio_state()

    held = [p["ticker"] for p in portfolio.get("positions", [])]
    prior_watchlist = prev_watchlist()
    watch = [w.get("ticker") for w in prior_watchlist if w.get("ticker")]
    watch = [t for t in watch if t]

    # Market-wide events (oil/VIX + geopolitical headline flags) — cheap, every depth.
    print("  • market events (commodities + geo flags)...")
    try:
        from tools.market_events import get_market_events
        market_events = get_market_events()
    except Exception as e:
        market_events = {"status": "error", "reason": str(e)[:150],
                         "note": "market_events module unavailable"}

    discovery_block: dict = {"status": "skipped", "note": f"not run at depth={depth}"}
    market_radar_block: dict = {"status": "skipped", "note": f"not run at depth={depth}"}
    # Fetched early on trading depths so tape/8-K names can join the deep set
    # before expensive research. Reused later when building the context bundle.
    market_news: dict | None = None
    todays_8ks: dict | None = None
    tape_promotions: list[dict] = []

    if depth == "light":
        tickers = list(dict.fromkeys(held + watch))
        print(f"  • light price check on {tickers}...")
        scan = {"status": "light", "note": "light run - no universe scan this cycle",
                "top_setups": [], "prices": light_prices(tickers),
                "atr_by_ticker": {}}
        focus = list(dict.fromkeys(held))
        print(f"  • news for holdings {focus}...")
        filings = {t: {"status": "skipped_light_run"} for t in focus}
        smart_money = {"status": "skipped_light_run"}
        insiders = {"status": "skipped_light_run"}
        partnerships = {"status": "skipped_light_run"}
        news = get_news_and_catalysts(focus) if focus else {"status": "skipped"}
        deep_fundamentals = {t: {"status": "skipped_light_run"} for t in focus}
        filing_texts = {}
        guidance = get_ledger_summary(held)
        options_signals = get_options_signals(held) if held else {"status": "skipped"}
        from tools.fundamental_screen import get_screen
        fundamental_screen = get_screen(None)
    elif depth == "holdings_watchlist":
        # Fast path: mini-scan holdings + watchlist only (no 180-name download).
        focus_seed = list(dict.fromkeys(held + watch))
        print(f"  • mini-scan holdings+watchlist ({len(focus_seed)} names)...")
        try:
            # Always pass an explicit list (even empty) so we never fall through
            # to a full-universe download on a focused depth.
            scan = scan_universe(top_n=max(len(focus_seed), 5),
                                 tickers=list(focus_seed),
                                 enrich=False)
            scan["status"] = scan.get("status") or "ok"
            scan["note"] = (
                "holdings_watchlist depth: scanned only held + watchlist names "
                "(not the full universe). New BUYs must still be in data/universe.json."
            )
        except TypeError:
            # Older scanner without tickers=/enrich= — fall back to light prices.
            scan = {"status": "holdings_watchlist", "top_setups": [],
                    "prices": light_prices(focus_seed), "atr_by_ticker": {},
                    "note": "scanner lacks mini-scan API; used last closes only"}
        except Exception as e:
            scan = {"status": "error", "top_setups": [],
                    "prices": light_prices(focus_seed), "atr_by_ticker": {},
                    "note": f"mini-scan failed ({e}); used last closes only"}

        # Prioritize watchlist trigger alerts into deep research.
        early_prices = scan.get("prices") or {}
        alerts = check_watchlist_triggers(prior_watchlist, early_prices)
        alert_tickers = [a["ticker"] for a in alerts if a.get("ticker")]
        focus = list(dict.fromkeys(held + watch + alert_tickers))

        # Balance: cheap universe radar (tape headlines + 8-K index) BEFORE deep
        # research so a mid-day catalyst on a non-watch name can still be underwritten.
        print("  • market-wide news (early, for tape promote)...")
        market_news = fetch_market_news()
        if budget.get("filings_sweep"):
            print("  • universe 8-K sweep (early, for promote)...")
            todays_8ks = fetch_universe_8ks()
        add, tape_promotions = tape_and_8k_promotions(
            focus, market_news, todays_8ks,
            int(budget.get("tape_promote_max") or 5),
            market_events=market_events)
        if add:
            print(f"  • tape/8-K promoted into deep focus: {add}")
            focus = list(dict.fromkeys(focus + add))
            # Price promoted names so marks/triggers/validator have a reference.
            for t, px in light_prices(add).items():
                scan.setdefault("prices", {})[t] = px

        print(f"  • deep research on holdings+watchlist+tape {focus}...")
        try:
            from tools.price_chart import render_charts
            render_charts(focus)
        except Exception as e:
            print(f"    (chart rendering failed: {e})")
        filings, deep_fundamentals, filing_texts = deep_research_bundle(focus)
        try:
            from tools.fundamental_screen import get_screen
            fundamental_screen = get_screen(None)
        except Exception as e:
            fundamental_screen = {"status": "error", "reason": str(e)[:200]}
        options_signals = get_options_signals(focus) if focus else {"status": "skipped"}
        for t in focus:
            try:
                auto_grade(t)
            except Exception as e:
                print(f"    (auto_grade {t} failed: {e})")
        guidance = get_ledger_summary(focus)
        smart_money = get_smart_money(focus) if focus else {"status": "skipped"}
        news = get_news_and_catalysts(focus) if focus else {"status": "skipped"}
        insiders = get_insider_activity(focus) if focus else {"status": "skipped"}
        try:
            from tools.partnerships import get_partnerships
            partnerships = get_partnerships(focus) if focus else {"status": "skipped"}
        except Exception as e:
            partnerships = {"status": "error", "reason": str(e)[:150]}
    else:
        # full | weekly_market | evening_review (evening still gets full context)
        weekly = bool(budget.get("weekly"))
        print("  • universe scan" + (" (weekly breadth)..." if weekly else "..."))
        try:
            scan = scan_universe(
                top_n=15 if not weekly else 20,
                enrich=bool(budget.get("enrich_scan", True)),
                weekly=weekly,
            )
        except TypeError:
            scan = scan_universe(top_n=15 if not weekly else 20)

        candidates = [r["ticker"] for r in scan.get("top_setups", [])[: budget["top_setups"]]]
        focus = list(dict.fromkeys(held + candidates))

        contrarian_picks = [r["ticker"] for r in scan.get("contrarian_setups", [])[: budget["contrarian"]]]
        focus = list(dict.fromkeys(focus + contrarian_picks))
        deep_value_picks = [r["ticker"] for r in scan.get("deep_value_200w", [])[: budget["deep_value"]]]
        focus = list(dict.fromkeys(focus + deep_value_picks))
        if budget.get("supplier_pullbacks"):
            sp = [r["ticker"] for r in scan.get("supplier_pullbacks", [])[: budget["supplier_pullbacks"]]]
            focus = list(dict.fromkeys(focus + sp))

        # Promote non-focus fat pitches (post-earnings drift, strong revisions, etc.).
        promote_n = int(budget.get("promote_extra") or 0)
        if promote_n > 0:
            try:
                from tools.universe_scanner import promote_focus_candidates
                extra = promote_focus_candidates(scan, focus, max_extra=promote_n)
            except Exception:
                extra = []
            if extra:
                print(f"  • promoted fat-pitch names into deep bundle: {extra}")
                focus = list(dict.fromkeys(focus + extra))

        # Weekly: discovery sweep + a few standout names outside the book.
        if weekly:
            print("  • discovery sweep (broad market)...")
            try:
                from tools.discovery_screen import run_discovery
                discovery_block = run_discovery()
            except Exception as e:
                discovery_block = {"status": "error", "reason": str(e)[:150]}
            tops = []
            for row in (discovery_block.get("top_candidates") or [])[: budget.get("discovery_deep", 5)]:
                t = row.get("ticker")
                if t and t not in focus:
                    tops.append(t)
            if tops:
                print(f"  • weekly standouts for deep research: {tops}")
                focus = list(dict.fromkeys(focus + tops))
            # Always include watchlist on weekly so triggers get underwritten.
            focus = list(dict.fromkeys(focus + watch))

        # Balance: tape + 8-K promotions join the deep set on full/weekly too
        # (cheap radar → expensive underwrite only for hits).
        print("  • market-wide news (early, for tape promote)...")
        market_news = fetch_market_news()
        print("  • market radar (Alpaca movers/actives/news, market-wide)...")
        try:
            from tools.market_radar import get_market_radar
            market_radar_block = get_market_radar()
        except Exception as e:
            market_radar_block = {"status": "error", "reason": str(e)[:150]}
        if budget.get("filings_sweep"):
            print("  • universe 8-K sweep (early, for promote)...")
            todays_8ks = fetch_universe_8ks()
        add, tape_promotions = tape_and_8k_promotions(
            focus, market_news, todays_8ks,
            int(budget.get("tape_promote_max") or 5),
            market_events=market_events)
        if add:
            print(f"  • tape/8-K promoted into deep focus: {add}")
            focus = list(dict.fromkeys(focus + add))
            for t, px in light_prices(add).items():
                scan.setdefault("prices", {})[t] = px

        # Earnings reporters that forced this full run always get deep research,
        # even if they did not rank into the scan's top setups (foreign filers /
        # off-momentum names would otherwise be scanned but not underwritten).
        earn_new = [t for t in earnings_reporters if t not in focus]
        if earn_new:
            print(f"  • earnings reporters promoted into deep focus: {earn_new}")
            focus = list(dict.fromkeys(focus + earn_new))
            for t, px in light_prices(earn_new).items():
                scan.setdefault("prices", {})[t] = px

        print("  • rendering candlestick charts...")
        try:
            from tools.price_chart import render_charts
            render_charts(focus)
        except Exception as e:
            print(f"    (chart rendering failed: {e})")

        print(f"  • deep research on {focus}...")
        filings, deep_fundamentals, filing_texts = deep_research_bundle(focus)

        print("  • fundamental screen (full universe, cached)...")
        try:
            from tools.fundamental_screen import get_screen
            all_universe = sorted(validator.load_universe())
            fundamental_screen = get_screen(all_universe)
        except Exception as e:
            fundamental_screen = {"status": "error", "reason": str(e)[:200]}
        print("  • options-derived signals...")
        options_signals = get_options_signals(focus)
        print("  • guidance ledger...")
        for t in focus:
            try:
                auto_grade(t)
            except Exception as e:
                print(f"    (auto_grade {t} failed: {e})")
        guidance = get_ledger_summary(focus)
        smart_money = get_smart_money(focus) if focus else {"status": "skipped"}
        news = get_news_and_catalysts(focus) if focus else {"status": "skipped"}
        insiders = get_insider_activity(focus) if focus else {"status": "skipped"}
        print("  • strategic partnerships / material deals...")
        try:
            from tools.partnerships import get_partnerships
            partnerships = get_partnerships(focus) if focus else {"status": "skipped"}
        except Exception as e:
            partnerships = {"status": "error", "reason": str(e)[:150]}

    # Held names that dropped out of the universe scan (e.g. removed in a weekly review)
    # must STILL be priced, or mark_to_market and the safety layer freeze them at entry
    # cost - which quietly flatters losers that fell out of the momentum funnel.
    scan_prices = scan.setdefault("prices", {})
    missing_held = [t for t in held if t not in scan_prices]
    if missing_held:
        for t, px in light_prices(missing_held).items():
            scan_prices[t] = px

    # Mark the book to today's prices EVERY run, not just when a trade fills.
    # Without this, hold days freeze last_price/equity at the last fill, so the
    # brain reasons on stale stop cushions and the published equity flat-lines.
    if held and scan_prices:
        try:
            broker.mark_to_market(scan_prices)
            portfolio = get_portfolio_state()
        except Exception as e:
            print(f"  (mark-to-market failed: {e})")

    # Momentum-factor health (cheap, every depth): are momentum LEADERS being
    # sold while the index holds up (July-2026-style factor unwind)? Uses this
    # run's scan rows when a real scan happened (light runs pass none) plus a
    # cached MTUM/SPMO-vs-SPY read. Fail-soft: never breaks a gather.
    print("  • momentum-factor health...")
    try:
        from tools.momentum_health import get_momentum_health
        _mh_rows = ((scan.get("top_setups") or [])
                    + (scan.get("contrarian_setups") or [])
                    + (scan.get("deep_value_200w") or [])
                    + (scan.get("supplier_pullbacks") or []))
        momentum_health = get_momentum_health(_mh_rows or None)
    except Exception as e:
        momentum_health = {"status": "unknown", "momentum_unwind": False,
                           "signals": {}, "as_of": None,
                           "note": f"momentum_health unavailable ({str(e)[:120]})"}

    print("  • position histories...")
    histories = get_position_histories(held) if held else {"status": "skipped"}

    print("  • watchlist triggers...")
    watchlist_alerts = check_watchlist_triggers(prior_watchlist, scan.get("prices", {}))

    # Market-wide tape + 8-K: often already fetched early for promotions.
    if market_news is None:
        print("  • market-wide news...")
        market_news = fetch_market_news()
    if todays_8ks is None:
        if budget.get("filings_sweep"):
            print("  • universe 8-K sweep...")
            todays_8ks = fetch_universe_8ks()
        else:
            todays_8ks = {"status": f"skipped_{depth}_run", "filers": []}

    # Portfolio correlation/beta: quantifies the book's common left tail and each
    # top candidate's diversification value BEFORE the brain reasons about adds.
    print("  • portfolio correlation/beta...")
    try:
        from tools.correlation import get_portfolio_risk
        _cand = [r.get("ticker") for r in (scan.get("top_setups") or [])[:5]
                 if r.get("ticker") and r.get("ticker") not in held]
        portfolio_risk = get_portfolio_risk(
            [{"ticker": p["ticker"], "market_value_usd": p.get("market_value_usd", 0)}
             for p in portfolio.get("positions", [])],
            candidates=_cand)
    except Exception as e:
        portfolio_risk = {"status": "error", "reason": str(e)[:150]}

    # The agent's own track record: what it predicted vs. what actually happened.
    closed = compute_closed_trades()
    hist_file = ROOT / "dashboard" / "data" / "equity_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    track_record = {
        "note": "Your own past trades. Study what worked and what did not before proposing.",
        "closed_trades": closed[-20:],
        "performance": compute_performance_stats(closed, hist),
        "breakdowns": build_performance_breakdown(closed),
        "calibration": compute_calibration(closed),
    }

    # Fundamentals freshness: per focus name, does our extracted data reach the
    # period covered by the newest filed 10-Q/10-K? Derived from the briefs already
    # fetched (no extra network). stale=true names must never be cited as current.
    fundamentals_freshness = {}
    for t, br in (filings or {}).items():
        if isinstance(br, dict) and br.get("status") == "ok":
            fundamentals_freshness[t] = {
                "current_through": br.get("fundamentals_current_through"),
                "latest_filing_period": (br.get("latest_periodic_filing") or {}).get("period_end"),
                "stale": bool(br.get("stale_fundamentals_warning")),
            }
    stale_names = sorted(t for t, v in fundamentals_freshness.items() if v["stale"])
    if stale_names:
        print(f"  !! STALE FUNDAMENTALS for {stale_names} - flagged to the brain")

    # PER-NAME DIGEST: the financial identity card the brain reads FIRST. Key numbers
    # from the statements (always labeled with their quarter-end - the backbone never
    # goes out of view even though deep re-fetches are quarterly), plus everything
    # that changes daily. Full detail remains below in the bundle for digging.
    def _dig(t: str) -> dict:
        br = filings.get(t) if isinstance(filings.get(t), dict) else {}
        qf = br.get("quarterly_fundamentals") or {}
        rev = (qf.get("revenue") or [])
        ni = (qf.get("net_income") or [])
        ratios = ((deep_fundamentals.get(t) or {}).get("quality_ratios") or {}).get("ratios") or {}

        def rv(key):
            v = ratios.get(key)
            return v.get("value") if isinstance(v, dict) else v
        row = next((r for r in (scan.get("top_setups") or []) + (scan.get("contrarian_setups") or [])
                    + (scan.get("deep_value_200w") or []) + (scan.get("supplier_pullbacks") or [])
                    if r.get("ticker") == t), {})
        yoy = None
        if len(rev) >= 5 and rev[-5].get("value_usd"):
            yoy = round((rev[-1]["value_usd"] / rev[-5]["value_usd"] - 1) * 100, 1)
        d = {
            "latest_quarter_end": rev[-1]["period_end"] if rev else None,  # CITE THIS DATE
            "revenue_usd": rev[-1]["value_usd"] if rev else None,
            "revenue_yoy_pct": yoy,
            "net_income_usd": ni[-1]["value_usd"] if ni else None,
            "ttm_ebitda_usd": rv("ebitda_ttm_usd"),
            "net_debt_to_ebitda": rv("net_debt_to_ebitda"),
            "fcf_ttm_usd": rv("free_cash_flow_ttm_usd"),
            "sbc_pct_of_revenue": rv("sbc_pct_of_revenue"),
            "market_cap_usd": rv("market_cap_usd") or row.get("market_cap_usd"),
            "fundamentals_stale": bool(br.get("stale_fundamentals_warning")),
            "next_earnings": (news.get("tickers", {}).get(t) or {}).get("next_earnings")
                             if isinstance(news, dict) else None,
            "days_to_earnings": row.get("days_to_earnings"),
            # daily-fresh layer
            "last_close": row.get("last_close"),
            "rel_strength_1m_pct": row.get("rel_strength_1m_pct"),
            "rsi_14": row.get("rsi_14"),
            "macd_state": (row.get("macd") or {}).get("state"),
            "volume_read": (row.get("volume_signal") or {}).get("read"),
            "ai_exposure": row.get("ai_exposure"),
            "insider_signal": ((insiders.get("tickers", {}).get(t) or {}).get("summary") or {}).get("signal")
                              if isinstance(insiders, dict) else None,
            # Echo the buyer count so "insider_buying" vs a real 2+ "bullish_cluster"
            # is unambiguous in the digest itself (no drill-down needed).
            "insider_distinct_buyers": ((insiders.get("tickers", {}).get(t) or {}).get("summary") or {}).get("distinct_discretionary_buyers")
                              if isinstance(insiders, dict) else None,
            "13f_net_activity": ((smart_money.get("by_ticker") or {}).get(t) or {}).get("net_activity")
                                if isinstance(smart_money, dict) else None,
            "deal_8ks_recent": len(((partnerships.get("tickers", {}).get(t) or {})
                                    .get("material_agreement_8ks") or []))
                               if isinstance(partnerships, dict) else None,
        }
        return {k: v for k, v in d.items() if v is not None}
    digest = {t: _dig(t) for t in focus}

    # Volatility -> stop engineering. One source of truth (validator.stop_floor_pct)
    # for both the brain-facing floors and the deterministic validation at execute time.
    volatility = build_volatility_context(scan, options_signals)
    stop_engineering = build_stop_engineering(focus, volatility, cfg)
    position_stop_cushion = build_position_stop_cushion(portfolio, volatility, cfg)

    # Weekly market check-in artifact for the dashboard (sector leaders + discovery).
    market_checkin = None
    if depth == "weekly_market":
        market_checkin = {
            "as_of_et": et_date(),
            "note": "Weekly multi-sector market check-in. Not a trade list — a "
                    "breadth map. Deep research was limited to holdings + standouts.",
            "benchmark_trend": scan.get("benchmark_trend"),
            "sector_relative_strength": scan.get("sector_relative_strength"),
            "by_sector_leaders": scan.get("by_sector_leaders"),
            "top_setups": (scan.get("top_setups") or [])[:15],
            "discovery": {
                "status": discovery_block.get("status"),
                "top_candidates": (discovery_block.get("top_candidates") or [])[:15],
                "scanned": discovery_block.get("scanned"),
            },
            "focus_deep_researched": focus,
        }
        try:
            out = ROOT / "dashboard" / "data" / "market_checkin.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(json_safe(market_checkin), indent=2, default=str))
            print(f"  • published market_checkin.json ({len(focus)} deep names)")
        except Exception as e:
            print(f"  (market_checkin publish failed: {e})")

    return {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "as_of_et": et_date(),  # today's US MARKET date - cite this, not the UTC run_date
        "digest": {
            "note": "READ FIRST: per focus name, the key statement numbers (each with its "
                    "quarter-end date - cite them WITH the date), leverage/cash-flow reads, "
                    "next earnings (when fresh statements arrive), and today's fast-moving "
                    "signals. Full detail lives in the sections below; dig there before "
                    "proposing, but this card is the always-current financial identity.",
            "by_ticker": digest,
        },
        "trading_mode": cfg["mode"]["trading_mode"],
        "run_depth": depth,
        "run_depth_note": depth_description(depth),
        "allows_new_buys": depth_allows_new_buys(depth),
        "market_events": {
            "note": "Oil/VIX/commodities moves + keyword-flagged geopolitical and "
                    "macro headlines. Weigh in the regime read; never the sole thesis.",
            **(market_events if isinstance(market_events, dict) else {}),
        },
        "momentum_health": momentum_health,
        "market_radar": {
            "note": "Market-WIDE movers/most-actives/news-trending (Alpaca screeners), "
                    "NOT limited to the universe. Off-universe names with a genuine "
                    "swing setup can be proposed via the universe_candidates output "
                    "field (they become tradeable next run after deterministic gates); "
                    "a big percent move alone is never a thesis.",
            **(market_radar_block if isinstance(market_radar_block, dict) else {}),
        },
        "discovery_screen": discovery_block if depth == "weekly_market" else {"status": "skipped"},
        "market_checkin": market_checkin,
        # Hard limits the validator will enforce - size within them or be rejected.
        "hard_limits": {
            "effective_max_position_usd": round(min(
                cfg["position_sizing"]["max_position_usd"],
                portfolio.get("total_equity_usd", 0)
                * cfg["position_sizing"]["max_position_pct_of_portfolio"]), 2),
            "position_sizing": cfg["position_sizing"],
            "quality": cfg["trade_quality_requirements"],
            "swing": {k: cfg["swing_rules"][k] for k in
                      ("min_holding_horizon_days", "max_holding_horizon_days",
                       "max_new_positions_per_day")},
        },
        "benchmark_close": benchmark_close(),
        "macro_regime": macro,
        "portfolio": portfolio,
        "position_histories": {
            "note": "Last 10 daily sessions for each CURRENT HOLDING, newest last, "
                    "with 5-day change and distance from the 10-day high/low. Use "
                    "this to judge whether a position is resting, breaking down, or "
                    "extended - not just today's print vs the entry plan.",
            **histories,
        },
        "watchlist_trigger_alerts": {
            "note": "Deterministic check of YOUR OWN previous run's watchlist "
                    "'would_buy_at' price levels against today's prices. Each alert "
                    "means a name you said you wanted cheaper is now at or within 2% "
                    "of your stated level - prioritize deep research on it this run "
                    "and either propose or update the watchlist entry. Non-price "
                    "triggers (earnings dates, conditions) are not checked here.",
            "alerts": watchlist_alerts,
        },
        "tape_focus_promotions": {
            "note": "Universe tickers auto-promoted into THIS run's deep-research "
                    "focus because they appeared in market-wide headlines and/or "
                    "filed an 8-K on the EDGAR daily index. Cap keeps cost bounded. "
                    "Still subject to universe + validator for any BUY. Not the same "
                    "as the published watchlist (that is Claude's ranked shortlist).",
            "promotions": tape_promotions,
            "promoted_tickers": [p.get("ticker") for p in tape_promotions],
        },
        "earnings_deep_dive": (
            {
                **{k: v for k, v in (earnings_trigger or {}).items() if k != "reporters"},
                "reporters": earnings_reporters,
                "note": (earnings_trigger or {}).get("note")
                or ("Universe names that reported earnings and forced this full deep "
                    "dive (9am ET = overnight/pre-market, 5:30pm ET = afternoon "
                    "after-hours). Promoted into deep-research focus; BUY still needs "
                    "the validator and fat-pitch bar."),
            }
            if earnings_reporters else
            {"reporters": [], "note": "No universe earnings reporter forced a deep "
                                      "dive this slot."}
        ),
        "reasoning_process": brain_reasoning_bundle(portfolio, depth, scan=scan),
        "stack_cards": stack_cards_for_focus(focus),
        "financial_checklists": financial_checklists_for_focus(focus),
        "concept_memory": concept_memory_for_focus(focus),
        "universe_scan": scan,
        "sec_filings": filings,
        "fundamentals_freshness": {
            "note": "Per focus name: the newest period our XBRL fundamentals reach vs the "
                    "period the latest filed 10-Q/10-K actually covers. stale=true means "
                    "the fundamentals below are NOT the latest reported quarter - do NOT "
                    "cite them as current; flag the staleness and lean on price/news. "
                    "universe_audit summarizes the weekly ALL-names sweep.",
            "by_ticker": fundamentals_freshness,
            "stale_tickers": stale_names,
            "universe_audit": universe_audit_summary(),
        },
        "deep_fundamentals": deep_fundamentals,
        "filing_texts": filing_texts,
        "filing_texts_note": (
            "Real MD&A and press-release prose per focus ticker. When "
            "earnings_release.contains_guidance_language is true, QUOTE the specific "
            "guidance sentence (numbers and fiscal period) instead of paraphrasing. "
            "mdna.section == 'document_start' means general filing text, not MD&A."),
        "guidance_ledger": guidance,
        "fundamental_screen": fundamental_screen,
        "options_signals": options_signals,
        "stop_engineering": stop_engineering,
        "position_stop_cushion": {
            "note": "For each open position: room left between today's price and your "
                    "recorded stop, measured in the name's own ATR. cushion_in_atr < 1 "
                    "(inside_noise_band true) means one ordinary session could hit the "
                    "stop - decide deliberately (hold through it, or exit on your terms) "
                    "rather than get noise-stopped. The safety layer still enforces the "
                    "recorded stop on a closing basis.",
            "positions": position_stop_cushion,
        },
        "smart_money_13f": smart_money,
        "news_and_catalysts": news,
        "market_news": {
            "note": "Market-wide headlines (last ~24h) for tape context - what is "
                    "moving EVERYTHING today, not just your focus names. Weigh it in "
                    "the macro/regime read; never build a single-name thesis on it alone.",
            **(market_news if isinstance(market_news, dict) else {}),
        },
        "todays_8ks": {
            "note": "8-K filings TODAY across the ENTIRE universe (one EDGAR index "
                    "sweep). An 8-K on a non-focus name may be the day's real "
                    "opportunity - pull the filing with WebFetch if the form looks "
                    "material before dismissing it.",
            **(todays_8ks if isinstance(todays_8ks, dict) else {}),
        },
        "portfolio_risk": {
            "note": "Correlation/beta math for the CURRENT book + top candidates. "
                    "shared_left_tail=true means every holding falls together in the "
                    "same shock - a new BUY >0.7 correlated to the book must justify "
                    "why it deserves to exist beyond its solo merits; a candidate "
                    "flagged diversifier=true is worth extra attention when the book "
                    "is clustered.",
            **(portfolio_risk if isinstance(portfolio_risk, dict) else {}),
        },
        "insider_activity": insiders,
        "partnerships": partnerships,
        "track_record": track_record,
        # THE LOOP, forward half: the weekly self-review's lessons reach every
        # trading run, so "the one change I'm making" actually changes behavior.
        "lessons_learned": {
            "note": "Your own most recent self-review conclusions and process notes. "
                    "The CURRENT stated behavior change is binding until the next "
                    "review grades it - honor it in this run's decisions.",
            "recent": [n for n in recent_improvements(40)
                       if str(n.get("note", "")).startswith("Weekly self-review:")
                       or "[style-log]" in str(n.get("note", ""))][:5],
        },
    }
