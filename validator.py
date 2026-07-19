"""East Equity Agent — deterministic proposal validator.

Pure Python, no LLM. Every trade proposal produced by Claude passes through
`validate_proposals()` before anything can reach a broker. A proposal is either
APPROVED or REJECTED with an explicit machine-readable reason list; rejections
are always journaled.

Design rule: this file must never import anything that talks to a network or a
broker. It reads config + portfolio state and applies hard rules. Boring on purpose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass
class ValidationResult:
    proposal: dict
    approved: bool
    reasons: list[str] = field(default_factory=list)  # rejection reasons (empty if approved)


def load_config() -> dict:
    return json.loads((ROOT / "autonomy_config.json").read_text())


def load_universe() -> set[str]:
    data = json.loads((ROOT / "data" / "universe.json").read_text())
    tickers: set[str] = set()
    for sector_tickers in data["sectors"].values():
        tickers.update(t.upper() for t in sector_tickers)
    return tickers


def load_sector_map() -> dict:
    """{TICKER: sector} from data/universe.json (for the concentration cap)."""
    data = json.loads((ROOT / "data" / "universe.json").read_text())
    return {t.upper(): sector
            for sector, tickers in data["sectors"].items() for t in tickers}


def is_market_hours(now_et) -> bool:
    """Regular US session: Mon-Fri 9:30am-4:00pm ET. Holidays are handled by the
    run-day gate upstream; this only gates order placement (BUYs) within a day."""
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def risk_halt_reasons(current_equity, equity_history: list, cfg: dict,
                      today_et: str | None = None) -> list[str]:
    """Auto-recovering BUY halts computed from the published equity history.
    Pure function (caller supplies the history list and today's ET date) so it
    is offline-testable.

    - daily_loss_halt: equity down more than risk_controls.max_daily_loss_pct_halt
      vs the last equity point BEFORE today (the day-start reference).
    - drawdown_halt: equity below (1 - max_drawdown_pct_halt) x the all-time peak.

    Halts block NEW BUYS only — sells and forced exits always proceed — and clear
    automatically once equity recovers. Fail-open on missing/garbage inputs."""
    rc = cfg.get("risk_controls", {})
    out: list[str] = []
    if not isinstance(current_equity, (int, float)) or current_equity <= 0:
        return out
    hist = [h for h in (equity_history or [])
            if isinstance(h, dict) and isinstance(h.get("equity"), (int, float))]
    daily = rc.get("max_daily_loss_pct_halt")
    if isinstance(daily, (int, float)) and daily > 0 and today_et:
        prior = [h for h in hist if str(h.get("date", "")) < today_et]
        if prior and prior[-1]["equity"] > 0:
            ref = prior[-1]["equity"]
            if current_equity <= ref * (1 - daily):
                out.append(f"daily_loss_halt:{current_equity / ref - 1:.1%} today "
                           f"(limit -{daily:.0%})")
    dd = rc.get("max_drawdown_pct_halt")
    if isinstance(dd, (int, float)) and dd > 0 and hist:
        peak = max(max(h["equity"] for h in hist), current_equity)
        if peak > 0 and current_equity <= peak * (1 - dd):
            out.append(f"drawdown_halt:{current_equity / peak - 1:.1%} from peak "
                       f"{peak:.0f} (limit -{dd:.0%})")
    return out


def kill_switch_active(cfg: dict) -> bool:
    return (ROOT / cfg["risk_controls"]["kill_switch_file"]).exists()


def stop_floor_pct(atr_pct, expected_move_pct, cfg: dict) -> float | None:
    """Minimum stop distance (as a fraction of entry) below which a stop is noise,
    not protection. Single source of truth - used both to VALIDATE proposals and to
    SURFACE the floor to the brain so it engineers stops correctly.

    Inputs are percentages as emitted by the tools (atr_pct=7.36 -> 7.36%,
    expected_move_pct=12.0 -> ±12%). The floor is the larger of:
      - min_stop_atr_multiple x 14-day ATR% (always available - the scanner computes
        it for every name; one ATR is roughly one average day's true range, so a stop
        inside it gets tripped by a single ordinary session), and
      - min_stop_expected_move_fraction x the options market's expected move to the
        nearest expiry (available for focus/held names when option data loads).
    Returns None when NO volatility input is available (caller fails open)."""
    q = cfg["trade_quality_requirements"]
    floors: list[float] = []
    if isinstance(atr_pct, (int, float)) and atr_pct > 0:
        floors.append(q["min_stop_atr_multiple"] * (atr_pct / 100.0))
    if isinstance(expected_move_pct, (int, float)) and expected_move_pct > 0:
        floors.append(q["min_stop_expected_move_fraction"] * (expected_move_pct / 100.0))
    return max(floors) if floors else None



# --- calendar-day helpers (US/Eastern market day) ----------------------------

def _et_now() -> datetime:
    """Current time in America/New_York. zoneinfo preferred; UTC-4 fallback."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=4)


def _et_today() -> date:
    return _et_now().date()


def _to_et_date(iso_ts: str | None) -> date | None:
    """Parse an ISO timestamp to its US/Eastern calendar date.

    Fail-open: unparseable timestamps return None (caller skips them).
    Naive timestamps are treated as UTC.
    """
    if not isinstance(iso_ts, str) or not iso_ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            return dt.astimezone(ZoneInfo("America/New_York")).date()
        except Exception:
            return (dt.astimezone(timezone.utc) - timedelta(hours=4)).date()
    except (ValueError, TypeError, OSError):
        return None


def _count_filled_buys_today(portfolio: dict, today_et: date | None = None) -> int:
    """Count filled BUY records in portfolio history on today's ET calendar date."""
    today = today_et or _et_today()
    count = 0
    for rec in portfolio.get("history") or []:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("action", "")).upper() != "BUY":
            continue
        if rec.get("status") != "filled":
            continue
        ts = rec.get("filled_at") or rec.get("submitted_at")
        rec_day = _to_et_date(ts)
        if rec_day is None:
            continue  # unparseable -> fail open (do not count)
        if rec_day == today:
            count += 1
    return count


