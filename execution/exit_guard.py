"""Deterministic exit enforcement — runs BEFORE the brain each cycle.

An LLM re-underwriting a losing position can rationalize holding through its
own stop ("thesis intact, stop was too tight"). This module removes that
option: stops and holding horizons are enforced in pure Python from the
position's original_plan, using the same scanner prices the brain will see.
Forced exits execute through the normal broker place_order/readback contract
so nothing about execution or journaling changes.

Never force-exits blind: positions with no recorded plan or no fresh price
are left for the brain to review.
"""

from __future__ import annotations

import json
import re

import journal
from execution import broker


def forced_exit_client_order_id(ticker: str, reason: str, et_day: str) -> str:
    """Stable id for one forced exit of one name on one trading day.

    THE HOLE THIS CLOSES (audited 2026-07-19). Every other order path is
    idempotent — queued intents are re-placed with their ORIGINAL client id, and
    Alpaca enforces client_order_id uniqueness, so a crashed or retried executor
    resolves to the existing broker order via the 409 path. Forced exits built
    their order dict with NO client_order_id, so place_order minted a fresh
    EE-{uuid4} on every call and the dedupe that protects every other path did
    not cover them.

    That matters because nothing serialises a cloud stop-watch against a local
    run: stop_watch takes acquire_run_lock but never the cross-node lease, and
    the two nodes hold separate locks on separate filesystems. Both read the same
    breach, both fire, and with random ids they become two genuinely different
    market sells. On a fractional or partially-filled position that over-sells.

    Keyed on the trading DAY rather than the timestamp so two nodes reacting to
    the same breach minutes apart still collide — which is the point. A genuine
    second exit of the same name on the same day cannot occur: the first one
    closes the position.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(reason or "exit").lower()).strip("-")[:20]
    return f"EEXIT-{str(ticker).upper()}-{et_day}-{slug or 'exit'}"

# Chandelier trailing-stop ATR multiple used when autonomy_config's
# trade_quality_requirements block does not carry trailing_stop_atr_multiple.
TRAILING_STOP_ATR_MULTIPLE_DEFAULT = 3.0


def _trailing_atr_multiple() -> float:
    """Chandelier multiple from config (trade_quality_requirements ->
    trailing_stop_atr_multiple) with a hard-coded 3.0 default. Fail-soft:
    an unreadable config or missing key never breaks a run."""
    try:
        cfg = json.loads((broker.ROOT / "autonomy_config.json").read_text())
        v = (cfg.get("trade_quality_requirements") or {}).get("trailing_stop_atr_multiple")
        if v is not None:
            return float(v)
    except Exception:
        pass
    return TRAILING_STOP_ATR_MULTIPLE_DEFAULT


def _chandelier_stop(high_water, atr_pct, last, multiple: float):
    """Chandelier exit level: high-water mark since entry − multiple × ATR,
    with ATR expressed in absolute dollars (atr_pct of the CURRENT price).
    None when any input is missing/unparseable — the caller then behaves
    exactly as today (plan stop only)."""
    if high_water is None or atr_pct is None or last is None:
        return None
    try:
        hw, atrp, px = float(high_water), float(atr_pct), float(last)
    except (TypeError, ValueError):
        return None
    if hw <= 0 or atrp <= 0 or px <= 0:
        return None
    return hw - multiple * (atrp / 100.0) * px


def check_forced_exits(portfolio: dict, prices: dict, atr_by_ticker: dict | None = None) -> list[dict]:
    """Return forced-exit instructions for stop breaches and expired horizons.

    portfolio: dict from tools.portfolio_state.get_portfolio_state()
    prices: {ticker: last_close} from the universe scanner
    atr_by_ticker: optional {TICKER: atr_pct} so each stop-breach exit carries
        the name's ATR — used downstream to model overnight stop gap-through.
        Optional/defaulted: the current call site works unchanged.

    Chandelier trailing stop (ratchet-up only): when a position carries a
    high_water mark and fresh ATR, the trail = high_water − N×ATR$ (N from
    config, default 3.0). It becomes active ONLY once it exceeds the plan
    stop — until then the plan stop governs, and there is deliberately NO
    breakeven jump at +1R. Once active it only ever RATCHETS UP (never
    lowers, never widens): effective stop = max(plan stop_loss, trailing).
    New trail levels are persisted back to the ledger via the broker
    (update_trailing_stops) — exit_guard itself never writes state.
    """
    atr_by_ticker = atr_by_ticker or {}
    multiple = _trailing_atr_multiple()
    exits = []
    trail_updates: dict = {}  # {TICKER: new level} persisted via the broker
    for pos in portfolio.get("positions", []):
        plan = pos.get("original_plan")
        if not plan:
            # Never force-exit blind — but never do it SILENTLY either. A position
            # with no recoverable plan is one this layer will not stop on any run,
            # for as long as it is held, and it used to vanish from the loop without
            # a single line of output. It is the same condition that makes
            # validator._check_portfolio_heat fail closed and block all new BUYs, so
            # the operator needs to see it here, where it is diagnosable, rather than
            # inferring it from a heat rejection three layers away.
            print(f"  UNSTOPPABLE POSITION {str(pos.get('ticker', '?')).upper()}: no "
                  f"recoverable plan (journal backfill found none) — this layer "
                  f"cannot stop it; new BUYs are blocked while it is open")
            continue
        ticker = pos["ticker"].upper()
        last = prices.get(ticker)
        if last is None:
            continue  # no fresh price — leave for the brain
        stop = plan.get("stop_loss")
        horizon = plan.get("holding_horizon_days")
        days_held = pos.get("days_held")

        # --- Chandelier trail (only meaningful alongside a plan stop) ---
        trailing = None
        if stop:
            try:
                trailing = float(pos.get("trailing_stop") or 0.0) or None
            except (TypeError, ValueError):
                trailing = None
            chandelier = _chandelier_stop(
                pos.get("high_water"), atr_by_ticker.get(ticker), last, multiple)
            # Activation gate: the trail exists only once the chandelier has
            # climbed ABOVE the plan stop; ratchet: it can only ever rise.
            if chandelier is not None and chandelier > float(stop) \
                    and chandelier > (trailing or 0.0):
                trailing = round(chandelier, 4)
                trail_updates[ticker] = trailing
                pos["trailing_stop"] = trailing  # callers see it this run too

        # Effective stop for the breach check: the trail never lowers it.
        effective_stop = None
        trail_binding = False
        if stop:
            effective_stop = float(stop)
            if trailing is not None and trailing > effective_stop:
                effective_stop = trailing
                trail_binding = True

        reason = None
        if effective_stop is not None and float(last) <= effective_stop:
            reason = "trailing_stop_breached" if trail_binding else "stop_loss_breached"
        elif horizon and days_held is not None and days_held >= float(horizon):
            reason = "horizon_expired"
        if reason:
            exits.append({
                "ticker": ticker, "reason": reason,
                "last_price": float(last),
                # stop_loss carries the BINDING level so the broker's gap-through
                # fill models the level that actually fired (trail or plan stop).
                "stop_loss": effective_stop,
                "plan_stop_loss": float(stop) if stop else None,
                "trailing_stop": trailing,
                "atr_pct": atr_by_ticker.get(ticker),
                "days_held": days_held, "horizon": horizon,
                # THE PLAN THIS EXIT IS BEING JUDGED AGAINST, carried on the exit
                # itself (added 2026-08-03). Audited that day: the HPE close of
                # 2026-07-15 sits in the ledger with `stop_loss: None` and
                # `forced_exit_reason: None`, so nothing on that row could say what
                # it was measured against or whether a rule or a judgement closed
                # it — while the DELL row two entries above carries both. An exit
                # whose plan did not survive it cannot be graded at all, and an
                # ungradeable exit is a decision the system takes for free.
                # target_price and holding_horizon_days ride along because the
                # claimed risk/reward that cleared the 2:1 gate is computed from
                # them, and grading that claim is impossible once the lot is gone.
                "plan_target_price": plan.get("target_price"),
                "plan_holding_horizon_days": horizon,
                "entry_plan_snapshot": {
                    "stop_loss": plan.get("stop_loss"),
                    "target_price": plan.get("target_price"),
                    "holding_horizon_days": plan.get("holding_horizon_days"),
                    "entry_price_max": plan.get("entry_price_max"),
                    "confidence": plan.get("confidence"),
                },
                "avg_cost": pos.get("avg_cost"),
            })
    if trail_updates:
        try:  # broker owns all ledger writes; a failed persist never blocks exits
            broker.update_trailing_stops(trail_updates)
        except Exception as e:
            print(f"  (trailing-stop persist failed: {e})")
    return exits


def execute_forced_exits(exits: list, prices: dict, run_id: str,
                         atr_by_ticker: dict | None = None) -> list[dict]:
    """Close each flagged position via the broker; mandatory readback + journal.

    atr_by_ticker: optional {TICKER: atr_pct}. For stop-breach exits, the stop
    level and ATR are passed to the broker so the fill models overnight
    gap-through (fills BELOW the stop) instead of pretending it filled at the
    stop. Optional/defaulted — the existing orchestrator call still works; the
    exit dict's own atr_pct (from check_forced_exits) is used when this is absent."""
    atr_by_ticker = atr_by_ticker or {}
    fills = []
    # Snapshot positions before closes so exit autopsies keep plan / days_held.
    try:
        pre_positions = {
            str(p.get("ticker", "")).upper(): dict(p)
            for p in (broker.get_portfolio().get("positions") or [])
        }
    except Exception:
        pre_positions = {}
    for ex in exits:
        ref = prices.get(ex["ticker"])
        if ref is None:
            continue  # price vanished between check and execute — skip, never guess
        pos_before = pre_positions.get(str(ex["ticker"]).upper())
        from runlib.core import et_date
        order = broker.place_order({
            "ticker": ex["ticker"], "action": "SELL_TO_CLOSE",
            "reference_price": ref, "proposal_id": run_id,
            "forced_exit_reason": ex["reason"],
            # Deterministic id so a cloud stop-watch and a local run reacting to
            # the SAME breach collide at the broker instead of both selling.
            "client_order_id": forced_exit_client_order_id(
                ex["ticker"], ex["reason"], et_date()),
            # stop + ATR let the broker model overnight gap-through on stop exits
            "stop_loss": ex.get("stop_loss"),
            "atr_pct": ex.get("atr_pct") if ex.get("atr_pct") is not None
                       else atr_by_ticker.get(ex["ticker"]),
            # PERSIST THE PLAN ONTO THE LEDGER ROW. Both brokers build the stored
            # fill as {**order, ...}, so anything put here survives on the trade
            # record forever — which is the only place a grader can find it once
            # the position is gone. These keys never reach a broker API (alpaca's
            # request body is constructed field by field), so they are free.
            "plan_stop_loss": ex.get("plan_stop_loss"),
            "plan_target_price": ex.get("plan_target_price"),
            "plan_holding_horizon_days": ex.get("plan_holding_horizon_days"),
            "trailing_stop": ex.get("trailing_stop"),
            "exit_plan_snapshot": ex.get("entry_plan_snapshot"),
            "days_held_at_exit": ex.get("days_held"),
        })
        fill = broker.readback(order["order_id"])  # mandatory readback
        status = (fill or {}).get("status")
        if fill is not None and status in broker.QUEUED_STATUSES + broker.RESTING_STATUSES:
            # Queued for the Actions executor, or resting at the broker until
            # the next session (protective sells are never canceled). The
            # executor/reconcile journals the fill + autopsy when it completes.
            journal.log_intent(order, status, run_id)
            print(f"  FORCED EXIT QUEUED {ex['ticker']} ({status})")
            continue
        if fill is None or status != "filled":
            print(f"  FORCED EXIT FAILED {ex['ticker']}: {status}")
            continue
        journal.log_trade(order, fill, run_id)
        fills.append(fill)
        try:
            from tools.exit_autopsy import (
                build_exit_autopsy_from_fill, grade_and_persist_autopsy,
            )
            rec = build_exit_autopsy_from_fill(
                fill, order, pos_before, forced=True, reason=ex.get("reason"))
            graded = grade_and_persist_autopsy(rec)
        except Exception as e:
            graded = None
            print(f"  (exit autopsy skipped: {e})")
        _grade_exit_execution(fill, order, ex, graded)
        print(f"  FORCED EXIT {fill['ticker']} {fill['quantity']} "
              f"@ {fill['fill_price']} ({ex['reason']})")
    return fills


