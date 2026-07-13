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
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

import journal
import validator
from execution import corporate_actions, exit_guard, simulated_broker
from tools.macro_regime import get_macro_snapshot
from tools.news_catalysts import get_news_and_catalysts
from tools.portfolio_state import get_portfolio_state
from tools.position_history import get_position_histories
from tools.watchlist_triggers import check_watchlist_triggers
from tools.insider_form4 import get_insider_activity
from tools.performance_breakdown import build_performance_breakdown
from tools.fundamentals import get_deep_fundamentals
from tools.filing_text import get_mdna_excerpt, get_latest_earnings_release
from tools.guidance_ledger import auto_grade, get_ledger_summary, record_guidance
from tools.options_signal import get_options_signals
from tools.sec_filings import get_filing_brief
from tools.smart_money_13f import get_smart_money
from tools.universe_scanner import scan_universe

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


# ---------------------------------------------------------------------------
# Step 1 — Safety preflight
# ---------------------------------------------------------------------------
LOCK_FILE = ROOT / "state" / "RUN_LOCK"
LOCK_STALE_SECONDS = 45 * 60


def acquire_run_lock(run_id: str) -> bool:
    """One run at a time: concurrent cycles trade on stale portfolio snapshots."""
    if LOCK_FILE.exists():
        age = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            return False
        print(f"  (clearing stale run lock, {age/60:.0f} min old)")
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(run_id)
    import atexit
    atexit.register(release_run_lock)  # releases on every exit path, crashes included
    return True


def release_run_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def _node_id() -> str:
    """Best-effort stable id for this execution node (cloud sandbox vs a Mac)."""
    import socket
    if os.environ.get("EE_NODE"):
        return os.environ["EE_NODE"]
    try:
        return socket.gethostname() or "unknown-node"
    except Exception:
        return "unknown-node"


def _read_remote_lease() -> dict | None:
    """The lease as committed on origin/main (the shared source of truth). Fail-open:
    returns None on any git/parse error so a lease can never brick the trader."""
    try:
        subprocess.run(["git", "fetch", "origin", "main"], cwd=ROOT,
                       capture_output=True, timeout=60)
        r = subprocess.run(["git", "show", "origin/main:state/RUN_LEASE.json"],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception as e:
        print(f"  (cross-node lease read failed, proceeding fail-open: {e})")
    return None


def acquire_cross_node_lease(run_id: str, cfg: dict, manual: bool = False) -> str | None:
    """Advisory cross-node lease over the shared ledger, arbitrated through git.

    Returns a halt reason if another NODE holds an unexpired lease (scheduled runs
    stand down; manual runs only warn), else None after claiming the lease. Fail-OPEN
    on any git/parse error - the lease only prevents the clear double-trade case, it
    must never prevent trading because of an infra hiccup."""
    rc = cfg.get("risk_controls", {})
    if not rc.get("cross_node_lease_enabled", True):
        return None
    ttl_min = rc.get("cross_node_lease_ttl_minutes", 30)
    now = datetime.now(timezone.utc)
    lease_path = ROOT / "state" / "RUN_LEASE.json"

    def _conflict(lease: dict | None) -> str | None:
        if not lease:
            return None
        try:
            exp = datetime.fromisoformat(lease["expires_at"])
        except Exception:
            return None  # unparseable -> fail open
        if exp > now and lease.get("run_id") not in (None, run_id) \
                and lease.get("holder") != _node_id():
            return (f"cross-node lease held by {lease.get('holder')} "
                    f"(run {lease.get('run_id')}) until {exp.isoformat()}")

    conflict = _conflict(_read_remote_lease())
    if conflict:
        if manual:
            print(f"  WARNING: {conflict} - proceeding anyway (manual run).")
        else:
            return f"{conflict} - standing down to avoid double-trading the ledger"

    # Claim the lease and push it so the other node can see it before it trades.
    lease = {"holder": _node_id(), "run_id": run_id, "acquired_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=ttl_min)).isoformat()}
    try:
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(json.dumps(lease, indent=2))
        subprocess.run(["git", "add", "state/RUN_LEASE.json"], cwd=ROOT, capture_output=True)
        c = subprocess.run(["git", "commit", "-m", "Acquire run lease [vercel skip]"],
                           cwd=ROOT, capture_output=True, text=True)
        if c.returncode == 0:
            p = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                               capture_output=True, text=True, timeout=120)
            if p.returncode != 0:  # lost the push race - rebase and re-check the holder
                rb = subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=120)
                if rb.returncode != 0:
                    subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, capture_output=True)
                    print("  (lease push race, rebase failed - proceeding fail-open)")
                    return None
                conflict = _conflict(_read_remote_lease())
                if conflict and not manual:
                    return f"lost the lease race: {conflict} - standing down"
                subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                               capture_output=True, timeout=120)
    except Exception as e:
        print(f"  (cross-node lease claim failed, proceeding fail-open: {e})")
    return None


def preflight(cfg: dict, run_id: str, news_only: bool = False,
              manual: bool = False) -> str | None:
    """Return a halt reason string, or None if clear to proceed."""
    if validator.kill_switch_active(cfg):
        return "KILL_SWITCH file present — no runs until it is removed"
    if not news_only and datetime.now().strftime("%a") not in cfg["schedule"]["run_days"]:
        return "not a configured run day (weekend/holiday guard)"
    # Usage budget: hard cap on SCHEDULED runs per day so the automation can never
    # drain the subscription's usage pool. Manual (user-initiated) runs are marked
    # in the journal and neither count toward nor are blocked by the budget.
    if not manual:
        cap = cfg["schedule"].get("max_completed_runs_per_day", 12)
        runs_file = ROOT / "journal" / "runs" / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl"
        if runs_file.exists():
            completed = 0
            for line in runs_file.read_text().splitlines():
                rec = json.loads(line)
                if "halted" not in rec and not rec.get("manual"):
                    completed += 1
            if completed >= cap:
                return f"daily run budget exhausted ({completed}/{cap}) - protecting usage limits"
    if not acquire_run_lock(run_id):
        return "another run is already in progress (RUN_LOCK held)"
    # Cross-node lease: a scheduled run that trades the shared ledger stands down if
    # another node (cloud vs local) already holds it. Skipped for news-only (never trades).
    if not news_only:
        lease_halt = acquire_cross_node_lease(run_id, cfg, manual=manual)
        if lease_halt:
            return lease_halt
    return None