def _check_daily_buy_cap(p: dict, cfg: dict, already_today: int,
                         buys_this_batch: int, reasons: list[str]) -> None:
    """Enforce swing_rules.max_new_positions_per_day against prior filled BUYs
    today (ET) plus the running count in this proposal batch."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    max_per_day = cfg["swing_rules"].get("max_new_positions_per_day")
    if not isinstance(max_per_day, (int, float)) or max_per_day <= 0:
        return
    # buys_this_batch already includes this proposal (incremented by caller).
    if already_today + buys_this_batch > max_per_day:
        reasons.append(
            f"max_new_positions_per_day_exceeded:already_{already_today}_today"
            f"+batch_{buys_this_batch}>limit_{int(max_per_day)}")


# --- individual rule checks -------------------------------------------------

# Non-BUY actions carry no entry geometry: a discretionary exit only needs to say
# what it is selling and why. Requiring the BUY schema (stop_loss, target_price,
# position_size_usd...) on a SELL used to reject legitimate exits outright.
_REQUIRED_SELL_FIELDS = ["ticker", "action", "thesis"]
_REQUIRED_HOLD_FIELDS = ["ticker", "action"]


def _check_required_fields(p: dict, cfg: dict, reasons: list[str]) -> None:
    action = str(p.get("action", "")).upper()
    q = cfg["trade_quality_requirements"]
    if action == "SELL_TO_CLOSE":
        fields = q.get("required_sell_fields", _REQUIRED_SELL_FIELDS)
    elif action == "HOLD":
        fields = _REQUIRED_HOLD_FIELDS
    else:
        fields = q["required_proposal_fields"]
    for f in fields:
        if f not in p or p[f] in (None, ""):
            reasons.append(f"missing_required_field:{f}")


def _check_long_only(p: dict, cfg: dict, reasons: list[str]) -> None:
    action = str(p.get("action", "")).upper()
    if action not in cfg["hard_rules"]["allowed_actions"]:
        reasons.append(f"forbidden_action:{action} (long-only: BUY/SELL_TO_CLOSE/HOLD)")
    instrument = str(p.get("instrument", "EQUITY")).upper()
    if instrument not in cfg["hard_rules"]["allowed_instruments"]:
        reasons.append(f"forbidden_instrument:{instrument}")
    # Belt-and-suspenders: scan free text for forbidden strategy language on BUYs.
    text = " ".join(str(p.get(k, "")) for k in ("thesis", "risk_map", "catalysts")).lower()
    for term in ("short sell", "short position", "put option", "call option",
                 "buy puts", "buy calls", "on margin", "leveraged etf", "inverse etf"):
        if term in text and action == "BUY":
            reasons.append(f"forbidden_strategy_language:'{term}'")


def _check_universe(p: dict, cfg: dict, universe: set[str], reasons: list[str]) -> None:
    ticker = str(p.get("ticker", "")).upper()
    if not re.fullmatch(r"[A-Z]{1,5}", ticker):
        reasons.append(f"invalid_ticker_format:{ticker}")
        return
    if ticker in cfg["hard_rules"]["forbidden_ticker_patterns"]:
        reasons.append(f"forbidden_ticker:{ticker} (leveraged/inverse product)")
    # Universe membership gates ENTRIES only: a holding removed from the universe
    # in a weekly review must still be sellable, or the position is trapped.
    if str(p.get("action", "")).upper() == "BUY" and \
            not cfg["universe"]["allow_off_universe_trades"] and ticker not in universe:
        reasons.append(f"off_universe_ticker:{ticker}")


def _check_swing_rules(p: dict, cfg: dict, reasons: list[str]) -> None:
    sw = cfg["swing_rules"]
    if str(p.get("action", "")).upper() == "BUY":  # horizon is an entry field
        horizon = p.get("holding_horizon_days")
        if not isinstance(horizon, (int, float)):
            reasons.append("holding_horizon_days_not_numeric")
        elif not sw["min_holding_horizon_days"] <= horizon <= sw["max_holding_horizon_days"]:
            reasons.append(
                f"horizon_out_of_swing_range:{horizon}d "
                f"(allowed {sw['min_holding_horizon_days']}-{sw['max_holding_horizon_days']}d)")
    if sw["reject_intraday_language"]:
        text = str(p.get("thesis", "")).lower()
        for term in ("day trade", "intraday", "scalp", "same-day"):
            if term in text:
                reasons.append(f"intraday_language_in_thesis:'{term}'")


def _check_prices_and_rr(p: dict, cfg: dict, reasons: list[str]) -> None:
    if str(p.get("action", "")).upper() != "BUY":
        return  # price geometry checks apply to entries only
    q = cfg["trade_quality_requirements"]
    try:
        entry = float(p["entry_price_max"])
        stop = float(p["stop_loss"])
        target = float(p["target_price"])
    except (KeyError, TypeError, ValueError):
        reasons.append("prices_not_numeric")
        return
    if not (stop < entry < target):
        reasons.append(f"price_geometry_invalid: require stop({stop}) < entry({entry}) < target({target})")
        return
    stop_dist = (entry - stop) / entry
    if stop_dist > q["max_stop_loss_distance_pct"]:
        reasons.append(f"stop_too_far:{stop_dist:.1%} > {q['max_stop_loss_distance_pct']:.0%}")
    upside = (target - entry) / entry
    if upside < q["min_target_upside_pct"]:
        reasons.append(f"target_upside_too_small:{upside:.1%} < {q['min_target_upside_pct']:.0%}")
    computed_rr = (target - entry) / (entry - stop)
    if computed_rr < q["min_risk_reward_ratio"]:
        reasons.append(f"risk_reward_too_low:{computed_rr:.2f} < {q['min_risk_reward_ratio']}")
    claimed_rr = p.get("risk_reward_ratio")
    if isinstance(claimed_rr, (int, float)) and abs(claimed_rr - computed_rr) > 0.5:
        reasons.append(f"claimed_rr_mismatch: claimed {claimed_rr}, computed {computed_rr:.2f}")


def _check_volatility_stop(p: dict, cfg: dict, market_context: dict, reasons: list[str]) -> None:
    """Reject stops placed inside the name's volatility noise band. Turns the
    CLAUDE.md guidance ('a stop inside the expected move is noise') into an enforced
    rule. Fail-open: with no volatility data for the ticker, this check is skipped -
    the price-geometry and max-distance rules still apply."""
    q = cfg["trade_quality_requirements"]
    if not q.get("volatility_stop_enforced"):
        return
    if str(p.get("action", "")).upper() != "BUY":
        return
    vol = (market_context or {}).get(str(p.get("ticker", "")).upper())
    if not isinstance(vol, dict):
        return  # no volatility data -> fail open
    try:
        entry = float(p["entry_price_max"])
        stop = float(p["stop_loss"])
    except (KeyError, TypeError, ValueError):
        return  # prices_not_numeric / geometry handled by _check_prices_and_rr
    if not (0 < stop < entry):
        return  # geometry handled elsewhere
    floor = stop_floor_pct(vol.get("atr_pct"), vol.get("expected_move_pct"), cfg)
    if floor is None:
        return  # fail open
    max_stop = q["max_stop_loss_distance_pct"]
    parts = []
    if isinstance(vol.get("atr_pct"), (int, float)) and vol["atr_pct"] > 0:
        parts.append(f"ATR {vol['atr_pct']}%")
    if isinstance(vol.get("expected_move_pct"), (int, float)) and vol["expected_move_pct"] > 0:
        parts.append(f"expected move {vol['expected_move_pct']}%")
    src = " / ".join(parts)
    if floor > max_stop + 1e-9:
        # Noise band is wider than the widest allowed stop: no valid stop exists.
        reasons.append(f"volatility_untradeable:noise_floor {floor:.1%} > max_stop "
                       f"{max_stop:.0%} ({src}) - too volatile for a swing stop")
        return
    stop_dist = (entry - stop) / entry
    if stop_dist < floor - 0.001:  # 0.1% tolerance for rounding
        reasons.append(f"stop_inside_noise_band:{stop_dist:.1%} < required {floor:.1%} "
                       f"below entry ({src})")


def _check_market_cap(p: dict, cfg: dict, market_context: dict, reasons: list[str]) -> None:
    """Hard floor: never BUY a company under the configured minimum market cap
    ($1B - sub-billion names carry delisting/manipulation/liquidity risk this
    system is not built to price). The cap comes from market_context (supplied by
    the orchestrator from the gather bundle / a live lookup); when no figure is
    available this fails OPEN - the validator itself never touches a network, and
    the universe review separately blocks sub-$1B names from ever entering."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    floor = cfg["hard_rules"].get("min_market_cap_usd")
    if not floor:
        return
    mcap = (market_context or {}).get(str(p.get("ticker", "")).upper(), {}).get("market_cap_usd")
    if isinstance(mcap, (int, float)) and 0 < mcap < floor:
        reasons.append(f"below_min_market_cap:${mcap/1e9:.2f}B < ${floor/1e9:.0f}B")


