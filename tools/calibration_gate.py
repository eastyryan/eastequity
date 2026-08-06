"""Code-enforced calibration after enough closed trades.

Reads published performance breakdown + closed-trade count. When sample size
crosses thresholds, BUY proposals into losing buckets are rejected or confidence
is capped — Learning Protocol becomes unskippable.

Used by validator (pure file reads, no network).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Sample floor shared with the bucket producer — imported so the gate can never
# act on a rate the breakdown would have withheld (2026-08-05 audit: this was a
# hand-synchronized literal 5 in three files).
from tools.performance_breakdown import MIN_BUCKET_TRADES  # noqa: E402

# Defaults (also overridable via autonomy_config learning_controls)
DEFAULTS = {
    "min_trades_for_binding": 15,
    "min_trades_for_caution": 5,
    "min_bucket_trades": MIN_BUCKET_TRADES,
    "losing_win_rate_pct": 40.0,
    "high_conf_bucket": "0.70+",
    "high_conf_min_win_rate_pct": 50.0,
    "confidence_cap_when_inflated": 0.69,
    "require_exception_paragraph_chars": 80,
    # Target attainment (see tools/exit_autopsy.build_target_calibration).
    # rr_inflation = mean claimed RR - mean realized RR. Above this, the model's
    # targets are systematically bigger than what the trades actually delivered.
    "target_rr_inflation_limit": 1.0,
    "confidence_cap_when_targets_inflated": 0.69,
}


def _load_cfg_block() -> dict:
    try:
        cfg = json.loads((ROOT / "autonomy_config.json").read_text())
        block = cfg.get("learning_controls") or {}
        out = dict(DEFAULTS)
        out.update({k: block[k] for k in DEFAULTS if k in block})
        return out
    except Exception:
        return dict(DEFAULTS)


def load_performance_breakdown() -> dict:
    """Prefer latest.json performance_breakdown; else empty."""
    try:
        latest = json.loads((ROOT / "dashboard" / "data" / "latest.json").read_text())
        return {
            "breakdown": latest.get("performance_breakdown") or {},
            "calibration": latest.get("calibration") or {},
            "total_trades": (latest.get("performance_breakdown") or {}).get("total_trades")
            or (latest.get("calibration") or {}).get("total_trades")
            or 0,
        }
    except Exception:
        return {"breakdown": {}, "calibration": {}, "total_trades": 0}


def high_conf_inflated(calib: dict, cfg: dict | None = None) -> tuple[bool, str]:
    """True if high-confidence bucket underperforms."""
    cfg = cfg or _load_cfg_block()
    # calibration shape from orchestrator.compute_calibration
    try:
        # Common shapes: {buckets: [...]} or {high_conf_0_70_plus: {inflated, win_rate_pct, trades}}
        hc = calib.get("high_conf_0_70_plus") or calib.get("high_conf") or {}
        if isinstance(hc, dict) and hc.get("inflated") is True:
            return True, "calibration.high_conf_0_70_plus.inflated"
        if isinstance(hc, dict) and isinstance(hc.get("trades"), int) and hc["trades"] >= 5:
            wr = hc.get("win_rate_pct")
            if isinstance(wr, (int, float)) and wr < cfg["high_conf_min_win_rate_pct"]:
                return True, f"high_conf win_rate {wr}% < {cfg['high_conf_min_win_rate_pct']}%"
        for b in calib.get("buckets") or calib.get("by_confidence") or []:
            if not isinstance(b, dict):
                continue
            if str(b.get("bucket") or b.get("name") or "") in ("0.70+", "high", "0.7+"):
                if int(b.get("trades") or 0) >= 5:
                    wr = b.get("win_rate_pct")
                    if isinstance(wr, (int, float)) and wr < cfg["high_conf_min_win_rate_pct"]:
                        return True, f"bucket 0.70+ WR {wr}%"
        # breakdown by_confidence
        return False, "ok"
    except Exception as e:
        return False, f"unreadable:{e}"


def losing_sector_buckets(breakdown: dict, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or _load_cfg_block()
    out = []
    for b in breakdown.get("by_sector") or []:
        if not isinstance(b, dict):
            continue
        trades = int(b.get("trades") or b.get("n") or 0)
        wr = b.get("win_rate_pct")
        if trades >= cfg["min_bucket_trades"] and isinstance(wr, (int, float)):
            if wr < cfg["losing_win_rate_pct"]:
                out.append({
                    "sector": b.get("sector") or b.get("bucket") or b.get("name"),
                    "trades": trades,
                    "win_rate_pct": wr,
                })
    return out


def check_buy_calibration(
    proposal: dict,
    *,
    sector: str | None = None,
    demand_driver: str | None = None,
) -> list[str]:
    """Return rejection reasons for a BUY under binding calibration rules.

    Empty list = pass. Soft caution is NOT returned as rejection until binding.
    """
    reasons: list[str] = []
    if str(proposal.get("action") or "").upper() != "BUY":
        return reasons
    cfg = _load_cfg_block()
    perf = load_performance_breakdown()
    total = int(perf.get("total_trades") or 0)
    if total < cfg["min_trades_for_binding"]:
        return reasons  # not binding yet

    # 1) High-confidence inflation → cap confidence
    inflated, why = high_conf_inflated(perf.get("calibration") or {}, cfg)
    conf = proposal.get("confidence")
    if inflated and isinstance(conf, (int, float)):
        cap = cfg["confidence_cap_when_inflated"]
        if conf > cap:
            reasons.append(
                f"calibration_confidence_cap:{conf} > {cap} ({why}; "
                f"recalibrate after inflated high-conf bucket)"
            )

    # 2) Losing sector without exception paragraph
    bd = perf.get("breakdown") or {}
    losers = losing_sector_buckets(bd, cfg)
    sec = sector or proposal.get("sector")
    # also try demand_driver as soft sector-like key
    for L in losers:
        if sec and str(L.get("sector") or "").lower() == str(sec).lower():
            exc = str(proposal.get("calibration_exception")
                      or proposal.get("conviction_case")
                      or proposal.get("variant_perception") or "")
            if len(exc) < cfg["require_exception_paragraph_chars"]:
                reasons.append(
                    f"calibration_losing_bucket:sector={L.get('sector')} "
                    f"WR={L.get('win_rate_pct')}% over {L.get('trades')} trades — "
                    f"need calibration_exception (>="
                    f"{cfg['require_exception_paragraph_chars']} chars) explaining "
                    f"why THIS trade differs"
                )

    # 3) Demand driver pattern if breakdown has by_theme later; skip if absent
    for b in bd.get("by_demand_driver") or bd.get("by_theme") or []:
        if not isinstance(b, dict):
            continue
        if demand_driver and str(b.get("demand_driver") or b.get("theme") or "").lower() \
                == str(demand_driver).lower():
            trades = int(b.get("trades") or 0)
            wr = b.get("win_rate_pct")
            if trades >= cfg["min_bucket_trades"] and isinstance(wr, (int, float)) \
                    and wr < cfg["losing_win_rate_pct"]:
                exc = str(proposal.get("calibration_exception")
                          or proposal.get("conviction_case") or "")
                if len(exc) < cfg["require_exception_paragraph_chars"]:
                    reasons.append(
                        f"calibration_losing_theme:{demand_driver} "
                        f"WR={wr}% over {trades} trades — need calibration_exception"
                    )

    return reasons


# --------------------------------------------------------------------------- #
# Target calibration — the 2:1 gate, made falsifiable
# --------------------------------------------------------------------------- #
def load_target_calibration() -> dict:
    """The published target_calibration block from latest.json. Fail-soft to {}."""
    try:
        latest = json.loads((ROOT / "dashboard" / "data" / "latest.json").read_text())
        tc = latest.get("target_calibration")
        return tc if isinstance(tc, dict) else {}
    except Exception:
        return {}


def targets_inflated(target_calibration: dict,
                     cfg: dict | None = None) -> tuple[bool, str]:
    """Are stated targets systematically bigger than realized outcomes?

    Returns (hit, why) and refuses to fire before the record supports it. The
    whole point of grading a claim is that you do not act on the grade until you
    have one: at n=2 the arithmetic exists but means nothing, so a non-binding
    record returns False with "not binding" in the reason rather than silently
    passing — a caller reading only the boolean should still be able to log WHY.
    """
    cfg = cfg or _load_cfg_block()
    if not isinstance(target_calibration, dict) or not target_calibration:
        return False, "no target calibration published yet"
    if not target_calibration.get("binding"):
        return False, (f"target calibration not binding "
                       f"(n_gradeable={target_calibration.get('n_gradeable')}, "
                       f"phase={target_calibration.get('phase')})")
    infl = target_calibration.get("rr_inflation")
    limit = cfg["target_rr_inflation_limit"]
    if isinstance(infl, (int, float)) and infl > limit:
        return True, (
            f"claimed RR {target_calibration.get('mean_claimed_rr')} vs realized "
            f"{target_calibration.get('mean_realized_rr')} over "
            f"{target_calibration.get('n_gradeable')} graded trades "
            f"(inflation {infl} > {limit})"
        )
    return False, f"targets within tolerance (inflation {infl} <= {limit})"


def check_buy_target_calibration(proposal: dict) -> list[str]:
    """Rejection reasons for a BUY when the record shows inflated targets.

    Wired into validator._check_calibration_gate. Deliberately narrow: it caps
    CONFIDENCE, it does not reject the trade and it does not touch the target.
    Targets remain non-binding for exits by design — the decision to let winners
    run past a milestone is correct. What is not acceptable is claiming 2.5:1
    repeatedly, realizing 0.6:1, and carrying the confidence that the claim
    bought. SELLs are never gated on entry calibration: reducing risk must never
    be blocked by a learning-layer verdict.
    """
    reasons: list[str] = []
    try:
        if str(proposal.get("action") or "").upper() != "BUY":
            return reasons
        cfg = _load_cfg_block()
        hit, why = targets_inflated(load_target_calibration(), cfg)
        if not hit:
            return reasons
        conf = proposal.get("confidence")
        cap = cfg["confidence_cap_when_targets_inflated"]
        if isinstance(conf, (int, float)) and conf > cap:
            reasons.append(
                f"target_calibration_confidence_cap:{conf} > {cap} ({why}) — "
                f"your stated targets have not been delivered; state confidence "
                f"at or below {cap} until the record closes the gap"
            )
    except Exception as e:
        # Fail OPEN: a learning-layer crash must never halt trading. It must not
        # be silent either — the caller prints, and this reason is inert.
        print(f"  (target calibration gate failed open: {str(e)[:120]})")
    return reasons


def brain_facing_target_calibration() -> dict:
    """Context-bundle view: what the model claimed vs what it delivered."""
    try:
        cfg = _load_cfg_block()
        tc = load_target_calibration()
        if not tc:
            return {"note": "no target calibration published yet", "phase": "anecdote",
                    "binding": False, "n_gradeable": 0}
        hit, why = targets_inflated(tc, cfg)
        return {
            "note": ("TARGET ATTAINMENT: your claimed risk_reward_ratio graded against "
                     "the REALIZED move on the same risk unit. The 2:1 floor is computed "
                     "from your own target_price, which the exit layer never reads - this "
                     "is what keeps that claim honest. Targets are still NOT tripwires: "
                     "holding past one is allowed. Binding only at "
                     f"{tc.get('min_trades_for_binding')} graded trades."),
            "phase": tc.get("phase"),
            "binding": tc.get("binding"),
            "n_gradeable": tc.get("n_gradeable"),
            "mean_claimed_rr": tc.get("mean_claimed_rr"),
            "mean_realized_rr": tc.get("mean_realized_rr"),
            "rr_inflation": tc.get("rr_inflation"),
            "provisional_rr_inflation": tc.get("provisional_rr_inflation"),
            "mean_capture_fraction": tc.get("mean_capture_fraction"),
            "n_target_reached": tc.get("n_target_reached"),
            "n_target_path_observed": tc.get("n_target_path_observed"),
            "targets_inflated": hit,
            "detail": why,
            "confidence_cap_when_targets_inflated":
                cfg["confidence_cap_when_targets_inflated"],
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "binding": False}


def brain_facing_calibration_status() -> dict:
    """For context bundle — where we are in the learning protocol."""
    try:
        cfg = _load_cfg_block()
        perf = load_performance_breakdown()
        total = int(perf.get("total_trades") or 0)
        inflated, why = high_conf_inflated(perf.get("calibration") or {}, cfg)
        phase = (
            "binding" if total >= cfg["min_trades_for_binding"] else
            "caution" if total >= cfg["min_trades_for_caution"] else
            "anecdote"
        )
        return {
            "note": (
                "Learning protocol phase from closed-trade sample size. "
                "binding = validator enforces confidence caps / losing-bucket exceptions. "
                "caution = shade confidence in CLAUDE only. anecdote = note only."
            ),
            "phase": phase,
            "total_closed_trades": total,
            "binding_at": cfg["min_trades_for_binding"],
            "high_conf_inflated": inflated,
            "high_conf_detail": why,
            "losing_sectors": losing_sector_buckets(perf.get("breakdown") or {}, cfg),
            "confidence_cap_when_inflated": cfg["confidence_cap_when_inflated"],
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "phase": "anecdote"}