# ---------------------------------------------------------------------------
# Steps 2 — Context gathering (all deterministic Python, all fail-soft)
# ---------------------------------------------------------------------------
def _light_prices(tickers: list) -> dict:
    """Last closes for a small ticker list without a full universe scan."""
    try:
        import yfinance as yf
        data = yf.download(tickers, period="5d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
        out = {}
        for t in tickers:
            try:
                df = data[t].dropna() if len(tickers) > 1 else data.dropna()
                out[t] = round(float(df["Close"].iloc[-1]), 2)
            except Exception:
                continue
        return out
    except Exception:
        return {}


def _json_safe(obj):
    """Recursively replace NaN/Infinity floats with None so every bundle we write
    is STRICT JSON. yfinance/pandas leak NaN into estimate/valuation fields, and a
    bare NaN token makes cloud_context.json unparseable for the cloud routine that
    depends on it. Cheap insurance applied at every bundle/dashboard write."""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _universe_audit_summary() -> dict | None:
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


def build_volatility_context(scan: dict, options_signals: dict) -> dict:
    """Per-ticker volatility for the deterministic stop floor: {TICKER: {atr_pct,
    expected_move_pct}}. ATR comes from the scanner (every name; also present in the
    relayed cloud bundle); expected move from options when that data loaded. This is
    the single map fed to BOTH the validator and the brain-facing stop_engineering
    block, so what the brain is told matches what the validator enforces."""
    vol: dict[str, dict] = {}
    scan = scan or {}
    # ATR for the whole scanned universe (new field), with a fallback to the
    # per-row atr on top_setups/contrarian for bundles gathered before that field.
    for t, atr in (scan.get("atr_by_ticker") or {}).items():
        vol.setdefault(t.upper(), {})["atr_pct"] = atr
    for r in (scan.get("top_setups") or []) + (scan.get("contrarian_setups") or []):
        t = str(r.get("ticker", "")).upper()
        if t and r.get("atr_pct") is not None:
            vol.setdefault(t, {}).setdefault("atr_pct", r["atr_pct"])
    for t, sig in ((options_signals or {}).get("tickers") or {}).items():
        if isinstance(sig, dict) and sig.get("expected_move_pct") is not None:
            vol.setdefault(t.upper(), {})["expected_move_pct"] = sig["expected_move_pct"]
    return vol


def build_stop_engineering(focus: list, vol: dict, cfg: dict) -> dict:
    """Brain-facing: the enforced minimum stop distance per focus name, so the agent
    engineers stops OUTSIDE the noise band on the first try instead of being rejected."""
    floors = {}
    for t in focus:
        t = str(t).upper()
        v = vol.get(t)
        if not v:
            continue
        floor = validator.stop_floor_pct(v.get("atr_pct"), v.get("expected_move_pct"), cfg)
        if floor is None:
            continue
        floors[t] = {
            "atr_pct": v.get("atr_pct"),
            "expected_move_pct": v.get("expected_move_pct"),
            "min_stop_distance_pct": round(floor * 100, 2),
            "tradeable": floor <= cfg["trade_quality_requirements"]["max_stop_loss_distance_pct"],
        }
    return {
        "note": "ENFORCED stop floor per name. Your stop_loss must sit at least "
                "min_stop_distance_pct below entry or the validator rejects it "
                "(stop_inside_noise_band). This is a floor, not a target - for a swing "
                "hold, aim WIDER (roughly 1.5-2x ATR, or clearly beyond the expected "
                "move) so ordinary volatility does not stop you out. If tradeable is "
                "false the name is too volatile for a valid stop under the 15% cap; do "
                "not propose it.",
        "floors": floors,
    }


def build_position_stop_cushion(portfolio: dict, vol: dict, cfg: dict) -> dict:
    """Per open position: how much room is left between today's price and the recorded
    stop, measured in the name's own volatility. A cushion under ~1 ATR means an
    ordinary session could trip the stop - the brain should decide deliberately
    (hold through, or exit on its own terms) rather than be noise-stopped."""
    out = {}
    for pos in portfolio.get("positions", []):
        t = str(pos.get("ticker", "")).upper()
        plan = pos.get("original_plan") or {}
        stop = plan.get("stop_loss")
        last = pos.get("last_price")
        v = vol.get(t) or {}
        atr = v.get("atr_pct")
        if not (stop and last):
            continue
        try:
            stop, last = float(stop), float(last)
        except (TypeError, ValueError):
            continue
        cushion_pct = (last - stop) / last * 100 if last else None
        entry = plan.get("entry_price_max") or pos.get("avg_cost")
        info = {
            "last_price": round(last, 2),
            "recorded_stop": round(stop, 2),
            "cushion_to_stop_pct": round(cushion_pct, 2) if cushion_pct is not None else None,
            "atr_pct": atr,
            "expected_move_pct": v.get("expected_move_pct"),
        }
        if atr and cushion_pct is not None and atr > 0:
            info["cushion_in_atr"] = round(cushion_pct / atr, 2)
            info["inside_noise_band"] = cushion_pct < atr  # < ~1 average day's range
        if entry:
            try:
                info["stop_distance_from_entry_pct"] = round((float(entry) - stop) / float(entry) * 100, 2)
            except (TypeError, ValueError):
                pass
        # Stall detection (soft time stop, forces a DECISION not an exit): two weeks
        # in with the price going nowhere is unpriced opportunity cost - the brain
        # must justify continuing to hold or rotate. The horizon force-close
        # remains the hard time stop.
        days_held = pos.get("days_held")
        avg_cost = pos.get("avg_cost")
        try:
            if days_held is not None and avg_cost:
                pnl_pct = (last / float(avg_cost) - 1) * 100
                info["unrealized_pnl_pct"] = round(pnl_pct, 2)
                info["stalled"] = bool(days_held >= 14 and abs(pnl_pct) < 3.0)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        out[t] = info
    return out


def gather_context(cfg: dict, light: bool = False) -> dict:
    print("  • macro regime...")
    macro = get_macro_snapshot()
    print("  • portfolio state...")
    portfolio = get_portfolio_state()

    held = [p["ticker"] for p in portfolio.get("positions", [])]
    if light:
        # Light check: no universe scan or deep research. Prices for held +
        # watchlist names only (needed for exits, marks, and trigger checks).
        latest_file = ROOT / "dashboard" / "data" / "latest.json"
        watch = []
        if latest_file.exists():
            watch = [w.get("ticker") for w in
                     json.loads(latest_file.read_text()).get("watchlist", []) if w.get("ticker")]
        tickers = list(dict.fromkeys(held + watch))
        print(f"  • light price check on {tickers}...")
        scan = {"status": "light", "note": "light run - no universe scan this cycle",
                "top_setups": [], "prices": _light_prices(tickers)}
        focus = held
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
        fundamental_screen = get_screen(None)  # cached only, never refreshes
    else:
        print("  • universe scan...")
        scan = scan_universe(top_n=15)

        # Deep research on: current holdings + top-5 scanner candidates.
        candidates = [r["ticker"] for r in scan.get("top_setups", [])[:5]]
        focus = list(dict.fromkeys(held + candidates))

        contrarian_picks = [r["ticker"] for r in scan.get("contrarian_setups", [])[:3]]
        focus = list(dict.fromkeys(focus + contrarian_picks))

        # Deep-value lane: strong names at/below their 200-WEEK MA get the full
        # deep-research treatment too - the whole point is vetting whether the
        # business is genuinely sound while the price sits on long-term support.
        deep_value_picks = [r["ticker"] for r in scan.get("deep_value_200w", [])[:2]]
        focus = list(dict.fromkeys(focus + deep_value_picks))

        print("  • rendering candlestick charts...")
        try:
            from tools.price_chart import render_charts
            render_charts(focus)
        except Exception as e:
            print(f"    (chart rendering failed: {e})")

        print(f"  • deep research on {focus}...")
        filings = {t: get_filing_brief(t) for t in focus}
        print("  • deep fundamentals + quality ratios...")
        deep_fundamentals = {t: get_deep_fundamentals(t) for t in focus}
        print("  • filing prose (MD&A + earnings releases)...")
        filing_texts = {t: {"mdna": get_mdna_excerpt(t, max_chars=10000),
                            "earnings_release": get_latest_earnings_release(t, max_chars=8000)}
                        for t in focus[:5]}
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
        for t, px in _light_prices(missing_held).items():
            scan_prices[t] = px

    print("  • position histories...")
    histories = get_position_histories(held) if held else {"status": "skipped"}

    print("  • watchlist triggers...")
    prev_file = ROOT / "dashboard" / "data" / "latest.json"
    prev_watchlist = []
    if prev_file.exists():
        try:
            prev_watchlist = json.loads(prev_file.read_text()).get("watchlist") or []
        except Exception:
            pass
    watchlist_alerts = check_watchlist_triggers(prev_watchlist, scan.get("prices", {}))

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

    # Volatility -> stop engineering. One source of truth (validator.stop_floor_pct)
    # for both the brain-facing floors and the deterministic validation at execute time.
    volatility = build_volatility_context(scan, options_signals)
    stop_engineering = build_stop_engineering(focus, volatility, cfg)
    position_stop_cushion = build_position_stop_cushion(portfolio, volatility, cfg)

    return {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "as_of_et": _et_date(),  # today's US MARKET date - cite this, not the UTC run_date
        "trading_mode": cfg["mode"]["trading_mode"],
        "run_depth": "light" if light else "full",
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
        "benchmark_close": _benchmark_close(),
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
            "universe_audit": _universe_audit_summary(),
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
        "insider_activity": insiders,
        "partnerships": partnerships,
        "track_record": track_record,
    }


# ---------------------------------------------------------------------------
# Safety layer: deterministic corporate actions + forced exits, BEFORE the brain
# ---------------------------------------------------------------------------
def apply_safety_layer(context: dict, cfg: dict, run_id: str) -> list[dict]:
    """Dividends/splits applied, then stops/horizons enforced - the brain can
    never rationalize holding through its own written plan. Returns forced fills."""
    if cfg["mode"]["trading_mode"] == "dry_run":
        return []
    ca = corporate_actions.apply_corporate_actions()
    if ca.get("events") or ca.get("errors"):
        context["corporate_actions"] = ca
        context["portfolio"] = get_portfolio_state()
        if ca.get("errors"):
            print(f"  corporate-actions errors: {ca['errors']}")

    prices = context["universe_scan"].get("prices", {})
    # ATR per name lets the broker model stop gap-through (a stop rarely fills exactly
    # at its level; price gaps beyond it). Same map the volatility-stop floor uses.
    atr_map = context["universe_scan"].get("atr_by_ticker") or {}
    forced = exit_guard.check_forced_exits(context["portfolio"], prices, atr_map)
    if not forced:
        return []
    fills = exit_guard.execute_forced_exits(forced, prices, run_id, atr_map)
    context["portfolio"] = get_portfolio_state()  # brain must see post-exit state
    context["forced_exits"] = [
        {**ex, "note": "Closed deterministically by the safety layer before this run; "
                       "do not re-propose selling it. Explain the exit in your commentary."}
        for ex in forced
    ]
    for f in fills:
        print(f"  FORCED EXIT {f['ticker']} @ {f.get('fill_price')} "
              f"({context['forced_exits'][0].get('reason', '?')})")
    return fills


# ---------------------------------------------------------------------------
# Step 3 — Wake the brain (single Claude invocation via Claude Code CLI)
# ---------------------------------------------------------------------------
def ask_claude(context: dict, run_id: str, news_only: bool = False) -> str:
    """Invoke Claude Code headlessly with CLAUDE.md rules + the context bundle."""
    context_file = ROOT / "state" / f"context_{run_id}.json"
    context_file.write_text(json.dumps(_json_safe(context), indent=2, default=str))

    if not news_only and context.get("run_depth") == "light":
        prompt = (
            "You are running a LIGHT East Equity Agent check (pre-market/overnight slot). "
            f"Read CLAUDE.md, then the context bundle at {context_file}. No universe scan "
            "was done this cycle. Review each open position against its original plan and "
            "the latest news; you MAY propose SELL_TO_CLOSE if a thesis has broken, but "
            "propose NO new BUYs this run (they will be discarded). Refresh your commentary "
            "and carry the watchlist forward (update thoughts only where news changed them). "
            "Output the exact JSON block per CLAUDE.md. End with an 'Improvement note:' line."
        )
        return _run_claude(prompt)

    if news_only:
        prompt = (
            "You are running a scheduled East Equity Agent NEWS REVIEW (markets are closed - "
            f"no trading this run). Read CLAUDE.md, then the context bundle at {context_file}. "
            "Review weekend/overnight news for current holdings and the whole universe: "
            "earnings reports, guidance changes, analyst moves, macro developments. Update "
            "your view of each holding and each watchlist name. Output the JSON block from "
            "CLAUDE.md with proposals REQUIRED to be an empty list, no_trade_reason set to "
            "'news review - markets closed', fresh commentary (lead with anything that "
            "changes Monday's plan), and an updated watchlist. End with an "
            "'Improvement note:' line."
        )
        return _run_claude(prompt)

    prompt = (
        f"Today's date in the US market timezone (ET) is {_et_date()} - use THIS date in "
        f"any prose, never the UTC run_date. "
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
    return _run_claude(prompt)


def _run_claude(prompt: str) -> str:
    import time
    last_err = ""
    # NOTE: no --permission-mode plan. Newer Claude Code (2.x) diverts a plan-mode
    # answer to a plan file and returns only a prose summary on stdout, so our JSON
    # block never appears and parsing falls back to a no-trade. The default headless
    # mode prints the full response (incl. the fenced ```json) to stdout, which is what
    # we parse - matching universe_review(), which already runs claude -p without plan mode.
    for attempt in (1, 2):  # one retry: overnight runs can hit transient/usage errors
        result = subprocess.run(
            ["claude", "-p", prompt],
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
# Adversarial risk desk: an independent skeptic tries to kill every BUY.
# ---------------------------------------------------------------------------
def adversarial_review(proposals: list[dict], context_file: str, run_id: str) -> list[dict]:
    """Second pass by a separate Claude session prompted to REFUTE each BUY.
    Veto drops the proposal (journaled); survivors may get a confidence haircut.
    Skipped where the claude CLI is unavailable (cloud sandboxes) - noted loudly."""
    import shutil
    buys = [p for p in proposals if str(p.get("action", "")).upper() == "BUY"]
    if not buys:
        return proposals
    if shutil.which("claude") is None:
        print("  (risk desk unavailable in this environment - proposals pass unreviewed)")
        return proposals
    prompt = (
        "You are the RISK DESK for a paper-trading fund. Another analyst proposed the "
        f"trades in this JSON: {json.dumps(buys)}. The full market context is at "
        f"{context_file} - read it. Your job is to try to KILL each trade: attack the "
        "thesis with the data (valuation, estimate revisions, insider selling, earnings "
        "timing, macro, entry geometry, portfolio concentration vs existing holdings). "
        "Be a skeptic, not a contrarian for sport - approve genuinely sound trades. "
        "Output ONLY a ```json block: {\"reviews\": [{\"ticker\": \"X\", "
        "\"verdict\": \"approve\"|\"veto\", \"objection\": \"one paragraph\", "
        "\"confidence_adjustment\": 0.0}]} where confidence_adjustment is 0 or negative "
        "(max -0.10) for approved-with-reservations."
    )
    try:
        out = _run_claude(prompt)
    except Exception as e:
        print(f"  risk desk failed ({e}) - proposals pass unreviewed")
        return proposals
    reviews = {}
    for block in reversed(re.findall(r"```json\s*(.*?)```", out, re.DOTALL)):
        try:
            data = json.loads(block)
            if "reviews" in data:
                reviews = {r["ticker"].upper(): r for r in data["reviews"]}
                break
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    if not reviews:
        print("  risk desk output unparsable - proposals pass unreviewed")
        return proposals
    kept = []
    for p in proposals:
        r = reviews.get(str(p.get("ticker", "")).upper())
        if r is None or str(p.get("action", "")).upper() != "BUY":
            kept.append(p)
            continue
        if r.get("verdict") == "veto":
            print(f"  RISK DESK VETO {p['ticker']}: {r.get('objection', '')[:120]}")
            journal.log_rejection(p, [f"risk_desk_veto: {r.get('objection', '')[:300]}"], run_id)
            continue
        adj = min(float(r.get("confidence_adjustment", 0) or 0), 0)
        if adj:
            p["confidence"] = round(max(p.get("confidence", 0) + adj, 0), 2)
            p["risk_desk_note"] = r.get("objection", "")[:300]
            print(f"  risk desk haircut {p['ticker']}: {adj} -> {p['confidence']}")
        kept.append(p)
    return kept


# ---------------------------------------------------------------------------
# Step 4 — Parse structured proposals out of the brain's response
# ---------------------------------------------------------------------------
def parse_proposals(response: str) -> dict:
    """Extract the structured output block: proposals, commentary, watchlist. Accepts a
    fenced ```json block or, as a fallback, a bare JSON object containing a 'proposals'
    key (so a run still parses if the model omits the fence)."""
    candidates = re.findall(r"```json\s*(.*?)```", response, re.DOTALL)
    # Fallback: any brace-balanced object mentioning "proposals", fence or not.
    for m in re.finditer(r'\{[^{}]*"proposals"[\s\S]*?\}\s*$', response):
        candidates.append(m.group(0))
    for block in reversed(candidates):  # last valid block wins
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "proposals" in data:
                return {
                    "proposals": data["proposals"],
                    "no_trade_reason": data.get("no_trade_reason"),
                    "commentary": data.get("commentary"),
                    "watchlist": (data.get("watchlist") or [])[:10],
                    "x_post": data.get("x_post"),
                    "guidance_entries": (data.get("guidance_entries") or [])[:20],
                }
        except json.JSONDecodeError:
            continue
    # Graceful public-facing fallback (this text can surface on the dashboard).
    return {"proposals": [],
            "no_trade_reason": "No trade this run; the agent's written review did not include a "
                               "machine-readable order block, so no orders were placed.",
            "commentary": None, "watchlist": [], "x_post": None, "guidance_entries": []}


# ---------------------------------------------------------------------------
# Step 6 — Execution (approved proposals only; broker readback required)
# ---------------------------------------------------------------------------
def execute(approved: list[validator.ValidationResult], context: dict,
            cfg: dict, run_id: str) -> list[dict]:
    fills = []
    prices = context["universe_scan"].get("prices", {})
    atr_map = context["universe_scan"].get("atr_by_ticker") or {}
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
            # ATR lets the broker vol-scale slippage and model an entry gap (a $400 name
            # swinging 7%/day costs more to fill than a flat 10bps implies).
            "atr_pct": atr_map.get(p["ticker"].upper()),
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
    except Exception as e:
        print(f"  WARNING: benchmark fetch failed ({e}) - S&P comparison will show a gap")
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
            # pnl_usd stays price-only (net of fees) for trade GRADING; total_pnl_usd adds
            # attributed dividends for honest RETURN. Dividends already hit cash when paid,
            # so never sum both into the same total (see compute_performance_stats).
            price_pnl = fill.get("realized_pnl_usd")
            divs = fill.get("dividends_received_usd") or 0.0
            total_pnl = fill.get("total_realized_pnl_usd")
            if total_pnl is None:
                total_pnl = (price_pnl or 0.0) + divs
            closed.append({
                "ticker": t, "entry_price": entry, "exit_price": exit_px,
                "opened_at": entry_fill["filled_at"][:10], "closed_at": fill["filled_at"][:10],
                "days_held": days_held, "pnl_usd": price_pnl,
                "dividends_usd": round(divs, 2), "total_pnl_usd": round(total_pnl, 2),
                "fees_usd": fill.get("fees_usd"), "gap_modeled": fill.get("gap_modeled", False),
                "r_multiple": r_multiple, "verdict": verdict,
                "confidence": plan.get("confidence"),
                "thesis": plan.get("thesis"),
            })
    return closed


def compute_performance_stats(closed: list[dict], equity_hist: list[dict]) -> dict | None:
    if not closed:
        return None
    pnls = [t["pnl_usd"] or 0 for t in closed]          # price-only, net of fees
    total_pnls = [t.get("total_pnl_usd", t["pnl_usd"] or 0) or 0 for t in closed]  # + dividends
    wins = [p for p in pnls if p > 0]
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    fees = sum((t.get("fees_usd") or {}).get(k, 0) or 0
               for t in closed for k in ("commission", "sec_fee", "taf"))
    peak, max_dd = 0.0, 0.0
    for h in equity_hist:
        peak = max(peak, h["equity"])
        if peak:
            max_dd = max(max_dd, (peak - h["equity"]) / peak)
    return {
        "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
        "realized_pnl_usd": round(sum(pnls), 2),               # price-only
        "realized_pnl_incl_dividends_usd": round(sum(total_pnls), 2),
        "total_fees_paid_usd": round(fees, 2),                 # cost drag, shown for honesty
        "avg_r_multiple": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_days_held": round(sum(t["days_held"] for t in closed) / len(closed), 1),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }


def compute_calibration(closed: list[dict]) -> dict | None:
    """Are the brain's stated confidences honest? Bucket closed trades by the confidence
    it claimed at entry and compare to the realized win rate. Directly feeds the CLAUDE.md
    rule 'if your 0.70+ bucket wins <50%, your scale is inflated - recalibrate'. Returns
    None until there is anything to measure."""
    graded = [t for t in closed if isinstance(t.get("confidence"), (int, float))]
    if not graded:
        return None
    buckets = {"0.60-0.69": (0.60, 0.70), "0.70-0.79": (0.70, 0.80), "0.80+": (0.80, 1.01)}
    out = {}
    for label, (lo, hi) in buckets.items():
        rows = [t for t in graded if lo <= t["confidence"] < hi]
        if not rows:
            continue
        wins = sum(1 for t in rows if (t.get("pnl_usd") or 0) > 0)
        win_rate = round(wins / len(rows) * 100, 1)
        avg_conf = round(sum(t["confidence"] for t in rows) / len(rows) * 100, 1)
        out[label] = {"trades": len(rows), "win_rate_pct": win_rate,
                      "avg_stated_confidence_pct": avg_conf,
                      "calibration_gap_pct": round(win_rate - avg_conf, 1)}
    high = [t for t in graded if t["confidence"] >= 0.70]
    high_wr = round(sum(1 for t in high if (t.get("pnl_usd") or 0) > 0) / len(high) * 100, 1) if high else None
    return {
        "note": "Realized win rate vs the confidence you STATED at entry, by bucket. A "
                "large negative calibration_gap_pct means your confidence is inflated; "
                "cap stated confidence until the gap closes (per the Learning Protocol).",
        "by_confidence": out,
        "high_conf_0_70_plus": {"trades": len(high), "win_rate_pct": high_wr,
                                "inflated": bool(high_wr is not None and len(high) >= 5 and high_wr < 50)},
    }


def recent_improvements(limit: int = 30) -> list[dict]:
    notes = []
    for f in sorted((ROOT / "journal" / "improvements").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            notes.append({"date": rec["ts"][:10], "note": rec["note"]})
    return notes[-limit:][::-1]


# ---------------------------------------------------------------------------
# Time: user-facing dates use the MARKET timezone (ET), never UTC. An evening run
# (after 8pm ET) is still ~02:00 UTC the NEXT day - stamping UTC would show viewers
# "tomorrow's" date on the review blurb, the equity curve, and X posts.
# ---------------------------------------------------------------------------
def _et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # tzdata unavailable (some minimal Linux images): approximate ET as UTC-4 (EDT).
        # Only affects a date stamp; off by at most an hour near midnight in winter.
        return datetime.now(timezone.utc) - timedelta(hours=4)


def _et_date() -> str:
    return _et_now().date().isoformat()


def _to_et_date(iso_ts: str | None) -> str | None:
    """Convert a UTC ISO timestamp to its ET calendar date (YYYY-MM-DD)."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        try:
            return (datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
                    - timedelta(hours=4)).date().isoformat()
        except Exception:
            return iso_ts[:10]


def _sector_map() -> dict:
    """ticker -> sector, from data/universe.json (for dashboard exposure)."""
    try:
        sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
        return {t.upper(): s for s, ts in sectors.items() for t in ts}
    except Exception:
        return {}


def build_trade_events() -> list[dict]:
    """Every filled BUY/SELL with its ET date - the equity curve annotates these so the
    line tells a story (entries, exits, stop-outs) instead of being an anonymous squiggle."""
    state_file = ROOT / "state" / "portfolio.json"
    if not state_file.exists():
        return []
    history = json.loads(state_file.read_text()).get("history", [])
    verdicts = {(t["ticker"].upper(), t["closed_at"]): t.get("verdict")
                for t in compute_closed_trades()}
    events = []
    for fill in history:
        if fill.get("status") != "filled":
            continue
        d = _to_et_date(fill.get("filled_at"))
        ev = {"date": d, "ticker": fill["ticker"].upper(),
              "action": fill["action"], "price": fill.get("fill_price")}
        if fill["action"] == "SELL_TO_CLOSE":
            ev["verdict"] = verdicts.get((fill["ticker"].upper(), d))
        events.append(ev)
    return events


def build_position_charts(positions: list[dict]) -> dict:
    """~90 daily OHLC bars per open holding plus its plan levels, for the per-position
    charts (entry/stop/target drawn on the tape). Fail-soft: a blocked fetch just omits
    that name. Written to its own file so latest.json stays slim."""
    out = {}
    if not positions:
        return out
    try:
        import yfinance as yf
    except Exception:
        return out
    for pos in positions:
        t = pos.get("ticker", "").upper()
        plan = pos.get("original_plan") or {}
        try:
            df = yf.download(t, period="5mo", interval="1d", auto_adjust=True,
                             progress=False)
            if df is None or df.empty:
                continue
            bars = []
            for idx, row in df.tail(90).iterrows():
                bars.append({
                    "date": idx.date().isoformat(),
                    "open": round(float(row["Open"]), 2), "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2), "close": round(float(row["Close"]), 2),
                })
            out[t] = {
                "bars": bars,
                "avg_cost": pos.get("avg_cost"),
                "last_price": pos.get("last_price"),
                "entry": plan.get("entry_price_max"),
                "stop": plan.get("stop_loss"),
                "target": plan.get("target_price"),
                "opened_at": _to_et_date(pos.get("opened_at")),
            }
        except Exception:
            continue
    return out


def update_watchlist_outcomes(watchlist: list, prices: dict, positions: list) -> list[dict]:
    """Track whether the agent's watchlist calls play out: when a name was first watched
    and at what price, whether price later reached its stated would_buy_at level, whether
    the agent actually bought it, and how far it has moved since. Persisted across runs so
    the site can grade the agent's foresight, not just its trades."""
    f = ROOT / "dashboard" / "data" / "watchlist_outcomes.json"
    try:
        tracked = {d["ticker"]: d for d in json.loads(f.read_text())} if f.exists() else {}
    except Exception:
        tracked = {}
    held = {p.get("ticker", "").upper() for p in positions}
    ever_bought = held | {e["ticker"].upper() for e in build_trade_events()
                          if e["action"] == "BUY"}
    today = _et_date()
    current = {str(w.get("ticker", "")).upper(): w for w in (watchlist or []) if w.get("ticker")}

    for tk, w in current.items():
        px = prices.get(tk)
        rec = tracked.get(tk) or {"ticker": tk, "first_watched": today,
                                  "price_when_added": px, "hit_buy_level": False}
        rec["currently_watched"] = True
        rec["would_buy_at"] = w.get("would_buy_at")
        rec["one_line"] = w.get("one_line")
        if px is not None:
            rec["latest_price"] = px
            base = rec.get("price_when_added") or px
            rec["move_pct_since_watched"] = round((px / base - 1) * 100, 1) if base else None
        # Parse a numeric buy level out of would_buy_at (e.g. "near $170") and flag a hit.
        lvl = None
        m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", str(w.get("would_buy_at") or ""))
        if m:
            lvl = float(m.group(1))
        if lvl and px is not None and px <= lvl * 1.02:
            rec["hit_buy_level"], rec["hit_date"] = True, rec.get("hit_date") or today
        rec["acted"] = tk in ever_bought
        tracked[tk] = rec

    for tk, rec in tracked.items():
        if tk not in current:
            rec["currently_watched"] = False
            rec.setdefault("dropped_date", today)
            px = prices.get(tk)
            if px is not None:
                rec["latest_price"] = px
                base = rec.get("price_when_added") or px
                rec["move_pct_since_watched"] = round((px / base - 1) * 100, 1) if base else None
            rec["acted"] = rec.get("acted") or (tk in ever_bought)

    rows = sorted(tracked.values(),
                  key=lambda r: (not r.get("currently_watched"), r.get("first_watched") or ""),
                  reverse=False)[:60]
    try:
        f.write_text(json.dumps(_json_safe(rows), indent=2))
    except Exception:
        pass
    return rows


def append_runs_index(run_id: str, mode: str, fills: list, commentary: str | None,
                      no_trade_reason: str | None) -> None:
    """A compact index of every published run so the site can offer a browsable archive
    linking each run's full reasoning (dashboard/data/run_<id>.json)."""
    f = ROOT / "dashboard" / "data" / "runs_index.json"
    try:
        idx = json.loads(f.read_text()) if f.exists() else []
    except Exception:
        idx = []
    headline = (commentary or no_trade_reason or "").strip().split(". ")[0][:180]
    entry = {"run_id": run_id, "date": _et_date(), "mode": mode,
             "n_fills": len(fills or []),
             "tickers_traded": sorted({str(x.get("ticker", "")).upper() for x in (fills or [])}),
             "headline": headline}
    idx = [e for e in idx if e.get("run_id") != run_id]
    idx.append(entry)
    try:
        f.write_text(json.dumps(idx[-400:], indent=2))
    except Exception:
        pass


def refresh_dashboard(context: dict, response: str, results: list, fills: list,
                      run_id: str, no_trade_reason: str | None = None,
                      commentary: str | None = None,
                      watchlist: list | None = None) -> None:
    dash = ROOT / "dashboard" / "data"
    dash.mkdir(parents=True, exist_ok=True)

    # Append today's equity + benchmark to the track-record series first (last point wins).
    hist_file = dash / "equity_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
    today = _et_date()  # market-timezone date, so evening runs don't stamp "tomorrow"
    # A benchmark value, once recorded for a date, must survive every later
    # rewrite of that day's entry - sandboxed runs can't refetch it.
    prev_today = next((h for h in hist if h.get("date") == today), {})
    bench = (context.get("benchmark_close") or _benchmark_close()
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
        "schedule_note": "Runs hourly during market hours, plus pre-market, evening, "
                         "midnight, and weekend news checks",
        "portfolio": {
            "cash_usd": context["portfolio"].get("cash_usd"),
            "total_equity_usd": context["portfolio"].get("total_equity_usd"),
            # Attach sector to each position for the dashboard exposure view.
            "positions": [{**p, "sector": _sector_map().get(p.get("ticker", "").upper())}
                          for p in context["portfolio"].get("positions", [])],
        },
        "as_of_et": _et_date(),
        "macro_snapshot": {
            name: context["macro_regime"].get("indicators", {}).get(name)
            for name in ("cpi_yoy_pct", "ten_year_yield", "vix",
                         "yield_curve_10y2y", "hy_credit_spread")
        } if context["macro_regime"].get("status") == "ok" else None,
        "macro_regime_hint": context["macro_regime"].get("regime_hint"),
        "latest_reasoning": response,
        "proposals": [{"proposal": r.proposal, "approved": r.approved,
                       "reasons": r.reasons} for r in results],
        "fills": fills,
        "closed_trades": closed[::-1],
        "performance": compute_performance_stats(closed, hist),
        "performance_breakdown": build_performance_breakdown(closed),
        "calibration": compute_calibration(closed),
        "position_risk": (context.get("position_stop_cushion") or {}).get("positions", {}),
        "data_quality": context.get("data_quality"),
        "stale_data_notice": context.get("stale_data_notice"),
        "universe_size": len(validator.load_universe()),
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
        (dash / "position_charts.json").write_text(
            json.dumps(_json_safe(build_position_charts(positions)), indent=2))
    except Exception as e:
        print(f"  (position_charts write failed: {e})")
    try:
        out["watchlist_outcomes"] = update_watchlist_outcomes(watchlist or [], prices, positions)
    except Exception as e:
        print(f"  (watchlist outcomes failed: {e})")
    append_runs_index(run_id, context.get("trading_mode", "paper"), fills, commentary, no_trade_reason)

    # Full response lives in the per-run archive; latest.json stays slim for the site.
    (dash / f"run_{run_id}.json").write_text(json.dumps(_json_safe(out), indent=2, default=str))
    slim = {k: v for k, v in out.items() if k != "latest_reasoning"}
    (dash / "latest.json").write_text(json.dumps(_json_safe(slim), indent=2, default=str))
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
              "breakdowns": build_performance_breakdown(closed),
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
        d["improvements"] = ([{"date": _et_date(),
                               "note": f"Weekly self-review: {summary}"}]
                             + d.get("improvements", []))[:30]
        _write_review_to_dashboard.write_text(json.dumps(d, indent=2, default=str))
    redeploy_dashboard()
    print("self-review complete and published")
    return 0


def run_freshness_audit(run_id: str) -> int:
    """Audit fundamentals freshness for EVERY universe name and publish the artifact.
    Runs weekly after the universe review and on demand via --freshness-audit. A
    stale name here means our XBRL extraction lags that company's latest filed
    report - the exact class of bug behind the DELL $23.4B incident."""
    from tools.freshness_audit import audit_universe
    audit = audit_universe(cross_check=True, progress=True)
    stale, errors = audit.get("stale_tickers", []), audit.get("error_tickers", [])
    mism = audit.get("value_mismatch_vs_yfinance", [])
    print(f"  audited {audit.get('audited')} names: {audit.get('fresh')} fresh, "
          f"{len(stale)} stale, {len(errors)} errors, {len(mism)} value mismatches")
    if stale or errors or mism:
        journal.log_improvement(
            f"Universe freshness audit: {len(stale)} stale ({stale}), {len(errors)} "
            f"errors ({errors}), {len(mism)} yfinance mismatches ({mism}) out of "
            f"{audit.get('audited')} names - investigate before trusting these names' "
            f"fundamentals.", run_id)
    else:
        journal.log_improvement(
            f"Universe freshness audit: all {audit.get('audited')} names verified "
            f"current against their latest SEC filings (+ yfinance cross-check).", run_id)
    redeploy_dashboard()
    return 0 if not stale else 1


def _below_min_cap(tickers: list, floor_usd: float) -> set:
    """Added universe names under the market-cap floor. Fail-open: if EVERY lookup
    fails (feed down), return empty rather than wiping a good review."""
    if not tickers or not floor_usd:
        return set()
    bad, got_any = set(), False
    try:
        import yfinance as yf
        for t in tickers:
            try:
                mcap = yf.Ticker(t).fast_info.get("marketCap")
                if isinstance(mcap, (int, float)) and mcap > 0:
                    got_any = True
                    if mcap < floor_usd:
                        bad.add(t.upper())
            except Exception:
                continue
    except Exception:
        return set()
    return bad if got_any else set()


def _unpriceable(tickers: list) -> set:
    """Names that return NO live price data (delisted / bad ticker). Fail-open: if the
    whole download fails (feed down), return empty so a good review is never wiped."""
    if not tickers:
        return set()
    try:
        import yfinance as yf
        data = yf.download(list(tickers), period="5d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
    except Exception:
        return set()
    bad, got_any = set(), False
    for t in tickers:
        try:
            df = data[t].dropna() if len(tickers) > 1 else data.dropna()
            if len(df) and float(df["Close"].iloc[-1]) > 0:
                got_any = True
            else:
                bad.add(t.upper())
        except Exception:
            bad.add(t.upper())
    return bad if got_any else set()  # nothing priced at all -> feed down, fail open


# ---------------------------------------------------------------------------
# Weekly universe review: the agent curates its own watchable universe.
# ---------------------------------------------------------------------------
def universe_review(run_id: str) -> int:
    """The agent researches the broad market (web search enabled) and proposes
    edits to data/universe.json. Deterministic guardrails cap what can change:
    held and watchlist names are untouchable, size stays 90-140, max 10 adds and
    10 removals per review, tech/AI bias preserved."""
    universe_file = ROOT / "data" / "universe.json"
    current = json.loads(universe_file.read_text())
    portfolio = get_portfolio_state()
    protected = {p["ticker"].upper() for p in portfolio.get("positions", [])}
    latest_file = ROOT / "dashboard" / "data" / "latest.json"
    if latest_file.exists():
        protected |= {w.get("ticker", "").upper()
                      for w in json.loads(latest_file.read_text()).get("watchlist", [])}
    protected.discard("")

    print("  • fresh sector scan for the review...")
    scan = scan_universe(top_n=10)
    bundle = {
        "as_of_et": _et_date(),
        "current_universe": current,
        "protected_tickers_never_remove": sorted(protected),
        "sector_relative_strength": scan.get("sector_relative_strength"),
        "top_setups_now": scan.get("top_setups"),
        "track_record_breakdowns": build_performance_breakdown(compute_closed_trades()),
    }
    review_file = ROOT / "state" / f"universe_review_{run_id}.json"
    review_file.write_text(json.dumps(bundle, indent=2, default=str))

    prompt = (
        f"Today's date in the US market timezone (ET) is {_et_date()} - use THIS date, not "
        f"the UTC run_date, in any prose. "
        "Weekly UNIVERSE REVIEW for East Equity Agent (no trading this run). Read CLAUDE.md, "
        f"then the bundle at {review_file}. Your job: curate the watchable universe honestly. "
        "Use WebSearch to research the broad market: which quality names are emerging as "
        "leaders (new highs, accelerating estimates, institutional accumulation) that we do "
        "NOT track, and which current universe names have lost leadership (persistent "
        "downtrends, broken growth, fading relevance)? Keep a strong AI/technology bias "
        "(at least ~70% of names) but market leaders from any sector earn a place. "
        "Constraints (code-enforced): US-listed common equities only, MINIMUM $1B market "
        "cap (sub-billion adds are dropped), no leveraged/inverse "
        "products, never remove the protected tickers, max 10 adds and 10 removals, final "
        "size 90-140 names. Removing a name is healthy - a universe that only grows is a "
        "museum. ALSO maintain the AI-exposure map (data/ai_exposure.json in the bundle "
        "context): classify every ADDED name and refresh any existing label your research "
        "contradicts, using exactly one of ai_supplier / ai_beneficiary / ai_neutral / "
        "ai_at_risk with a <=160-char reason written as the plain retail bear/bull case "
        "(e.g. 'frontier LLMs teach languages free'). Output a ```json block: "
        "{\"sectors\": {<full updated sectors map>}, "
        "\"added\": [..], \"removed\": [..], "
        "\"ai_exposure_updates\": {\"TICK\": {\"exposure\": \"...\", \"reason\": \"...\"}}, "
        "\"rationale\": \"3-5 plain sentences for the public improvement log\"}."
    )
    result = subprocess.run(["claude", "-p", prompt], cwd=ROOT,
                            capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        print(f"universe review failed: {result.stderr[:300]}{result.stdout[:300]}")
        return 1
    blocks = re.findall(r"```json\s*(.*?)```", result.stdout, re.DOTALL)
    data = None
    for block in reversed(blocks):
        try:
            cand = json.loads(block)
            if isinstance(cand, dict) and "sectors" in cand:
                data = cand
                break
        except json.JSONDecodeError:
            continue
    if data is None:
        print("universe review: no parsable proposal, universe unchanged")
        return 1

    # Deterministic guardrails - the agent proposes, code disposes.
    new_tickers = {t.upper() for ts in data["sectors"].values() for t in ts}
    old_tickers = {t.upper() for ts in current["sectors"].values() for t in ts}
    added, removed = new_tickers - old_tickers, old_tickers - new_tickers
    problems = []
    if not 90 <= len(new_tickers) <= 140:
        problems.append(f"size {len(new_tickers)} outside 90-140")
    if len(added) > 10 or len(removed) > 10:
        problems.append(f"too many changes (+{len(added)}/-{len(removed)}, max 10 each)")
    if removed & protected:
        problems.append(f"tried to remove protected: {sorted(removed & protected)}")
    forbidden = set(validator.load_config()["hard_rules"]["forbidden_ticker_patterns"])
    if new_tickers & forbidden:
        problems.append(f"forbidden products: {sorted(new_tickers & forbidden)}")
    if any(not re.fullmatch(r"[A-Z]{1,5}", t) for t in new_tickers):
        problems.append("invalid ticker format present")
    if problems:
        print(f"universe review REJECTED by guardrails: {problems}")
        journal.log_improvement(
            f"Universe review rejected by guardrails ({'; '.join(problems)}) - no changes.",
            run_id)
        return 1

    # Price-validate ADDED names: the review has no web access in the cloud sandbox and
    # can propose a delisted/unpriceable ticker (e.g. PSTG). Drop any added name that
    # returns no live price data before it can poison the universe. Fail-open: if the
    # whole check fails (yfinance down), keep the names rather than wipe a good review.
    dropped = _unpriceable(sorted(added)) if added else set()
    # $1B market-cap floor: sub-billion adds are dropped the same way (hard rule).
    cap_floor = validator.load_config()["hard_rules"].get("min_market_cap_usd", 0)
    small = _below_min_cap(sorted(added - dropped), cap_floor) if added else set()
    if small:
        print(f"  dropping sub-${cap_floor/1e9:.0f}B added names: {sorted(small)}")
    dropped |= small
    if dropped:
        print(f"  dropping unpriceable/sub-cap added names: {sorted(dropped)}")
        data["sectors"] = {s: [t for t in ts if t.upper() not in dropped]
                           for s, ts in data["sectors"].items()}
        added = added - dropped
        new_tickers = new_tickers - dropped

    # AI-exposure map maintenance (enum-guarded): the review classifies added names
    # and refreshes labels its research contradicts; code validates and merges so a
    # malformed label can never corrupt the business-reality layer the scanner reads.
    try:
        exp_file = ROOT / "data" / "ai_exposure.json"
        exp = json.loads(exp_file.read_text()) if exp_file.exists() else {
            "description": "per-name AI exposure", "valid_exposures":
            ["ai_supplier", "ai_beneficiary", "ai_neutral", "ai_at_risk"], "labels": {}}
        valid = set(exp.get("valid_exposures") or
                    ["ai_supplier", "ai_beneficiary", "ai_neutral", "ai_at_risk"])
        applied, rejected_lbl = 0, []
        for tk, upd in (data.get("ai_exposure_updates") or {}).items():
            tk = str(tk).upper()
            e = (upd or {}).get("exposure")
            reason = str((upd or {}).get("reason") or "")[:160]
            if tk in new_tickers and e in valid and reason:
                exp["labels"][tk] = {"exposure": e, "reason": reason}
                applied += 1
            else:
                rejected_lbl.append(tk)
        # prune labels for names no longer in the universe
        exp["labels"] = {t: v for t, v in exp["labels"].items() if t in new_tickers}
        exp["last_reviewed"] = _et_date()
        exp_file.write_text(json.dumps(exp, indent=1))
        if applied or rejected_lbl:
            print(f"  ai_exposure: {applied} labels updated"
                  + (f", rejected {rejected_lbl}" if rejected_lbl else ""))
    except Exception as e:
        print(f"  (ai_exposure maintenance failed: {e})")

    current["sectors"] = data["sectors"]
    current["last_reviewed"] = _et_date()
    universe_file.write_text(json.dumps(current, indent=2))
    drop_note = f" (dropped unpriceable: {sorted(dropped)})" if dropped else ""
    note = (f"Weekly universe review: added {sorted(added) if added else 'none'}, "
            f"removed {sorted(removed) if removed else 'none'} "
            f"({len(new_tickers)} names){drop_note}. {data.get('rationale', '')}")
    journal.log_improvement(note, run_id)
    # Persistent public log of universe changes (for the dashboard changelog).
    log_file = ROOT / "dashboard" / "data" / "universe_log.json"
    try:
        ulog = json.loads(log_file.read_text()) if log_file.exists() else []
    except Exception:
        ulog = []
    ulog.append({"date": _et_date(), "added": sorted(added), "removed": sorted(removed),
                 "dropped_unpriceable": sorted(dropped), "size": len(new_tickers),
                 "rationale": data.get("rationale", "")})
    log_file.write_text(json.dumps(ulog[-100:], indent=2))
    if latest_file.exists():
        d = json.loads(latest_file.read_text())
        d["improvements"] = ([{"date": _et_date(), "note": note}]
                             + d.get("improvements", []))[:30]
        latest_file.write_text(json.dumps(d, indent=2, default=str))
    # Weekly full-universe freshness sweep rides the same Sunday slot: audit the
    # FINAL curated universe so every name - not just this week's focus set - is
    # verified current against its latest SEC filing before Monday's pre-market.
    try:
        from tools.freshness_audit import audit_universe
        audit = audit_universe(cross_check=True, progress=False)
        a_stale = audit.get("stale_tickers", [])
        print(f"  freshness sweep: {audit.get('fresh')}/{audit.get('audited')} fresh"
              + (f", STALE: {a_stale}" if a_stale else ""))
        if a_stale:
            journal.log_improvement(
                f"Freshness sweep found stale fundamentals for {a_stale} - "
                f"do not trust these names' fundamentals until resolved.", run_id)
    except Exception as e:
        print(f"  (freshness sweep failed: {e})")
    redeploy_dashboard()
    print(f"universe updated: +{len(added)} -{len(removed)} = {len(new_tickers)} names")
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
    ap.add_argument("--gather-only", action="store_true",
                    help="cloud mode step 1: write context bundle to state/ and exit")
    ap.add_argument("--act-on", metavar="RESPONSE_FILE",
                    help="cloud mode step 2: validate/execute/publish from a saved brain response")
    ap.add_argument("--context", metavar="CONTEXT_FILE",
                    help="context bundle path (required with --act-on)")
    args = ap.parse_args()

    cfg = validator.load_config()
    run_id = f"{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6]}"
    if args.self_review:
        print(f"=== East Equity Agent self-review {run_id} ===")
        return self_review(run_id)
    if args.universe_review:
        print(f"=== East Equity Agent universe review {run_id} ===")
        return universe_review(run_id)
    if args.freshness_audit:
        print(f"=== East Equity Agent universe freshness audit {run_id} ===")
        return run_freshness_audit(run_id)
    print(f"=== East Equity Agent run {run_id} (mode: {cfg['mode']['trading_mode']}) ===")

    if args.gather_only:
        # Pure data collection (used by the relay/Action): no preflight gates,
        # no lock, no budget - it must work on weekends and holidays too.
        context = gather_context(cfg, light=args.light)
        # Never hand the brain empty/partial data WITHOUT a label. Distinguish three
        # cases: full-degradation (scan/macro dead), partial (prices OK but research
        # feeds failed), and healthy.
        scan_empty = not args.light and not context["universe_scan"].get("top_setups")
        macro_bad = context["macro_regime"].get("status") != "ok"
        degraded = macro_bad or scan_empty

        def _feed_ok(key: str) -> bool:
            v = context.get(key) or {}
            return isinstance(v, dict) and v.get("status") not in ("error", "unavailable")
        partial = (not degraded) and not all(
            _feed_ok(k) for k in ("news_and_catalysts", "sec_filings", "insider_activity"))

        relay = ROOT / "data" / "cloud_context.json"
        if degraded and relay.exists():
            cached = json.loads(relay.read_text())
            try:
                age_h = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(cached["run_date"])).total_seconds() / 3600
            except Exception:
                age_h = 999.0
            # Even a several-hours-old bundle beats an empty context - but SAY how stale.
            cached["portfolio"] = context["portfolio"]
            cached["hard_limits"] = context["hard_limits"]
            severity = "still fresh" if age_h < 4 else "STALE"
            cached["stale_data_notice"] = (
                f"Live data feeds unreachable here; using the relay bundle gathered "
                f"{age_h:.1f}h ago ({severity}). Prices and news may be up to that old - "
                f"lower confidence and avoid time-sensitive entries accordingly.")
            cached["data_quality"] = {"source": "relay_bundle", "age_hours": round(age_h, 1),
                                      "stale": age_h >= 4}
            context = cached
            print(f"  (live feeds blocked - using relay bundle, {age_h:.1f}h old)")
        elif degraded:
            # No bundle to fall back to: proceed but LABEL the emptiness loudly so the
            # brain doesn't mistake missing data for a quiet market.
            context["stale_data_notice"] = (
                "Live feeds failed AND no relay bundle is available - this context is "
                "largely EMPTY. Treat scan/macro data as missing; do not open new "
                "positions on absent data, and say so in commentary.")
            context["data_quality"] = {"source": "degraded_empty", "stale": True}
            print("  (degraded and no relay bundle - handing a labeled empty context)")
        elif partial:
            context["data_quality"] = {"source": "live_partial",
                                       "note": "price scan OK but one or more research "
                                               "feeds (news/filings/insiders) failed"}
            print("  (partial degradation - some research feeds failed; labeled)")

        out = ROOT / "state" / f"context_{run_id}.json"
        out.write_text(json.dumps(_json_safe(context), indent=2, default=str))
        print(f"CONTEXT_FILE={out}")
        return 0

    halt = preflight(cfg, run_id, news_only=args.news_only, manual=args.manual)
    if halt:
        print(f"HALT: {halt}")
        journal.log_run_summary({"halted": halt}, run_id)
        return 1

    forced_exit_fills = []
    if args.act_on:
        # Cloud mode: the scheduled agent already did the thinking; act on its output.
        print("[1-2/5] Loading saved context + brain response (cloud mode)...")
        context = json.loads(Path(args.context).read_text())
        context["portfolio"] = get_portfolio_state()
        if not args.news_only:
            forced_exit_fills = apply_safety_layer(context, cfg, run_id)
        response = Path(args.act_on).read_text()
    else:
        print("[1/5] Gathering context...")
        context = gather_context(cfg, light=args.light)
        if not args.news_only:
            forced_exit_fills = apply_safety_layer(context, cfg, run_id)
        print("[2/5] Waking the brain (Claude)...")
        response = ask_claude(context, run_id, news_only=args.news_only)

    parsed = parse_proposals(response)
    proposals, no_trade_reason = parsed["proposals"], parsed["no_trade_reason"]
    if args.news_only:
        proposals = []  # belt and suspenders: a news review never trades
    elif args.light:
        dropped = [p for p in proposals if str(p.get("action", "")).upper() == "BUY"]
        for p in dropped:
            journal.log_rejection(p, ["light_run_no_new_buys"], run_id)
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

    if proposals and not args.news_only:
        print("[2.5/5] Risk desk review...")
        ctx_path = args.context if args.act_on else str(ROOT / "state" / f"context_{run_id}.json")
        proposals = adversarial_review(proposals, ctx_path, run_id)

    print("[3/5] Validating (pure Python)...")
    # Validate against LIVE portfolio state, not the snapshot from step 1 - another
    # run may have traded while the brain was thinking.
    live_portfolio = get_portfolio_state()
    context["portfolio"] = live_portfolio
    # Rebuild the volatility map from the (possibly file-loaded) context so the
    # stop floor the validator enforces matches the stop_engineering shown to the brain.
    market_context = build_volatility_context(
        context.get("universe_scan") or {}, context.get("options_signals") or {})
    # Market caps for the $1B floor: bundle first (deep fundamentals / scanner lane
    # rows - gathered where network exists), live yfinance as a last resort. Absent
    # figures fail open in the validator; the universe review is the real gate.
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
    if fills:  # the snapshot from step 1 predates execution - re-read before publishing
        simulated_broker.mark_to_market(context["universe_scan"].get("prices", {}))
        context["portfolio"] = get_portfolio_state()
    refresh_dashboard(context, response, results, fills, run_id, no_trade_reason,
                      parsed["commentary"], parsed["watchlist"])
    draft_x_summary(fills, results, context, run_id, parsed.get("x_post"))
    journal.log_run_summary({
        "manual": args.manual,
        "proposals": len(proposals), "approved": len(approved),
        "fills": len(fills), "no_trade_reason": no_trade_reason,
    }, run_id)
    redeploy_dashboard()  # publish last so every artifact of this run is committed
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