def _grade_exit_execution(fill: dict, order: dict, ex: dict,
                          autopsy: dict | None) -> None:
    """Measure this exit against its own plan and against the gap model.

    THE UNMEASURED ASSUMPTION (DELL, 2026-07-15). Plan stop 408.00, filled at
    389.7542 — 4.47% of the stop level, 0.55 ATR THROUGH the line the position was
    sized against, turning a 1%-risk trade into a realized -13.5% (1.43x planned
    risk). autonomy_config models exactly this via
    `execution_costs.stop_gap_atr_fraction` (0.5) and book_risk spends heat budget
    on the number, but NOTHING had ever compared a realized fill against it. An
    assumption that is never confronted with a fill is a guess wearing a config
    key's clothes.

    Inline here so a forced exit is graded the moment it happens; the ledger sweep
    in post_exit_runners.sweep_exit_grades is the safety net that catches this
    exit anyway if this call fails, plus every exit placed by another path.

    Fail-soft to the point of silence about its own failure only in the
    unreachable case: a grading error prints and moves on. EXITS REDUCE RISK AND
    ARE SACRED — measurement never gets to interfere with one.
    """
    try:
        from tools.exit_grade import grade_exit
        from tools.post_exit_runners import record_exit_grade
        row = dict(fill or {})
        # The order dict carries the plan; the fill is built as {**order, ...} by
        # both brokers, but readback() is the only thing that guarantees it, so
        # merge defensively rather than assume.
        for k in ("plan_stop_loss", "plan_target_price", "plan_holding_horizon_days",
                  "trailing_stop", "exit_plan_snapshot", "forced_exit_reason",
                  "stop_loss", "atr_pct", "reference_price"):
            if row.get(k) is None and (order or {}).get(k) is not None:
                row[k] = order[k]
        if not isinstance(row.get("entry_plan"), dict) and \
                isinstance(row.get("exit_plan_snapshot"), dict):
            row["entry_plan"] = row["exit_plan_snapshot"]
        row.setdefault("forced", True)
        row.setdefault("days_held", ex.get("days_held"))
        g = grade_exit(row, autopsy=autopsy, atr_pct=ex.get("atr_pct"))
        record_exit_grade(g)
        if g.get("headline"):
            print(f"  EXIT GRADE {g['headline']}")
    except Exception as e:
        print(f"  (exit grade skipped: {e})")