def _check_confidence(p: dict, cfg: dict, reasons: list[str]) -> None:
    conf = p.get("confidence")
    if str(p.get("action", "")).upper() != "BUY":
        # Exits don't need a confidence score; sanity-check the range only if given.
        if conf is not None and (not isinstance(conf, (int, float)) or not 0 <= conf <= 1):
            reasons.append("confidence_not_in_0_1")
        return
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        reasons.append("confidence_not_in_0_1")
    elif conf < cfg["trade_quality_requirements"]["min_confidence"]:
        reasons.append(f"confidence_too_low:{conf} < {cfg['trade_quality_requirements']['min_confidence']}")


def _check_calibration_gate(p: dict, cfg: dict, sector_map: dict, reasons: list[str]) -> None:
    """After enough closed trades, enforce learning-protocol calibration (code)."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    try:
        from tools.calibration_gate import check_buy_calibration
        ticker = str(p.get("ticker", "")).upper()
        sector = sector_map.get(ticker)
        driver = p.get("demand_driver")
        for r in check_buy_calibration(p, sector=sector, demand_driver=driver):
            reasons.append(r)
        # Target calibration: claimed RR vs REALIZED RR. min_risk_reward_ratio is
        # computed from the model's own target_price, which exit_guard never reads —
        # so without this the system's best-justified rule filters on a number
        # nothing grades, passable by writing a bigger one. Returns [] until
        # learning_controls.min_trades_for_binding closed trades exist.
        try:
            from tools.calibration_gate import check_buy_target_calibration
            for r in check_buy_target_calibration(p):
                reasons.append(r)
        except ImportError:
            pass  # target calibration not built yet on this checkout
    except Exception as e:
        # Fail-open is right — a crash in the LEARNING layer must not halt trading —
        # but it must never be SILENT. This was a bare `except: pass`, so any
        # exception here quietly removed the confidence cap and the losing-bucket
        # exception requirement with no log line and no journal entry.
        print(f"  (calibration gate failed open: {e})")


def _conviction_unlocked(cfg: dict) -> tuple[bool, str]:
    """Conviction sizing is an EARNED privilege: enough closed trades AND proof
    that high-confidence calls actually win. Reads the published breakdown
    (local file, deterministic) - no network, no LLM."""
    tier = cfg["position_sizing"].get("conviction_tier")
    if not tier:
        return False, "no conviction tier configured"
    try:
        latest = json.loads((ROOT / "dashboard" / "data" / "latest.json").read_text())
        bd = latest.get("performance_breakdown") or {}
        total = bd.get("total_trades", 0)
        if total < tier["unlocked_after_closed_trades"]:
            return False, f"{total}/{tier['unlocked_after_closed_trades']} closed trades"
        for bucket in bd.get("by_confidence", []):
            if bucket.get("bucket") == "0.70+" and bucket.get("trades", 0) >= 5:
                if bucket.get("win_rate_pct", 0) >= tier["required_high_conf_bucket_win_rate_pct"]:
                    return True, "unlocked: calibration proven"
                return False, (f"0.70+ bucket win rate {bucket.get('win_rate_pct')}% < "
                               f"{tier['required_high_conf_bucket_win_rate_pct']}%")
        return False, "0.70+ confidence bucket lacks 5 graded trades"
    except Exception as e:
        return False, f"breakdown unreadable: {e}"


def _position_committed_risk(pos: dict):
    """Entry-basis committed risk of an open position in dollars: what firing
    the stop costs relative to what was PAID (not to current marks). A stop
    trailed above cost means the position risks none of the account's own
    capital - its heat is zero and the budget is free for pyramiding.

    Returns None — NOT 0.0 — when the position carries no parseable stop.

    This used to fail OPEN, inside a risk control. A position with a malformed or
    missing stop contributed zero heat, so the 8% cap under-counted true committed
    risk and admitted BUYs that breached it. Worse, exit_guard reads a DIFFERENT
    source for the same position (`original_plan`, journal-merged), so it would
    stop that position out on a plan the heat calculation pretended did not exist —
    the two safety layers disagreed about what a position's stop was.

    Callers must treat None as "unverifiable" and refuse the trade rather than
    silently under-count; see _sum_committed_risk.
    """
    try:
        plan = pos.get("plan") or {}
        stop = float(plan.get("stop_loss") or pos.get("stop_loss") or 0)
        # An exit-guard ratcheted trailing stop supersedes the entry stop.
        trail = pos.get("trailing_stop")
        if isinstance(trail, (int, float)) and trail > stop:
            stop = float(trail)
        cost = float(pos.get("avg_cost") or 0)
        qty = float(pos.get("quantity") or 0)
        if stop <= 0 or cost <= 0 or qty <= 0:
            return None
        return max(0.0, (cost - stop) * qty)
    except (TypeError, ValueError):
        return None


def _sum_committed_risk(positions, driver: str | None = None,
                        driver_map: dict | None = None):
    """(total_committed_risk, [tickers whose risk could not be computed]).

    Separating the unverifiable set is what lets the caps fail CLOSED with a reason
    naming the offending position, instead of quietly summing it as zero. Pure.
    """
    total, unverifiable = 0.0, []
    for pos in (positions or []):
        if driver is not None and _position_demand_driver(pos, driver_map) != driver:
            continue
        risk = _position_committed_risk(pos)
        if risk is None:
            unverifiable.append(str(pos.get("ticker") or "?").upper())
        else:
            total += risk
    return total, unverifiable


def _position_demand_driver(pos: dict, driver_map: dict | None = None) -> str | None:
    """The economic bet an open position represents.

    `driver_map` is the universe-derived {TICKER: driver} fallback. Without it this
    reads only the position's own stamped field, so every lot predating the stamping
    — or one whose plan was rewritten by backfill — silently drops out of the theme
    bucket and the 2% cap under-counts. The notional cap already fell back this way;
    the risk cap did not, which is the asymmetry this parameter closes.

    The CANONICAL mapping now outranks the stamp when it is authoritative: the stamp
    is written at fill time from the proposal, so a single mislabelled entry would
    otherwise keep the 2% risk cap mis-bucketed for the whole life of the position."""
    t = str(pos.get("ticker", "")).upper()
    try:
        from tools.demand_drivers import AUTHORITATIVE_SOURCES, resolve_driver
        canonical, source = resolve_driver(t)
        if source in AUTHORITATIVE_SOURCES:
            return canonical
    except Exception:
        pass  # fall through to the stamp — never halt sizing on a map failure
    d = pos.get("demand_driver") or (pos.get("plan") or {}).get("demand_driver")
    if not d and driver_map:
        d = driver_map.get(t)
    return str(d).strip().lower() if d else None


def _risk_budget_pct(p: dict, cfg: dict) -> float:
    """Per-trade risk budget as a fraction of equity. Conviction tier (when
    earned AND the proposal qualifies) raises it; a flagged momentum unwind
    halves it (applied by the caller via market_context)."""
    rbs = cfg["position_sizing"].get("risk_based_sizing") or {}
    base = float(rbs.get("risk_per_trade_pct", 0.01))
    tier = cfg["position_sizing"].get("conviction_tier") or {}
    tier_risk = tier.get("risk_per_trade_pct")
    if isinstance(tier_risk, (int, float)) and tier_risk > base:
        unlocked, _ = _conviction_unlocked(cfg)
        conf_ok = isinstance(p.get("confidence"), (int, float)) and \
            p["confidence"] >= tier.get("min_confidence", 1.0)
        case_ok = (not tier.get("requires_conviction_case")
                   or len(str(p.get("conviction_case", ""))) >= 50)
        if unlocked and conf_ok and case_ok and "risk_desk_note" not in p:
            return float(tier_risk)
    return base


def _apply_risk_based_sizing(p: dict, cfg: dict, portfolio: dict,
                             market_context: dict, reasons: list[str]) -> float:
    """Size every BUY from the stop: risk budget = equity x risk%, position is
    CLAMPED down so (entry - stop) x shares never exceeds the budget. Returns
    the proposal's committed risk in dollars (post-clamp) for the heat checks.

    Clamping (not rejecting) keeps a good thesis tradeable when the brain
    over-sizes; the adjustment is stamped on the proposal (sizing_note) so the
    journal and dashboard show what changed and why."""
    if str(p.get("action", "")).upper() != "BUY":
        return 0.0
    rbs = cfg["position_sizing"].get("risk_based_sizing") or {}
    if not rbs.get("enabled"):
        return 0.0
    try:
        entry = float(p["entry_price_max"])
        stop = float(p["stop_loss"])
        size = float(p["position_size_usd"])
    except (KeyError, TypeError, ValueError):
        return 0.0  # reported by _check_prices_and_rr / _check_sizing
    if not (0 < stop < entry) or size <= 0:
        return 0.0  # geometry/size errors reported elsewhere
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    budget_pct = _risk_budget_pct(p, cfg)
    regime = (market_context or {}).get("_regime") or {}
    if regime.get("momentum_unwind"):
        scale = float(rbs.get("momentum_unwind_risk_scale", 0.5))
        budget_pct *= scale
    budget_usd = equity * budget_pct
    stop_dist_pct = (entry - stop) / entry
    proposed_risk = size * stop_dist_pct
    if proposed_risk <= budget_usd + 1e-9:
        return proposed_risk
    clamped = budget_usd / stop_dist_pct
    floor_usd = float(rbs.get("min_clamped_position_usd", 200))
    if clamped < floor_usd:
        reasons.append(
            f"risk_budget_size_too_small:budget ${budget_usd:.0f} at "
            f"{stop_dist_pct:.1%} stop sizes to ${clamped:.0f} < ${floor_usd:.0f} floor")
        return proposed_risk
    p["position_size_usd"] = round(clamped, 2)
    p["sizing_note"] = (
        f"risk-clamped from ${size:.0f} to ${clamped:.0f}: "
        f"{budget_pct:.2%} risk budget (${budget_usd:.0f}) / {stop_dist_pct:.1%} stop"
        + (" [momentum-unwind half budget]" if regime.get("momentum_unwind") else ""))
    return budget_usd


def _check_portfolio_heat(p: dict, cfg: dict, portfolio: dict,
                          proposed_risk: float, batch_risk: float,
                          reasons: list[str], market_context: dict | None = None) -> None:
    """Total committed risk across the book (entry-basis, to current stops)
    plus this batch's accepted BUY risk plus this proposal must stay under
    portfolio_heat_cap_pct of equity."""
    if str(p.get("action", "")).upper() != "BUY" or proposed_risk <= 0:
        return
    rbs = cfg["position_sizing"].get("risk_based_sizing") or {}
    cap = rbs.get("portfolio_heat_cap_pct")
    if not rbs.get("enabled") or not isinstance(cap, (int, float)) or cap <= 0:
        return
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    positions = portfolio.get("positions", [])
    open_heat, unverifiable = _sum_committed_risk(positions)
    if unverifiable:
        # Fail CLOSED. True heat is unknown, so approving would be a guess in the
        # risk-increasing direction — and exit_guard may still stop these positions
        # out on a plan this calculation cannot see.
        reasons.append(
            f"portfolio_heat_unverifiable:{','.join(sorted(unverifiable))} carry no "
            f"parseable stop — committed risk cannot be computed, so the "
            f"{cap:.0%} heat cap cannot be enforced")
        return
    total = open_heat + batch_risk + proposed_risk

    gap_note = ""
    br = cfg.get("book_risk") or {}
    if br.get("enabled") and br.get("gap_adjusted_heat") and positions:
        try:
            from tools.book_risk import gap_adjusted_heat
            mc = market_context or {}
            costs = cfg.get("execution_costs") or {}
            total, detail = gap_adjusted_heat(
                total, positions,
                atr_pct_of=lambda pos: (mc.get(str(pos.get("ticker") or "").upper())
                                        or {}).get("atr_pct"),
                gap_atr_fraction=costs.get("stop_gap_atr_fraction"),
                avg_pairwise_corr=(mc.get("_book") or {}).get("avg_pairwise_corr"),
                corr_threshold=br.get("gap_correlation_threshold", 0.7))
            if detail["gap_penalty_usd"] > 0:
                gap_note = (f" [incl. ${detail['gap_penalty_usd']:.0f} modelled "
                            f"stop gap-through]")
        except Exception:
            # Never let the ADJUSTMENT break the base cap: the un-adjusted total is
            # still enforced below. Loosening on an internal error would be the
            # fail-open pattern this whole rework exists to remove.
            pass

    if total > equity * cap + 1e-9:
        reasons.append(
            f"portfolio_heat_cap_exceeded:open ${open_heat:.0f} + batch "
            f"${batch_risk:.0f} + new ${proposed_risk:.0f} = ${total:.0f}{gap_note} > "
            f"{cap:.0%} of ${equity:.0f}")


# --------------------------------------------------------------------------- #
# Book-level risk: factor concentration, beta, stress. See tools/book_risk.py.
# --------------------------------------------------------------------------- #
def _canonical_driver_of(pos: dict) -> str:
    """Driver resolver passed to book_risk — canonical wins over the lot stamp."""
    return _position_demand_driver(pos) or "other"


def _check_factor_stack(p: dict, cfg: dict, portfolio: dict, proposed_risk: float,
                        reasons: list[str]) -> None:
    """Aggregate exposure to the co-moving AI-stack factor group.

    The 2% per-demand_driver cap permits roughly two positions per driver, and
    factor_map classifies 15 of 27 drivers as AI-stack — so eight positions spread
    across four different AI-stack drivers was a 100% AI book that passed every
    hard limit in this file. A per-driver cap is not a factor cap.
    """
    if str(p.get("action", "")).upper() != "BUY":
        return
    br = cfg.get("book_risk") or {}
    if not br.get("enabled"):
        return
    size = p.get("position_size_usd")
    if not isinstance(size, (int, float)) or size <= 0:
        return
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    driver = str(p.get("demand_driver") or "").strip().lower()
    if not driver:
        return
    try:
        from tools.book_risk import factor_stack_breach, factor_stack_risk_breach
        from tools.factor_map import AI_STACK_DRIVERS
    except Exception:
        return
    positions = portfolio.get("positions", [])
    breach = factor_stack_breach(
        positions, driver, size, equity,
        driver_of=_canonical_driver_of, stack_drivers=AI_STACK_DRIVERS,
        max_pct=br.get("max_factor_stack_pct"))
    if breach:
        reasons.append(breach)
    risk_breach = factor_stack_risk_breach(
        positions, driver, proposed_risk, equity,
        driver_of=_canonical_driver_of, risk_of=_position_committed_risk,
        stack_drivers=AI_STACK_DRIVERS,
        max_risk_pct=br.get("max_factor_stack_risk_pct"))
    if risk_breach:
        reasons.append(risk_breach)


def _check_portfolio_beta(p: dict, cfg: dict, portfolio: dict, market_context: dict,
                          reasons: list[str]) -> None:
    """MV-weighted beta of the book the proposal would create, against equity.

    Fails CLOSED when a held position has no beta: an unknown-beta book cannot be
    shown to be inside the ceiling, and a long-only book of high-beta AI names is a
    leveraged index bet wearing the costume of independent ideas.
    """
    if str(p.get("action", "")).upper() != "BUY":
        return
    br = cfg.get("book_risk") or {}
    cap = br.get("max_portfolio_beta")
    if not br.get("enabled") or not isinstance(cap, (int, float)) or cap <= 0:
        return
    book = (market_context or {}).get("_book") or {}
    betas = book.get("betas") or {}
    if not betas:
        return  # correlation feed unavailable this run — nothing to enforce against
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    size = p.get("position_size_usd")
    ticker = str(p.get("ticker", "")).upper()
    try:
        from tools.book_risk import projected_portfolio_beta
    except Exception:
        return
    beta, missing = projected_portfolio_beta(
        portfolio.get("positions", []),
        beta_of=lambda pos: betas.get(str(pos.get("ticker") or "").upper()),
        equity=equity, proposed_ticker=ticker,
        proposed_size_usd=size if isinstance(size, (int, float)) else 0.0,
        proposed_beta=betas.get(ticker))
    if missing:
        reasons.append(
            f"portfolio_beta_unverifiable:{','.join(sorted(set(missing)))} have no "
            f"beta in the correlation feed — projected book beta cannot be checked "
            f"against the {cap:.2f} ceiling")
        return
    if beta is not None and beta > cap + 1e-9:
        reasons.append(
            f"portfolio_beta_cap_exceeded:projected {beta:.2f} > {cap:.2f} — the "
            f"book the proposal creates is a leveraged index bet, not eight ideas")


def _check_stress_scenarios(p: dict, cfg: dict, portfolio: dict, market_context: dict,
                            reasons: list[str]) -> None:
    """Named shocks priced against the book the proposal would create."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    br = cfg.get("book_risk") or {}
    scenarios = br.get("stress_scenarios") or []
    cap = br.get("max_stress_loss_pct")
    if not br.get("enabled") or not scenarios or not isinstance(cap, (int, float)):
        return
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    size = p.get("position_size_usd")
    ticker = str(p.get("ticker", "")).upper()
    betas = ((market_context or {}).get("_book") or {}).get("betas") or {}
    try:
        from tools.book_risk import stress_breach, stress_losses
        from tools.factor_map import AI_STACK_DRIVERS
    except Exception:
        return
    rows = stress_losses(
        portfolio.get("positions", []), scenarios,
        driver_of=_canonical_driver_of,
        beta_of=lambda pos: betas.get(str(pos.get("ticker") or "").upper()),
        equity=equity, stack_drivers=AI_STACK_DRIVERS,
        proposed={"ticker": ticker,
                  "market_value_usd": size if isinstance(size, (int, float)) else 0.0,
                  "demand_driver": str(p.get("demand_driver") or "").strip().lower(),
                  "beta": betas.get(ticker)})
    breach = stress_breach(rows, cap)
    if breach:
        reasons.append(breach)


