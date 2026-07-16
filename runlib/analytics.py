"""Run analytics, closed trades, calibration, charts helpers."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import validator
from tools.watchlist_triggers import TRIGGER_TOLERANCE_PCT, parse_price_level
from runlib.core import ROOT, et_date, et_now, to_et_date, json_safe, light_prices

def expected_slots(weekday: bool) -> list[float]:
    """Scheduled run slots (ET hours) for a weekday vs a weekend day.
    KEEP IN SYNC with the slot gate in scripts/run_cycle.sh and the cloud
    routines: weekdays run SEVEN slots (user policy 2026-07-13) — 6am, 9am,
    10am, 12pm, 2pm, 4pm, 5:30pm (5:30 is a research review, no trading, but
    still journals a run summary); weekends run news-only at midnight and
    11:59pm. The nightly cloud midnight news run may journal an extra completed
    run — the heartbeat only alarms on MISSING runs, so that is harmless."""
    return [6, 9, 10, 12, 14, 16, 17.5] if weekday else [0, 23.98]


def build_health() -> dict:
    """Runs heartbeat: expected schedule slots so far today (ET) vs runs actually
    journaled. A silently-dead pipeline (the plan-mode parse bug ran for DAYS
    unnoticed) now shows up on the dashboard as missed runs instead of nothing."""
    now = et_now()
    weekday = now.weekday() < 5
    slots = expected_slots(weekday)
    now_h = now.hour + now.minute / 60
    expected = sum(1 for s in slots if s <= now_h)
    completed = 0
    last_ts = None
    from datetime import timedelta as _td
    for delta in (0, 1):  # journal files are named by UTC date; today ET spans two
        f = ROOT / "journal" / "runs" / f"{(datetime.now(timezone.utc) - _td(days=delta)).date().isoformat()}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = to_et_date(rec.get("ts"))
            if d == et_date() and "halted" not in rec and not rec.get("manual"):
                completed += 1
                last_ts = rec.get("ts") or last_ts
    missed = max(0, expected - completed)
    # Bundle-age alarm: the relay bundle is the cloud runs' data supply. If the
    # gatherers (GH Action / local relay) die, the bundle ages - warn well BEFORE
    # the 4h stale threshold so it gets fixed, not discovered after the fact.
    bundle_age_h = None
    try:
        b = json.loads((ROOT / "data" / "cloud_context.json").read_text())
        bundle_age_h = round((datetime.now(timezone.utc)
                              - datetime.fromisoformat(b["run_date"])).total_seconds() / 3600, 1)
    except Exception:
        pass
    market_hours = weekday and 9.5 <= now_h <= 16
    bundle_alarm = bool(bundle_age_h is not None and
                        (bundle_age_h > 2 if market_hours else bundle_age_h > 8))
    status = "ok"
    if missed > 1:
        status = "DEGRADED - scheduled runs are being missed"
    elif bundle_alarm:
        status = f"WARNING - data bundle is {bundle_age_h}h old (gatherers may be down)"
    return {
        "as_of_et": now.isoformat(timespec="minutes"),
        "expected_runs_so_far": expected,
        "completed_scheduled_runs": completed,
        "missed": missed,
        "bundle_age_hours": bundle_age_h,
        "last_scheduled_run_utc": last_ts,
        "status": status,
    }


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
    """Per open position: how much room is left between today's price and the
    EFFECTIVE stop — max(plan stop, chandelier trailing_stop) — measured in the
    name's own volatility, so the brain sees the level the safety layer will
    actually enforce. A cushion under ~1 ATR means an ordinary session could
    trip the stop - the brain should decide deliberately (hold through, or exit
    on its own terms) rather than be noise-stopped."""
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
        # Effective stop: the ratcheted chandelier trail can only RAISE the
        # enforced level, never lower it. Missing/garbage trail -> plan stop
        # only (identical to the pre-trail behaviour).
        try:
            trail = float(pos.get("trailing_stop") or 0.0) or None
        except (TypeError, ValueError):
            trail = None
        eff_stop = max(stop, trail) if trail is not None else stop
        cushion_pct = (last - eff_stop) / last * 100 if last else None
        entry = plan.get("entry_price_max") or pos.get("avg_cost")
        info = {
            "last_price": round(last, 2),
            "recorded_stop": round(stop, 2),
            "cushion_to_stop_pct": round(cushion_pct, 2) if cushion_pct is not None else None,
            "atr_pct": atr,
            "expected_move_pct": v.get("expected_move_pct"),
        }
        if trail is not None and trail > stop:
            info["trailing_stop"] = round(trail, 2)
            info["effective_stop"] = round(eff_stop, 2)
        if atr and cushion_pct is not None and atr > 0:
            info["cushion_in_atr"] = round(cushion_pct / atr, 2)
            info["inside_noise_band"] = cushion_pct < atr  # < ~1 average day's range
        if entry:
            try:
                info["stop_distance_from_entry_pct"] = round((float(entry) - eff_stop) / float(entry) * 100, 2)
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

def benchmark_close(ticker: str = "SPY") -> float | None:
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="5d")
        return round(float(h["Close"].iloc[-1]), 2)
    except Exception as e:
        print(f"  WARNING: benchmark fetch failed ({e}) - S&P comparison will show a gap")
        return None


def trade_plans() -> dict:
    """Latest FILLED BUY proposal per ticker from the journal (thesis, stop,
    target, horizon). Rejected/unfilled proposals are excluded: the journal logs
    every validated proposal, and an unfiltered 'latest per ticker' let a later
    rejected proposal supply the plan a closed trade was graded against."""
    from tools.portfolio_state import filled_buy_proposal_ids
    filled = filled_buy_proposal_ids()
    plans: dict = {}
    for f in sorted((ROOT / "journal" / "proposals").glob("*.jsonl")):
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            p = rec.get("proposal", {})
            if str(p.get("action", "")).upper() == "BUY" and p.get("ticker"):
                tk = p["ticker"].upper()
                if rec.get("run_id") not in filled.get(tk, ()):
                    continue
                plans[tk] = p
    return plans


def compute_closed_trades() -> list[dict]:
    """Closed trades with a thesis verdict, from the broker history + trade plans."""
    state_file = ROOT / "state" / "portfolio.json"
    if not state_file.exists():
        return []
    history = json.loads(state_file.read_text()).get("history", [])
    plans = trade_plans()
    opens: dict[str, dict] = {}
    closed = []
    for fill in history:
        if fill.get("status") != "filled":
            continue
        t = fill["ticker"].upper()
        if fill["action"] == "BUY":
            opens[t] = fill
        elif fill["action"] == "SELL_TO_CLOSE" and (fill.get("avg_cost") or t in opens):
            plan = plans.get(t, {})
            # Grading numbers come from the position's OWN plan stamped on the
            # sell fill (entry_plan) whenever present; the journal join is the
            # legacy fallback and supplies the narrative (thesis) only.
            entry_plan = fill.get("entry_plan") or {}
            # New-style fills stamp entry data directly (adds/partials make
            # open/close pairing unreliable); legacy fills fall back to pairing.
            if fill.get("avg_cost"):
                entry = float(fill["avg_cost"])
                opened_ts = fill.get("position_opened_at") or fill["filled_at"]
                opens.pop(t, None)
            else:
                entry_fill = opens.pop(t)
                entry = float(entry_fill["fill_price"])
                opened_ts = entry_fill["filled_at"]
            exit_px = float(fill["fill_price"])
            stop = float(entry_plan.get("stop_loss") or plan.get("stop_loss") or 0)
            target = float(entry_plan.get("target_price") or plan.get("target_price") or 0)
            days_held = max((datetime.fromisoformat(fill["filled_at"])
                             - datetime.fromisoformat(opened_ts)).days, 0)
            horizon = entry_plan.get("holding_horizon_days") or plan.get("holding_horizon_days")
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
                "opened_at": to_et_date(opened_ts), "closed_at": to_et_date(fill["filled_at"]),
                "days_held": days_held, "pnl_usd": price_pnl,
                "dividends_usd": round(divs, 2), "total_pnl_usd": round(total_pnl, 2),
                "fees_usd": fill.get("fees_usd"), "gap_modeled": fill.get("gap_modeled", False),
                "r_multiple": r_multiple, "verdict": verdict,
                "confidence": entry_plan.get("confidence") or plan.get("confidence"),
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
def et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # tzdata unavailable (some minimal Linux images): approximate ET as UTC-4 (EDT).
        # Only affects a date stamp; off by at most an hour near midnight in winter.
        return datetime.now(timezone.utc) - timedelta(hours=4)


def et_date() -> str:
    return et_now().date().isoformat()


def to_et_date(iso_ts: str | None) -> str | None:
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


def proposal_ev(p: dict):
    """Probability-weighted expected value derived from a BUY proposal's own
    scenarios - published next to the proposal so every thesis carries its
    honest math. None for non-BUYs or when scenarios are unusable."""
    try:
        if str(p.get("action", "")).upper() != "BUY":
            return None
        from tools.scenario_ev import expected_value
        return expected_value(p.get("scenarios"), p.get("entry_price_max"),
                              p.get("holding_horizon_days"))
    except Exception:
        return None


def sector_map() -> dict:
    """ticker -> sector, from data/universe.json (for dashboard exposure)."""
    try:
        sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
        return {t.upper(): s for s, ts in sectors.items() for t in ts}
    except Exception:
        return {}


def sector_exposure(portfolio: dict) -> list[dict]:
    """Market value + share of equity per sector for the open book — the
    machine-readable counterpart of the enforced sector-concentration cap."""
    smap = sector_map()
    equity = portfolio.get("total_equity_usd") or 0
    by: dict = {}
    for p in portfolio.get("positions", []):
        s = smap.get(str(p.get("ticker", "")).upper()) or "unmapped"
        by[s] = by.get(s, 0.0) + (p.get("market_value_usd") or 0.0)
    return sorted(({"sector": s, "value_usd": round(v, 2),
                    "pct_of_equity": round(v / equity * 100, 1) if equity else None}
                   for s, v in by.items()),
                  key=lambda d: -(d["value_usd"] or 0))


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
        d = to_et_date(fill.get("filled_at"))
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
                "opened_at": to_et_date(pos.get("opened_at")),
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
    today = et_date()
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
        # Buy level via the SAME $-anchored parser the trigger checker uses -
        # the old first-bare-number regex read "50-over-200" as a $50 level and
        # "7/29 earnings" as $7, so the public foresight grades were wrong.
        lvl = parse_price_level(w.get("would_buy_at"))
        if rec.get("parsed_level") != lvl:
            # Level changed (or is no longer a price): a sticky hit graded
            # against the OLD text is stale evidence - reset and regrade.
            rec["parsed_level"] = lvl
            rec["hit_buy_level"] = False
            rec.pop("hit_date", None)
        if lvl and px is not None and abs(px / lvl - 1) <= TRIGGER_TOLERANCE_PCT:
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
        f.write_text(json.dumps(json_safe(rows), indent=2))
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
    entry = {"run_id": run_id, "date": et_date(), "mode": mode,
             "n_fills": len(fills or []),
             "tickers_traded": sorted({str(x.get("ticker", "")).upper() for x in (fills or [])}),
             "headline": headline}
    idx = [e for e in idx if e.get("run_id") != run_id]
    idx.append(entry)
    try:
        f.write_text(json.dumps(idx[-400:], indent=2))
    except Exception:
        pass
