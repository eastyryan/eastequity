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


# --- individual rule checks -------------------------------------------------

def _check_required_fields(p: dict, cfg: dict, reasons: list[str]) -> None:
    for f in cfg["trade_quality_requirements"]["required_proposal_fields"]:
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
    if not cfg["universe"]["allow_off_universe_trades"] and ticker not in universe:
        reasons.append(f"off_universe_ticker:{ticker}")


def _check_swing_rules(p: dict, cfg: dict, reasons: list[str]) -> None:
    sw = cfg["swing_rules"]
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
    q = cfg["trade_quality_requirements"]
    try:
        entry = float(p["entry_price_max"])
        stop = float(p["stop_loss"])
        target = float(p["target_price"])
    except (KeyError, TypeError, ValueError):
        reasons.append("prices_not_numeric")
        return
    if str(p.get("action", "")).upper() != "BUY":
        return  # price geometry checks apply to entries
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
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        reasons.append("confidence_not_in_0_1")
    elif conf < cfg["trade_quality_requirements"]["min_confidence"]:
        reasons.append(f"confidence_too_low:{conf} < {cfg['trade_quality_requirements']['min_confidence']}")


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
    if any(pos["ticker"] == str(p.get("ticker", "")).upper() for pos in open_positions):
        reasons.append("already_holding_ticker (adds not yet supported)")


# --- entry point --------------------------------------------------------------

def validate_proposals(proposals: list[dict], portfolio: dict,
                       market_context: dict | None = None) -> list[ValidationResult]:
    """Validate a batch of proposals. Kill switch rejects everything.

    market_context: optional {TICKER: {"atr_pct": float, "expected_move_pct": float}}
    used to enforce the volatility-aware stop floor. When absent, that check fails
    open and all other rules still apply (backward compatible)."""
    cfg = load_config()
    universe = load_universe()
    results: list[ValidationResult] = []

    if kill_switch_active(cfg):
        return [ValidationResult(p, False, ["KILL_SWITCH_ACTIVE"]) for p in proposals]

    buys_this_batch = 0
    for p in proposals:
        reasons: list[str] = []
        _check_required_fields(p, cfg, reasons)
        _check_long_only(p, cfg, reasons)
        _check_universe(p, cfg, universe, reasons)
        _check_swing_rules(p, cfg, reasons)
        _check_prices_and_rr(p, cfg, reasons)
        _check_volatility_stop(p, cfg, market_context or {}, reasons)
        _check_market_cap(p, cfg, market_context or {}, reasons)
        _check_confidence(p, cfg, reasons)
        _check_sizing(p, cfg, portfolio, reasons)
        if str(p.get("action", "")).upper() == "BUY":
            buys_this_batch += 1
            if buys_this_batch > cfg["swing_rules"]["max_new_positions_per_day"]:
                reasons.append("max_new_positions_per_day_exceeded")
        results.append(ValidationResult(p, approved=not reasons, reasons=reasons))
    return results