def _check_theme_initial_risk(p: dict, cfg: dict, portfolio: dict,
                              proposed_risk: float, batch_theme_risk: dict,
                              reasons: list[str]) -> None:
    """Committed risk sharing one demand_driver (open positions, entry-basis,
    plus this batch) must stay under theme_initial_risk_cap_pct of equity.
    This is the risk-based complement to the notional theme cap: two tickers
    on the same economic bet fail together, so their combined pre-paid risk is
    capped as one bet. Fail-open when the driver is missing (field check
    rejects separately)."""
    if str(p.get("action", "")).upper() != "BUY" or proposed_risk <= 0:
        return
    rbs = cfg["position_sizing"].get("risk_based_sizing") or {}
    cap = rbs.get("theme_initial_risk_cap_pct")
    if not rbs.get("enabled") or not isinstance(cap, (int, float)) or cap <= 0:
        return
    driver = p.get("demand_driver")
    if not driver or not isinstance(driver, str):
        return
    driver = driver.strip().lower()
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    # Same universe-derived fallback the notional cap uses, so a position whose
    # stamped driver is missing still counts toward its own theme's risk budget.
    try:
        from tools.demand_drivers import build_driver_map
        driver_map = build_driver_map()
    except Exception:
        driver_map = {}
    theme_heat, unverifiable = _sum_committed_risk(
        portfolio.get("positions", []), driver=driver, driver_map=driver_map)
    if unverifiable:
        reasons.append(
            f"theme_risk_unverifiable:{driver} — {','.join(sorted(unverifiable))} "
            f"carry no parseable stop, so the {cap:.0%} theme budget cannot be enforced")
        return
    total = theme_heat + batch_theme_risk.get(driver, 0.0) + proposed_risk
    if total > equity * cap + 1e-9:
        reasons.append(
            f"theme_risk_cap_exceeded:{driver} committed risk ${total:.0f} > "
            f"{cap:.0%} of ${equity:.0f} (one economic bet, capped as one)")


