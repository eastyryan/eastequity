"""Deterministic LOCAL risk desk — the fallback when the LLM desk cannot run.

WHY THIS EXISTS (2026-10-01). The box (grok-bot-vm) runs the cloud routine: the
routine's own session IS the brain and hands orchestrator.py a finished
response via --act-on. The adversarial desk, however, shells out to the `grok`
CLI (runlib.brain_io.run_claude), and that binary has never been installed on
the box. With risk_controls.require_risk_desk_for_buys = true, every BUY was
therefore hard-rejected as `risk_desk_unavailable` before the validator ever
saw it — the HPE BUY of the 15:00 slot on 2026-10-01 (run 20261001-87d7c5) was
the first BUY the box proposed since it took over on 2026-09-22, and it died
there. The same missing binary is behind the launch_failed FileNotFoundError on
every daily_study / universe_review call since 2026-09-22.

WHAT IT IS NOT. It is not a pass-through and it is not an LLM. It cannot
WebSearch-verify a catalyst or judge a variant perception's prose, so it is
deliberately STRICTER than the LLM desk on everything that can be measured,
and it makes its limits visible: every review carries
desk="local_deterministic", every survivor is stamped risk_desk_mode, and the
caller journals an improvement line on every run it is used.

VETO (hard fail, any one kills the BUY):
  * stop missing / non-numeric / not below entry; target not above entry
  * volatility data missing for the ticker (stop-vs-noise cannot be shown —
    the validator fails OPEN here unless book_risk.require_volatility_data; the
    desk fails CLOSED because it is standing in for a reviewer)
  * stop inside the ATR / expected-move noise floor (validator.stop_floor_pct)
  * target upside < max(10%, min_target_upside_pct)
  * claimed RR flattered: claimed > computed + rr_flatter_tolerance
  * portfolio or bundle unavailable (cannot review = cannot approve)
  * DRY RUN OF THE FULL VALIDATOR against the live book, batch-aware: any
    reason it gives (risk per trade / heat, theme initial-risk and concentration
    caps, cash and notional sizing, thesis invalidators, variant perception,
    scenarios, RR, earnings window, regime, daily BUY cap, probe cap...) is a
    veto here. The validator still runs again after the desk on the real path;
    the dry run is what makes the desk refuse rather than merely annotate.
HAIRCUT (bounded, total clamped to MAX_RISK_DESK_HAIRCUT = -0.10):
  * entities cited by the thesis that appear nowhere in the bundle
    (validator.unsourced_claims) — per-entity, capped. NOT a veto: WebSearch is
    a mandatory brain step, so absent-from-bundle is often a real finding; the
    same reasoning keeps trade_quality_requirements.enforce_claim_grounding off.
  * same demand_driver as an open position (theme stack; caps are enforced
    by the dry-run validator, the haircut is the desk's 'force smaller size')
  * bundle data_quality stale/degraded

Pure apart from reading context_file. Never raises: an internal error on a BUY
is a veto for that BUY (fail closed), never an approval.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import validator

DESK_NAME = "local_deterministic"

DEFAULTS = {
    "min_target_upside_pct": 0.10,
    "rr_flatter_tolerance": 0.25,
    "unsourced_haircut_per_entity": -0.01,
    "unsourced_haircut_cap": -0.03,
    "theme_overlap_haircut": -0.03,
    "stale_data_haircut": -0.05,
    "require_volatility_data": True,
}
MAX_TOTAL_HAIRCUT = -0.10


def settings(cfg: dict) -> dict:
    """risk_controls.local_risk_desk merged over DEFAULTS (fail-soft)."""
    rc = (cfg.get("risk_controls") or {}) if isinstance(cfg, dict) else {}
    raw = rc.get("local_risk_desk") or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if not str(k).startswith("_")})
    return out


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN -> None


def _held_drivers(portfolio: dict) -> set[str]:
    pos = (portfolio or {}).get("positions") or []
    if isinstance(pos, dict):
        pos = [dict(v, ticker=k) for k, v in pos.items() if isinstance(v, dict)]
    out = set()
    for p in pos:
        if not isinstance(p, dict):
            continue
        d = str(p.get("demand_driver") or "").strip().lower()
        if d:
            out.add(d)
    return out


def _geometry(p: dict, mc: dict, cfg: dict, s: dict) -> list[str]:
    """Desk-specific hard fails the validator tolerates or skips."""
    fails: list[str] = []
    tkr = str(p.get("ticker", "")).upper()
    entry, stop, target = (_f(p.get("entry_price_max")), _f(p.get("stop_loss")),
                           _f(p.get("target_price")))
    if stop is None or stop <= 0:
        return ["stop_missing: a BUY without a numeric protective stop is never approved"]
    if entry is None or entry <= 0:
        return ["entry_price_max_missing"]
    if not stop < entry:
        return [f"stop_not_below_entry: stop {stop} >= entry {entry}"]
    if target is None or target <= entry:
        return [f"target_not_above_entry: target {target} vs entry {entry}"]
    stop_dist = (entry - stop) / entry
    upside = (target - entry) / entry
    q = cfg.get("trade_quality_requirements") or {}
    min_up = max(float(s["min_target_upside_pct"]),
                 float(q.get("min_target_upside_pct") or 0))
    if upside < min_up - 1e-9:
        fails.append(f"target_too_close: upside {upside:.1%} < {min_up:.0%}")
    computed_rr = (target - entry) / (entry - stop)
    claimed = _f(p.get("risk_reward_ratio"))
    if claimed is not None and claimed > computed_rr + float(s["rr_flatter_tolerance"]):
        fails.append(f"rr_flattered: claimed {claimed} vs computed {computed_rr:.2f}")
    vol = (mc or {}).get(tkr)
    floor = None
    if isinstance(vol, dict):
        try:
            floor = validator.stop_floor_pct(vol.get("atr_pct"),
                                             vol.get("expected_move_pct"), cfg)
        except Exception:
            floor = None
    if floor is None:
        if s.get("require_volatility_data", True):
            fails.append(f"volatility_data_missing: no ATR/expected move for {tkr} — "
                         f"cannot show the stop sits outside daily noise")
    elif stop_dist < floor - 0.001:
        fails.append(f"stop_inside_noise: {stop_dist:.1%} < floor {floor:.1%}")
    return fails


def review(proposals: list[dict], context_file: str | None = None, *,
           portfolio: dict | None = None, market_context: dict | None = None,
           cfg: dict | None = None) -> dict[str, dict]:
    """Deterministic reviews keyed by TICKER for every BUY in `proposals`, in the
    exact shape the LLM desk returns, so adversarial_review can consume either.
    Does not mutate `proposals`."""
    try:
        cfg = cfg if isinstance(cfg, dict) else validator.load_config()
    except Exception as exc:  # no config = no caps = no approval
        return {str(p.get("ticker", "")).upper(): _veto(
            [f"config_unavailable:{str(exc)[:120]}"]) for p in proposals
            if str(p.get("action", "")).upper() == "BUY"}
    s = settings(cfg)
    buys = [p for p in proposals if str(p.get("action", "")).upper() == "BUY"]
    if not buys:
        return {}

    bundle = None
    if context_file:
        try:
            bundle = json.loads(Path(context_file).read_text())
        except Exception:
            bundle = None
    mc = market_context if isinstance(market_context, dict) else None
    if mc is None and isinstance(bundle, dict):
        try:
            from runlib.analytics import build_volatility_context
            mc = build_volatility_context(bundle.get("universe_scan") or {},
                                          bundle.get("options_signals") or {})
        except Exception:
            mc = {}
    if isinstance(mc, dict) and "_bundle" not in mc and isinstance(bundle, dict):
        mc = dict(mc, _bundle=bundle)
    if portfolio is None and isinstance(bundle, dict) and isinstance(
            bundle.get("portfolio"), dict):
        portfolio = bundle["portfolio"]

    blocking: list[str] = []
    if not isinstance(bundle, dict) and not (isinstance(mc, dict) and mc.get("_bundle")):
        blocking.append("context_bundle_unreadable: nothing to review against")
    if not isinstance(portfolio, dict):
        blocking.append("portfolio_unavailable: caps cannot be checked against the book")
    if blocking:
        return {str(p.get("ticker", "")).upper(): _veto(blocking) for p in buys}

    # Batch-aware dry run of the full validator on COPIES: the daily-cap,
    # batch heat and batch theme risk counters see the BUYs in proposal order.
    try:
        dry = validator.validate_proposals([copy.deepcopy(p) for p in buys],
                                           copy.deepcopy(portfolio), mc)
    except Exception as exc:
        return {str(p.get("ticker", "")).upper(): _veto(
            [f"validator_dry_run_failed:{type(exc).__name__}: {str(exc)[:160]}"])
            for p in buys}

    held = _held_drivers(portfolio)
    dq = (mc or {}).get("_data_quality") or {}
    out: dict[str, dict] = {}
    for p, res in zip(buys, dry):
        tkr = str(p.get("ticker", "")).upper()
        try:
            fails = _geometry(p, mc, cfg, s)
            if not res.approved:
                fails.append("validator_dry_run: " + "; ".join(
                    str(r)[:200] for r in res.reasons))
            if fails:
                out[tkr] = _veto(fails)
                continue
            notes, adj = [], 0.0
            unsourced = validator.unsourced_claims(p, mc) or []
            if unsourced:
                cut = max(float(s["unsourced_haircut_per_entity"]) * len(unsourced),
                          float(s["unsourced_haircut_cap"]))
                adj += cut
                notes.append(f"{len(unsourced)} cited entit(ies) absent from the bundle "
                             f"and unverifiable locally {unsourced[:6]} ({cut:+.2f})")
            drv = str(p.get("demand_driver") or "").strip().lower()
            if drv and drv in held:
                adj += float(s["theme_overlap_haircut"])
                notes.append(f"stacks held demand_driver '{drv}' "
                             f"({float(s['theme_overlap_haircut']):+.2f})")
            if dq.get("stale") or dq.get("empty") or tkr in (dq.get("stale_tickers") or []):
                adj += float(s["stale_data_haircut"])
                notes.append(f"bundle data_quality stale/degraded "
                             f"({float(s['stale_data_haircut']):+.2f})")
            adj = round(max(min(adj, 0.0), MAX_TOTAL_HAIRCUT), 2)
            out[tkr] = {
                "ticker": tkr, "verdict": "approve", "desk": DESK_NAME,
                "objection": ("LOCAL DETERMINISTIC DESK (LLM desk unavailable): "
                              "geometry, stop-vs-noise, RR and a full validator dry "
                              "run against the live book all pass. "
                              + ("Haircuts: " + "; ".join(notes) if notes
                                 else "No haircut.")
                              + " Catalyst sourcing was NOT web-verified."),
                "confidence_adjustment": adj, "repairable": False,
                "repair_instruction": "",
            }
        except Exception as exc:  # fail closed per BUY
            out[tkr] = _veto([f"local_desk_error:{type(exc).__name__}: {str(exc)[:160]}"])
    return out


def _veto(fails: list[str]) -> dict:
    return {
        "verdict": "veto", "desk": DESK_NAME,
        "objection": "LOCAL DETERMINISTIC DESK (LLM desk unavailable) HARD FAIL: "
                     + " | ".join(fails),
        "confidence_adjustment": 0.0, "repairable": False, "repair_instruction": "",
        "hard_fails": list(fails),
    }