def _check_data_quality(p: dict, cfg: dict, market_context: dict,
                        reasons: list[str]) -> None:
    """No new risk on data the run already knows is bad.

    CLAUDE.md calls fundamentals freshness a "HARD RULE" and instructs the brain that
    on an EMPTY/degraded bundle it must not open positions. Neither was enforced by
    anything: a grep for `stale`, `data_quality` or `freshness` across validator.py
    returned NOTHING. The rule existed only as prose addressed to the same model whose
    judgement it was supposed to constrain.

    Two gates, both BUY-only — exits and holds are never blocked, because acting on
    stale data to REDUCE risk is fine and refusing to exit would be the dangerous
    direction.
    """
    if str(p.get("action", "")).upper() != "BUY":
        return
    dq = (market_context or {}).get("_data_quality") or {}
    if not dq:
        return  # no label published this run — nothing asserted, nothing to enforce
    if dq.get("empty"):
        reasons.append(
            "data_quality_empty:context bundle is EMPTY (feeds blocked and no usable "
            "relay) — opening a position would be a decision made on absent data")
        return
    if dq.get("stale"):
        age = dq.get("age_hours")
        age_s = f" ({age:.1f}h old)" if isinstance(age, (int, float)) else ""
        reasons.append(
            f"data_quality_stale:run is trading a stale relay bundle{age_s} "
            f"(source: {dq.get('source')}) — prices and catalysts may have moved")
        return
    stale_tickers = dq.get("stale_tickers") or []
    ticker = str(p.get("ticker", "")).upper()
    if ticker and ticker in {str(t).upper() for t in stale_tickers}:
        reasons.append(
            f"fundamentals_stale:{ticker} — extracted fundamentals do not reach the "
            f"latest filed period, so any fundamental leg of this thesis is unverified")


def _check_regime_gate(p: dict, cfg: dict, market_context: dict,
                       reasons: list[str]) -> None:
    """Coded regime filter: when SPY is below its 200DMA, new BUYs are
    rejected. Exits/holds are never blocked. Fail-open when regime data is
    absent (cloud bundles without the field keep working)."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    gate = cfg.get("regime_gate") or {}
    if not gate.get("block_buys_below_200dma"):
        return
    regime = (market_context or {}).get("_regime") or {}
    if regime.get("spy_below_200dma") is True:
        reasons.append(
            "regime_gate_spy_below_200dma:new entries blocked while the "
            "benchmark trades below its 200-day average (exits unaffected)")


def _check_sizing(p: dict, cfg: dict, portfolio: dict, reasons: list[str]) -> None:
    if str(p.get("action", "")).upper() != "BUY":
        return
    ps = cfg["position_sizing"]
    size = p.get("position_size_usd")
    if not isinstance(size, (int, float)) or size <= 0:
        reasons.append("position_size_not_positive_number")
        return
    equity = portfolio.get("total_equity_usd", ps["starting_capital_usd"])
    max_usd = ps["max_position_usd"]
    max_pct = ps["max_position_pct_of_portfolio"]
    # Conviction tier: higher caps, only when earned and the case is complete.
    tier = ps.get("conviction_tier")
    if tier and (size > max_usd or size > equity * max_pct):
        unlocked, why = _conviction_unlocked(cfg)
        conf_ok = isinstance(p.get("confidence"), (int, float)) and \
            p["confidence"] >= tier["min_confidence"]
        case_ok = (not tier.get("requires_conviction_case")
                   or len(str(p.get("conviction_case", ""))) >= 50)
        no_haircut = "risk_desk_note" not in p
        if unlocked and conf_ok and case_ok and no_haircut:
            max_usd = tier["max_position_usd"]
            max_pct = tier["max_position_pct_of_portfolio"]
        else:
            blockers = []
            if not unlocked: blockers.append(f"not_unlocked({why})")
            if not conf_ok: blockers.append(f"confidence<{tier['min_confidence']}")
            if not case_ok: blockers.append("missing_conviction_case")
            if not no_haircut: blockers.append("risk_desk_haircut_present")
            reasons.append(f"conviction_sizing_denied:{';'.join(blockers)}")
    if size > max_usd:
        reasons.append(f"size_exceeds_max_usd:{size} > {max_usd}")
    if size > equity * max_pct:
        reasons.append(f"size_exceeds_pct_cap:{size} > {max_pct:.0%} of {equity}")
    open_positions = portfolio.get("positions", [])
    if len(open_positions) >= ps["max_open_positions"]:
        reasons.append(f"max_open_positions_reached:{len(open_positions)}")
    invested = sum(pos.get("market_value_usd", 0) for pos in open_positions)
    if invested + size > equity * ps["max_total_exposure_pct"]:
        reasons.append("total_exposure_cap_exceeded")
    # Cash reserve floor: measured on actual cash, so it still bites when marks
    # drift and the exposure cap (market value vs equity) alone would not.
    reserve_pct = ps.get("min_cash_reserve_pct")
    cash = portfolio.get("cash_usd")
    if isinstance(reserve_pct, (int, float)) and reserve_pct > 0 \
            and isinstance(cash, (int, float)) and cash - size < equity * reserve_pct:
        reasons.append(f"cash_reserve_floor_breached:cash_after_buy "
                       f"{cash - size:.0f} < {reserve_pct:.0%} of {equity:.0f}")
    held = next((pos for pos in open_positions
                 if pos["ticker"] == str(p.get("ticker", "")).upper()), None)
    if held is not None:
        # SCALE-IN rules: adds are allowed only into WINNERS (add price above the
        # blended cost - never average down), capped per position in count and in
        # combined exposure. Config-gated so the old flat rejection is one flag away.
        sc = ps.get("scale_in") or {}
        if not sc.get("enabled"):
            reasons.append("already_holding_ticker (scale-in disabled)")
        else:
            if int(held.get("adds_count", 0)) >= int(sc.get("max_adds_per_position", 2)):
                reasons.append(f"max_adds_reached:{held.get('adds_count')}")
            try:
                if sc.get("add_only_above_cost", True) and \
                        float(p.get("entry_price_max", 0)) <= float(held.get("avg_cost", 0)):
                    reasons.append(
                        f"add_below_cost:{p.get('entry_price_max')} <= avg_cost "
                        f"{held.get('avg_cost')} (never average down)")
            except (TypeError, ValueError):
                reasons.append("add_price_not_numeric")
            combined = held.get("market_value_usd", 0) + (size or 0)
            if combined > max_usd or combined > equity * max_pct:
                reasons.append(f"combined_position_exceeds_cap:{combined:.0f} > "
                               f"{min(max_usd, equity * max_pct):.0f}")


def _check_sector_concentration(p: dict, cfg: dict, portfolio: dict,
                                sector_map: dict, reasons: list[str]) -> None:
    """position_sizing.max_sector_concentration_pct: same-sector market value
    plus the proposed size must stay under the cap. Fail-open when the ticker
    has no sector mapping (off-universe names are caught elsewhere)."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    cap = cfg["position_sizing"].get("max_sector_concentration_pct")
    if not isinstance(cap, (int, float)) or cap <= 0:
        return
    sector = sector_map.get(str(p.get("ticker", "")).upper())
    if not sector:
        return  # no mapping -> fail open
    size = p.get("position_size_usd")
    if not isinstance(size, (int, float)):
        return  # _check_sizing reports the bad size
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    same_sector = sum(
        pos.get("market_value_usd", 0) for pos in portfolio.get("positions", [])
        if sector_map.get(str(pos.get("ticker", "")).upper()) == sector)
    if same_sector + size > equity * cap:
        reasons.append(f"sector_concentration_exceeded:{sector} "
                       f"{same_sector + size:.0f} > {cap:.0%} of {equity:.0f}")


def _check_thesis_invalidators(p: dict, cfg: dict, reasons: list[str]) -> None:
    """Binding falsifiers on every BUY: invalidating_print, invalidating_structure,
    time_box — each a non-empty string. Missing/short/wrong shape -> reject."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    q = cfg.get("trade_quality_requirements") or {}
    keys = q.get("thesis_invalidator_keys") or [
        "invalidating_print", "invalidating_structure", "time_box"]
    min_chars = int(q.get("min_invalidator_chars") or 12)
    inv = p.get("thesis_invalidators")
    if not isinstance(inv, dict):
        reasons.append("thesis_invalidators_missing_or_not_object")
        return
    for k in keys:
        v = inv.get(k)
        if not isinstance(v, str) or len(v.strip()) < min_chars:
            reasons.append(f"thesis_invalidator_weak_or_missing:{k}")


def _check_demand_driver_field(p: dict, reasons: list[str]) -> None:
    """demand_driver must be a KNOWN theme label on BUYs — not merely well-formed.

    This was a format-only check, and the brain writes the field itself. Both theme
    caps match on the raw string (the 2% risk cap groups by it; the 35% notional cap
    OVERWRITES the canonical map with it), so one novel snake_case label zeroed out
    both. Reproduced: a book at the cap rejected a same-theme BUY under
    `hyperscaler_server_capex` and approved the identical BUY under
    `hyperscaler_capex_supercycle`. That defeats exactly the DELL+HPE clustering the
    caps exist to prevent — and DELL+HPE is this book's entire realized loss history.

    Membership is enforced against tools.demand_drivers.KNOWN_DRIVERS.
    """
    if str(p.get("action", "")).upper() != "BUY":
        return
    d = p.get("demand_driver")
    if not isinstance(d, str) or len(d.strip()) < 3:
        reasons.append("demand_driver_missing_or_weak")
        return
    driver = d.strip().lower()
    # Light format guard: no spaces-only garbage; allow letters/numbers/underscore.
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", driver):
        reasons.append(f"demand_driver_invalid_format:{d}")
        return
    # Membership guard: the label must be one the theme maps actually know about.
    # Fail-soft on import (pure module, no network — a failure here means something
    # is badly wrong), because halting all trading on it would be worse than
    # falling back to the format check the system ran for its whole life until now.
    try:
        from tools.demand_drivers import KNOWN_DRIVERS, driver_mismatch
    except Exception:
        return
    if driver not in KNOWN_DRIVERS:
        reasons.append(
            f"demand_driver_unknown:{driver} (not in KNOWN_DRIVERS — a novel label "
            f"would escape both theme caps; use the closest canonical driver)")
        return
    # Membership alone was never enough: every ADJACENT canonical label is also a
    # member, so the novel-label fix above closed one door and left the next one
    # open. Cross-check the stated driver against the TICKER's own mapping so the
    # same economic bet cannot be relabelled past the 2% theme risk cap. Fails open
    # when the canonical answer is only a guess — see demand_drivers.driver_mismatch.
    mismatch = driver_mismatch(p.get("ticker", ""), driver)
    if mismatch:
        reasons.append(mismatch)


def _check_theme_concentration(p: dict, cfg: dict, portfolio: dict,
                               reasons: list[str]) -> None:
    """theme_risk.max_demand_driver_concentration_pct — same demand_driver MV +
    proposed size vs equity. Uses proposal demand_driver + tools.demand_drivers
    map for held names. Fail-open if helper unavailable."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    tr = cfg.get("theme_risk") or {}
    cap = tr.get("max_demand_driver_concentration_pct")
    if not isinstance(cap, (int, float)) or cap <= 0:
        return
    size = p.get("position_size_usd")
    if not isinstance(size, (int, float)):
        return
    driver = str(p.get("demand_driver") or "").strip().lower()
    if not driver:
        return  # field check already reports
    equity = portfolio.get("total_equity_usd",
                           cfg["position_sizing"]["starting_capital_usd"])
    try:
        from tools.demand_drivers import (AUTHORITATIVE_SOURCES, build_driver_map,
                                          resolve_driver, theme_concentration_breach)
        dmap = build_driver_map()
        dmap = dict(dmap)
        # The proposal's stated driver is accepted ONLY for names we do not really
        # classify. This line used to be unconditional, which meant the concentration
        # math was computed against whatever label the proposal supplied — the
        # persistence half of the relabelling exploit _check_demand_driver_field
        # now rejects. Defense in depth: even if that check fails open on an import
        # error, a mapped ticker is still budgeted under its canonical driver.
        tk = str(p.get("ticker", "")).upper()
        _canon, _source = resolve_driver(tk)
        if _source not in AUTHORITATIVE_SOURCES:
            dmap[tk] = driver
        breach = theme_concentration_breach(
            portfolio.get("positions") or [],
            str(p.get("ticker", "")).upper(),
            size,
            equity,
            dmap,
            cap,
        )
        if breach:
            reasons.append(breach)
    except Exception:
        # Fall back: sum positions that already stamped demand_driver on the lot.
        same = 0.0
        for pos in portfolio.get("positions") or []:
            pd = str(pos.get("demand_driver") or "").strip().lower()
            if not pd:
                continue
            if pd == driver:
                same += float(pos.get("market_value_usd") or 0)
        if equity > 0 and same + size > equity * cap:
            reasons.append(
                f"theme_concentration_exceeded:{driver} "
                f"{same + size:.0f} > {cap:.0%} of {equity:.0f}")


def _check_entry_cooldown(p: dict, cfg: dict, portfolio: dict, reasons: list[str]) -> None:
    """swing_rules.min_days_between_entries_same_ticker: a fresh BUY within N days
    of the last FILLED buy on the same ticker is churn, not a new thesis. Applies
    to re-entries after a close AND to scale-in adds."""
    if str(p.get("action", "")).upper() != "BUY":
        return
    days = cfg["swing_rules"].get("min_days_between_entries_same_ticker")
    if not isinstance(days, (int, float)) or days <= 0:
        return
    ticker = str(p.get("ticker", "")).upper()
    last_buy = None
    for rec in portfolio.get("history", []):
        if not isinstance(rec, dict):
            continue
        if str(rec.get("ticker", "")).upper() == ticker \
                and str(rec.get("action", "")).upper() == "BUY" \
                and rec.get("status") == "filled":
            ts = rec.get("filled_at") or rec.get("submitted_at")
            if isinstance(ts, str) and (last_buy is None or ts > last_buy):
                last_buy = ts
    if not last_buy:
        return
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_buy)).days
    except (ValueError, TypeError):
        return  # unparseable timestamp -> fail open
    if elapsed < days:
        reasons.append(f"entry_cooldown:{elapsed}d_since_last_buy < {days:.0f}d")


def _check_sell_fraction(p: dict, reasons: list[str]) -> None:
    """Partial exits: sell_fraction, when present, must be a sane fraction."""
    if str(p.get("action", "")).upper() != "SELL_TO_CLOSE":
        return
    f = p.get("sell_fraction")
    if f is None:
        return
    if not isinstance(f, (int, float)) or not 0 < f <= 1:
        reasons.append(f"invalid_sell_fraction:{f} (must be in (0, 1])")


def _check_sell_position(p: dict, portfolio: dict, reasons: list[str]) -> None:
    """A SELL_TO_CLOSE must reference a ticker we actually hold. Previously this
    only surfaced at the broker (rejected_no_position) AFTER the proposal was
    journaled as approved."""
    if str(p.get("action", "")).upper() != "SELL_TO_CLOSE":
        return
    ticker = str(p.get("ticker", "")).upper()
    held = {str(pos.get("ticker", "")).upper() for pos in portfolio.get("positions", [])}
    if ticker not in held:
        reasons.append(f"sell_ticker_not_held:{ticker}")


# --- entry point --------------------------------------------------------------

def validate_proposals(proposals: list[dict], portfolio: dict,
                       market_context: dict | None = None) -> list[ValidationResult]:
    """Validate a batch of proposals. Kill switch rejects everything.

    market_context: optional {TICKER: {"atr_pct": float, "expected_move_pct": float}}
    used to enforce the volatility-aware stop floor. When absent, that check fails
    open and all other rules still apply (backward compatible)."""
    cfg = load_config()
    universe = load_universe()
    sector_map = load_sector_map()
    results: list[ValidationResult] = []

    if kill_switch_active(cfg):
        return [ValidationResult(p, False, ["KILL_SWITCH_ACTIVE"]) for p in proposals]

    # Calendar-day BUY cap: count already-filled BUYs today (US/Eastern), then
    # combine with the running batch counter so multi-run days and multi-BUY
    # batches both stay under swing_rules.max_new_positions_per_day.
    already_today = _count_filled_buys_today(portfolio)
    buys_this_batch = 0
    batch_risk = 0.0          # committed risk accepted earlier in this batch
    batch_theme_risk: dict[str, float] = {}
    for p in proposals:
        reasons: list[str] = []
        _check_required_fields(p, cfg, reasons)
        _check_long_only(p, cfg, reasons)
        _check_universe(p, cfg, universe, reasons)
        _check_swing_rules(p, cfg, reasons)
        _check_prices_and_rr(p, cfg, reasons)
        _check_volatility_stop(p, cfg, market_context or {}, reasons)
        _check_market_cap(p, cfg, market_context or {}, reasons)
        _check_sell_fraction(p, reasons)
        _check_sell_position(p, portfolio, reasons)
        _check_confidence(p, cfg, reasons)
        _check_calibration_gate(p, cfg, sector_map, reasons)
        _check_regime_gate(p, cfg, market_context or {}, reasons)
        _check_data_quality(p, cfg, market_context or {}, reasons)
        # Risk-based sizing clamps position_size_usd BEFORE the notional checks
        # so every downstream cap sees the final size.
        proposed_risk = _apply_risk_based_sizing(
            p, cfg, portfolio, market_context or {}, reasons)
        _check_sizing(p, cfg, portfolio, reasons)
        _check_portfolio_heat(p, cfg, portfolio, proposed_risk, batch_risk, reasons,
                              market_context or {})
        _check_theme_initial_risk(p, cfg, portfolio, proposed_risk,
                                  batch_theme_risk, reasons)
        _check_sector_concentration(p, cfg, portfolio, sector_map, reasons)
        _check_thesis_invalidators(p, cfg, reasons)
        _check_demand_driver_field(p, reasons)
        _check_theme_concentration(p, cfg, portfolio, reasons)
        # Book-level risk runs AFTER the driver checks so a mislabelled proposal is
        # rejected on the label before its factor exposure is computed from it.
        _check_factor_stack(p, cfg, portfolio, proposed_risk, reasons)
        _check_portfolio_beta(p, cfg, portfolio, market_context or {}, reasons)
        _check_stress_scenarios(p, cfg, portfolio, market_context or {}, reasons)
        _check_entry_cooldown(p, cfg, portfolio, reasons)
        if str(p.get("action", "")).upper() == "BUY":
            buys_this_batch += 1
            _check_daily_buy_cap(p, cfg, already_today, buys_this_batch, reasons)
        approved = not reasons
        if approved and str(p.get("action", "")).upper() == "BUY":
            batch_risk += proposed_risk
            d = str(p.get("demand_driver") or "").strip().lower()
            if d:
                batch_theme_risk[d] = batch_theme_risk.get(d, 0.0) + proposed_risk
        results.append(ValidationResult(p, approved=approved, reasons=reasons))
    return results
