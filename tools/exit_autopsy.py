"""Exit Autopsy — durable lesson store for closed positions.

When a position closes (SELL_TO_CLOSE fill or forced exit), the system writes
a structured "system_stub" autopsy immediately (no LLM required). Later the
brain can grade the exit; ungraded stubs surface in brain_facing_exit_lessons
so the next run can learn from recent closes.

Wire points (orchestrator / exit_guard import these — do not call from here):
  * On forced exit / SELL_TO_CLOSE fill: build_exit_autopsy_from_fill(...) then
    append_exit_autopsy(record). Prefer portfolio_pos_before snapshot so
    days_held / entry_plan survive after the lot is removed.
  * gather_context: include brain_facing_exit_lessons() in the context bundle
    (alongside lessons_learned / track_record) so the brain sees recent closes.

Storage: journal/exit_autopsies/YYYY-MM-DD.jsonl (date-partitioned, append-only).

All public APIs are fail-soft and never raise.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTOPSY_DIR = ROOT / "journal" / "exit_autopsies"


def _safe_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _days_held_from(opened_at, closed_at) -> int | None:
    """Calendar days between open and close timestamps; None if unparseable."""
    try:
        if not opened_at or not closed_at:
            return None
        o = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        c = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        return max(0, (c.date() - o.date()).days)
    except Exception:
        return None


def build_exit_autopsy_from_fill(
    fill: dict,
    order: dict | None,
    portfolio_pos_before: dict | None,
    *,
    forced: bool,
    reason: str | None,
) -> dict:
    """Structured system stub written at close time (no LLM).

    Fields: ticker, action, fill_price, quantity, days_held, entry_plan,
    forced, forced_reason, realized_pnl_usd (if computable), status, needs_brain_grade.
    """
    try:
        fill = fill if isinstance(fill, dict) else {}
        order = order if isinstance(order, dict) else {}
        pos = portfolio_pos_before if isinstance(portfolio_pos_before, dict) else {}

        ticker = str(
            fill.get("ticker") or order.get("ticker") or pos.get("ticker") or ""
        ).upper()
        action = str(fill.get("action") or order.get("action") or "SELL_TO_CLOSE").upper()
        fill_price = _safe_float(fill.get("fill_price"))
        quantity = _safe_float(fill.get("quantity"))

        # Prefer fill-stamped plan (survives after lot removal), then position snapshot.
        entry_plan = (
            fill.get("entry_plan")
            or pos.get("plan")
            or pos.get("original_plan")
            or None
        )
        if entry_plan is not None and not isinstance(entry_plan, dict):
            entry_plan = None

        days_held = pos.get("days_held")
        if days_held is not None:
            try:
                days_held = int(days_held)
            except (TypeError, ValueError):
                days_held = None
        if days_held is None:
            days_held = _days_held_from(
                fill.get("position_opened_at") or pos.get("opened_at"),
                fill.get("filled_at") or datetime.now(timezone.utc).isoformat(),
            )

        forced_reason = reason
        if forced_reason is None and forced:
            forced_reason = (
                order.get("forced_exit_reason")
                or fill.get("forced_exit_reason")
                or None
            )

        realized = fill.get("realized_pnl_usd")
        if realized is None:
            # Fallback: price P&L from fill + avg_cost if both present.
            avg = _safe_float(fill.get("avg_cost") or pos.get("avg_cost"))
            if fill_price is not None and quantity is not None and avg is not None:
                realized = round((fill_price - avg) * quantity, 2)
        else:
            realized = _safe_float(realized)

        return {
            "ticker": ticker,
            "action": action,
            "fill_price": fill_price,
            "quantity": quantity,
            "days_held": days_held,
            "entry_plan": entry_plan,
            "forced": bool(forced),
            "forced_reason": forced_reason,
            "realized_pnl_usd": realized,
            "status": "system_stub",
            "needs_brain_grade": True,
            # Extras for later grading / joins (harmless if unused).
            "avg_cost": _safe_float(fill.get("avg_cost") or pos.get("avg_cost")),
            "position_opened_at": fill.get("position_opened_at") or pos.get("opened_at"),
            "filled_at": fill.get("filled_at"),
            "proposal_id": fill.get("proposal_id") or order.get("proposal_id"),
            "entry_proposal_id": fill.get("entry_proposal_id") or pos.get("proposal_id"),
            "sell_fraction": fill.get("sell_fraction"),
        }
    except Exception:
        return {
            "ticker": "",
            "action": "SELL_TO_CLOSE",
            "fill_price": None,
            "quantity": None,
            "days_held": None,
            "entry_plan": None,
            "forced": bool(forced),
            "forced_reason": reason,
            "realized_pnl_usd": None,
            "status": "system_stub",
            "needs_brain_grade": True,
            "error": "build_failed",
        }


def append_exit_autopsy(record: dict) -> Path:
    """Append one autopsy record as JSONL under journal/exit_autopsies/YYYY-MM-DD.jsonl.

    Returns the path written (or a best-effort path on failure). Never raises.
    """
    try:
        rec = dict(record) if isinstance(record, dict) else {"raw": str(record)}
        ts = rec.get("ts") or datetime.now(timezone.utc).isoformat()
        rec = {"ts": ts, **{k: v for k, v in rec.items() if k != "ts"}}
        day = str(ts)[:10]
        path = AUTOPSY_DIR / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return path
    except Exception:
        try:
            day = datetime.now(timezone.utc).date().isoformat()
            return AUTOPSY_DIR / f"{day}.jsonl"
        except Exception:
            return AUTOPSY_DIR / "unknown.jsonl"


def recent_exit_autopsies(limit: int = 15) -> list[dict]:
    """Newest-first list of autopsy records from journal/exit_autopsies/*.jsonl."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 15
    if limit == 0:
        return []
    rows: list[dict] = []
    try:
        if not AUTOPSY_DIR.exists():
            return []
        # Newest day files first; within a file, later lines are newer.
        files = sorted(AUTOPSY_DIR.glob("*.jsonl"), reverse=True)
        for f in files:
            try:
                lines = f.read_text().splitlines()
            except OSError:
                continue
            day_rows: list[dict] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    day_rows.append(rec)
            # reverse within day so last-written (newest) comes first
            rows.extend(reversed(day_rows))
            if len(rows) >= limit:
                break
        return rows[:limit]
    except Exception:
        return []


def _compact_autopsy(rec: dict) -> dict:
    """Brain-facing compact row — keep the lesson signal, drop noise."""
    return {
        "ts": rec.get("ts"),
        "ticker": rec.get("ticker"),
        "action": rec.get("action"),
        "fill_price": rec.get("fill_price"),
        "quantity": rec.get("quantity"),
        "days_held": rec.get("days_held"),
        "realized_pnl_usd": rec.get("realized_pnl_usd"),
        "avg_cost": rec.get("avg_cost"),
        "forced": rec.get("forced"),
        "forced_reason": rec.get("forced_reason"),
        "status": rec.get("status"),
        "needs_brain_grade": rec.get("needs_brain_grade"),
        "entry_plan": rec.get("entry_plan"),
        "brain_grade": rec.get("brain_grade"),
        "lesson": rec.get("lesson"),
        "post_exit_track": rec.get("post_exit_track"),
    }


def grade_exit_autopsy(record: dict) -> dict:
    """Deterministic grade for a system_stub autopsy (no LLM).

    Returns grade fields to merge into the record + a binding lesson string.
    """
    rec = record if isinstance(record, dict) else {}
    forced = bool(rec.get("forced"))
    reason = str(rec.get("forced_reason") or rec.get("brain_exit_thesis") or "")
    pnl = rec.get("realized_pnl_usd")
    try:
        pnl_f = float(pnl) if pnl is not None else None
    except (TypeError, ValueError):
        pnl_f = None
    days = rec.get("days_held")
    try:
        days_i = int(days) if days is not None else None
    except (TypeError, ValueError):
        days_i = None
    plan = rec.get("entry_plan") if isinstance(rec.get("entry_plan"), dict) else {}

    tags: list[str] = []
    process = "mixed"
    lesson = ""

    if forced and str(reason).startswith("stop"):
        tags.append("stop_exit")
        if pnl_f is not None and pnl_f < 0:
            # Stop loss working as designed is process_win if stop was outside noise
            # We don't have ATR here — mark as process_win_risk_managed if plan had stop
            if plan.get("stop_loss") is not None:
                process = "process_win"
                lesson = (
                    f"{rec.get('ticker')}: stop enforced as planned — risk management "
                    f"worked (loss ${pnl_f:.0f}). Review if stop was inside noise band."
                )
            else:
                process = "process_fail"
                lesson = (
                    f"{rec.get('ticker')}: stop exit without a recorded plan stop — "
                    "plan integrity failure."
                )
        else:
            process = "mixed"
            lesson = f"{rec.get('ticker')}: forced stop with non-negative PnL — check fill."
    elif forced and "horizon" in str(reason).lower():
        tags.append("horizon_exit")
        if pnl_f is not None and abs(pnl_f) < 50 and days_i and days_i >= 20:
            process = "process_fail"
            tags.append("stalled_capital")
            lesson = (
                f"{rec.get('ticker')}: horizon expired with flat P&L — capital was "
                "parked; rotate earlier next time (stall discipline)."
            )
        elif pnl_f is not None and pnl_f > 0:
            process = "process_win"
            lesson = (
                f"{rec.get('ticker')}: held to horizon with profit — thesis played out "
                "inside swing window."
            )
        else:
            process = "mixed"
            lesson = f"{rec.get('ticker')}: horizon exit — re-check entry quality next time."
    elif not forced:
        tags.append("discretionary_exit")
        if pnl_f is not None and pnl_f > 0:
            process = "process_win"
            lesson = (
                f"{rec.get('ticker')}: discretionary winner — document which pillar "
                "worked for pattern library."
            )
        elif pnl_f is not None and pnl_f < 0:
            process = "process_fail"
            tags.append("discretionary_loss")
            lesson = (
                f"{rec.get('ticker')}: discretionary loss — was an invalidator hit, or "
                "did conviction crack without thesis break?"
            )
        else:
            process = "mixed"
            lesson = f"{rec.get('ticker')}: flat discretionary exit."
    else:
        tags.append("forced_other")
        lesson = f"{rec.get('ticker')}: forced exit ({reason}) — review."

    # Invalidators present on plan?
    inv = plan.get("thesis_invalidators") if isinstance(plan, dict) else None
    if isinstance(inv, dict) and any(inv.values()):
        tags.append("had_invalidators")
    else:
        tags.append("missing_invalidators_on_plan")

    return {
        "status": "graded",
        "needs_brain_grade": False,
        "brain_grade": {
            "process": process,  # process_win | process_fail | mixed
            "tags": tags,
            "graded_by": "deterministic_v1",
            "graded_at": datetime.now(timezone.utc).isoformat(),
        },
        "lesson": lesson[:400],
        "binding": process == "process_fail",  # fails become binding cautions
    }


def grade_and_persist_autopsy(record: dict) -> dict:
    """Grade a stub, append graded copy, write concept lesson + binding store.

    Also registers post-exit runner tracking (15/30/60d leftover-gain learning)
    so the agent learns to let winners run via partials/trails without undoing
    good risk management.
    """
    try:
        rec = dict(record) if isinstance(record, dict) else {}
        grade = grade_exit_autopsy(rec)
        merged = {**rec, **grade}
        # Tag winners for hold-learning
        try:
            avg = _safe_float(merged.get("avg_cost"))
            fill = _safe_float(merged.get("fill_price"))
            if avg and fill and fill > avg:
                tags = list((merged.get("brain_grade") or {}).get("tags") or [])
                if "winner_exit" not in tags:
                    tags.append("winner_exit")
                merged.setdefault("brain_grade", {})["tags"] = tags
                merged["post_exit_track"] = "registered"
        except Exception:
            pass
        append_exit_autopsy(merged)
        # Concept memory lesson
        try:
            from tools.concept_memory import append_lesson
            if merged.get("lesson") and merged.get("ticker"):
                append_lesson(
                    merged["ticker"], merged["lesson"],
                    source="exit_grade",
                    tags=(merged.get("brain_grade") or {}).get("tags") or [],
                )
        except Exception:
            pass
        # Binding lessons file
        if grade.get("binding") and merged.get("lesson"):
            _append_binding_lesson(merged)
        # Post-exit runner: mark path 15/30/60d after exit (let winners run study)
        try:
            from tools.post_exit_runners import register_from_exit
            register_from_exit(merged)
        except Exception:
            pass
        return merged
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150]}


# --------------------------------------------------------------------------- #
# TARGET ATTAINMENT — grading the claim behind the 2:1 gate
#
# min_risk_reward_ratio (2.0) is the most rigorously justified rule in the
# system: at realistic 40-55% win rates a 1:1 book loses money and 2:1 stays
# profitable across the whole band. But the ratio is computed from the model's
# own `target_price`, and `target_price` appears ZERO times in
# execution/exit_guard.py — exits are stop, trail or horizon only, and CLAUDE.md
# calls the target "a milestone, not a tripwire". So the gate filtered on a
# number nothing enforced and nothing graded, which the model could always clear
# by writing a bigger one. That is an unfalsifiable claim.
#
# The fix is NOT to make targets binding on exits. Letting winners run is correct
# and well supported, and turning the target into a tripwire would cap every
# trend trade at its first milestone. The fix is to make the claim FALSIFIABLE:
# grade claimed RR against REALIZED RR on every close, and let the calibration
# gate act on the record — only once there is a record.
# --------------------------------------------------------------------------- #

# Capture fraction at or above which the trade essentially got what it claimed.
NEAR_TARGET_CAPTURE = 0.8


def grade_target_attainment(entry_price=None, exit_price=None, stop_loss=None,
                            target_price=None, high_during_hold=None) -> dict:
    """Claimed RR at entry vs REALIZED RR at exit, on the SAME risk unit. Pure.

    Both ratios are divided by the identical entry-to-stop distance, so they are
    directly comparable; dividing the realized move by anything else would let a
    trade look good against a risk it never took.

    target_reached is True / False / None, and the None is load-bearing: None
    means the path was UNOBSERVABLE (no daily highs, and the exit itself does not
    prove it), False means we looked and it did not happen. Averaging those
    together manufactures a hit rate out of missing data. An exit at or above the
    target proves attainment without any bars at all.

    A non-positive risk unit (stop at or above entry) yields None rather than a
    confident nonsense ratio, and a missing plan is ungradeable rather than zero.
    """
    e = _safe_float(entry_price)
    x = _safe_float(exit_price)
    s = _safe_float(stop_loss)
    tp = _safe_float(target_price)
    hi = _safe_float(high_during_hold)

    risk = (e - s) if (e is not None and s is not None) else None
    reward = (tp - e) if (e is not None and tp is not None) else None
    bad = []
    if e is None or x is None:
        bad.append("missing_entry_or_exit")
    if s is None or tp is None:
        bad.append("missing_plan")            # no stop/target recovered
    if risk is not None and risk <= 0:
        bad.append("non_positive_risk_unit")  # stop at or above entry
    if reward is not None and reward <= 0:
        bad.append("non_positive_reward")

    gradeable = not bad
    out = {
        "gradeable": gradeable,
        "entry_price": e, "exit_price": x,
        "stop_loss": s, "target_price": tp,
        "risk_unit": round(risk, 4) if (gradeable and risk is not None) else None,
        "claimed_rr": None, "realized_rr": None,
        "capture_fraction": None, "target_reached": None, "near_target": False,
    }
    if not gradeable:
        out["ungradeable_reason"] = ",".join(bad)
        return out

    out["claimed_rr"] = round(reward / risk, 2)
    out["realized_rr"] = round((x - e) / risk, 2)
    out["capture_fraction"] = round((x - e) / reward, 4)
    if x >= tp:
        out["target_reached"] = True          # the exit itself is proof
    elif hi is not None:
        out["target_reached"] = bool(hi >= tp)
        out["high_during_hold"] = hi
    else:
        out["target_reached"] = None          # could not look — NOT False
    out["near_target"] = bool(out["capture_fraction"] >= NEAR_TARGET_CAPTURE)
    return out


def _buy_plan_lookup() -> list:
    """Every BUY proposal as (ts, TICKER, {stop_loss, target_price, rr, conf}).

    Mirrors performance_breakdown._confidence_lookup: closed trades do not always
    carry the entry plan (the lot is gone by then), so the ORIGINAL proposal is
    where the claim lives. Grading the claim requires recovering it. Fail-soft.
    """
    rows: list = []
    proposals_dir = ROOT / "journal" / "proposals"
    if not proposals_dir.exists():
        return rows
    try:
        for f in sorted(proposals_dir.glob("*.jsonl")):
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = rec.get("proposal") if isinstance(rec, dict) else None
                if not isinstance(p, dict):
                    continue
                if str(p.get("action", "")).upper() != "BUY" or not p.get("ticker"):
                    continue
                rows.append((rec.get("ts", ""), str(p["ticker"]).upper(), {
                    "stop_loss": _safe_float(p.get("stop_loss")),
                    "target_price": _safe_float(p.get("target_price")),
                    "claimed_risk_reward_ratio": _safe_float(p.get("risk_reward_ratio")),
                    "confidence": _safe_float(p.get("confidence")),
                }))
    except Exception:
        return rows
    return rows


def _plan_for_trade(trade: dict, plans: list) -> dict:
    """Latest BUY plan for this ticker on or before the trade's opened_at."""
    ticker = str(trade.get("ticker", "")).upper()
    opened = str(trade.get("opened_at") or "")[:10]
    if not ticker or not opened:
        return {}
    best_ts, best = "", {}
    for ts, t, plan in plans:
        if t == ticker and str(ts)[:10] <= opened and str(ts) >= best_ts:
            best_ts, best = str(ts), plan
    return best


def _highs_during_holds(trades: list) -> dict:
    """{(ticker, opened, closed): high} from real daily bars, one batched fetch.

    Without true daily highs, target_reached collapses to "did the EXIT clear the
    target", which under-counts every trade that touched its target and gave it
    back. Fail-soft to {} — the grade then reports None, not False.
    """
    try:
        from tools.benchmark import daily_ohlc_batch, window_extremes
        rows = [t for t in trades if t.get("ticker") and t.get("opened_at")
                and t.get("closed_at")]
        if not rows:
            return {}
        days = [str(t["opened_at"])[:10] for t in rows] + \
               [str(t["closed_at"])[:10] for t in rows]
        bars = daily_ohlc_batch(sorted({str(t["ticker"]).upper() for t in rows}),
                                min(days), max(days))
        out = {}
        for t in rows:
            key = (str(t["ticker"]).upper(), str(t["opened_at"])[:10],
                   str(t["closed_at"])[:10])
            ext = window_extremes(bars.get(key[0]), key[1], key[2])
            if ext.get("n_bars"):
                out[key] = ext.get("high")
        return out
    except Exception:
        return {}


def build_target_calibration(closed_trades: list, *, fetch_bars: bool = True,
                             min_trades_for_binding: int = 15) -> dict:
    """Aggregate target attainment across closed trades.

    Publishes `rr_inflation` (mean claimed RR minus mean realized RR) ONLY once
    the sample is binding. Below that the same arithmetic is published as
    `provisional_rr_inflation`, clearly labelled, because a binding-grade number
    at n=2 is exactly the failure this whole layer exists to prevent — the point
    of grading a claim is that you do not act on the grade before you have one.
    """
    try:
        trades = [t for t in (closed_trades or []) if isinstance(t, dict)]
        plans = _buy_plan_lookup()
        highs = _highs_during_holds(trades) if fetch_bars else {}

        rows = []
        for t in trades:
            plan = _plan_for_trade(t, plans) or {}
            stop = (_safe_float(t.get("stop_loss"))
                    or _safe_float((t.get("entry_plan") or {}).get("stop_loss")
                                   if isinstance(t.get("entry_plan"), dict) else None)
                    or plan.get("stop_loss"))
            target = (_safe_float(t.get("target_price"))
                      or _safe_float((t.get("entry_plan") or {}).get("target_price")
                                     if isinstance(t.get("entry_plan"), dict) else None)
                      or plan.get("target_price"))
            key = (str(t.get("ticker", "")).upper(),
                   str(t.get("opened_at") or "")[:10],
                   str(t.get("closed_at") or "")[:10])
            high = _safe_float(t.get("high_during_hold"))
            if high is None:
                high = highs.get(key)
            g = grade_target_attainment(
                entry_price=t.get("entry_price"), exit_price=t.get("exit_price"),
                stop_loss=stop, target_price=target, high_during_hold=high)
            rows.append({"ticker": key[0], "opened_at": key[1],
                         "closed_at": key[2],
                         "claimed_rr_stated": plan.get("claimed_risk_reward_ratio"),
                         **g})

        graded = [r for r in rows if r.get("gradeable")]
        n = len(graded)
        binding = n >= int(min_trades_for_binding or 15)
        phase = ("binding" if binding else "caution" if n >= 5 else "anecdote")

        def _mean(vals):
            vals = [v for v in vals if isinstance(v, (int, float))]
            return round(sum(vals) / len(vals), 2) if vals else None

        mean_claimed = _mean([r["claimed_rr"] for r in graded])
        mean_realized = _mean([r["realized_rr"] for r in graded])
        inflation = (round(mean_claimed - mean_realized, 2)
                     if mean_claimed is not None and mean_realized is not None
                     else None)
        # target_reached is scored over the OBSERVED subset only; rows where the
        # path was unobservable are excluded rather than counted as misses.
        observed = [r for r in graded if r.get("target_reached") is not None]

        return {
            "note": ("Grades the claim behind min_risk_reward_ratio. Targets stay "
                     "NON-BINDING for exits (letting winners run is correct); this "
                     "measures whether the claimed RR was ever real. rr_inflation is "
                     "WITHHELD until the sample binds."),
            "n_trades": len(rows),
            "n_gradeable": n,
            "n_ungradeable": len(rows) - n,
            "phase": phase,
            "binding": binding,
            "sufficient_sample": binding,
            "min_trades_for_binding": int(min_trades_for_binding or 15),
            "mean_claimed_rr": mean_claimed,
            "mean_realized_rr": mean_realized,
            "rr_inflation": inflation if binding else None,
            "provisional_rr_inflation": None if binding else inflation,
            "mean_capture_fraction": _mean([r["capture_fraction"] for r in graded]),
            "n_target_path_observed": len(observed),
            "n_target_reached": sum(1 for r in observed if r.get("target_reached")),
            "target_hit_rate_pct": (round(100 * sum(1 for r in observed
                                                    if r.get("target_reached"))
                                          / len(observed), 1)
                                    if binding and observed else None),
            "trades": rows[-25:],
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:200], "n_gradeable": 0,
                "binding": False, "phase": "anecdote", "rr_inflation": None,
                "sufficient_sample": False}


BINDING_FILE = ROOT / "data" / "binding_exit_lessons.json"


def _append_binding_lesson(rec: dict) -> None:
    try:
        data = {"lessons": []}
        if BINDING_FILE.exists():
            data = json.loads(BINDING_FILE.read_text())
        lessons = data.get("lessons") if isinstance(data, dict) else []
        if not isinstance(lessons, list):
            lessons = []
        lessons.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": rec.get("ticker"),
            "lesson": rec.get("lesson"),
            "process": (rec.get("brain_grade") or {}).get("process"),
            "tags": (rec.get("brain_grade") or {}).get("tags"),
            "realized_pnl_usd": rec.get("realized_pnl_usd"),
        })
        BINDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        BINDING_FILE.write_text(json.dumps({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "lessons": lessons[-50:],
        }, indent=2))
    except Exception:
        pass


def load_binding_exit_lessons(limit: int = 15) -> list[dict]:
    try:
        if not BINDING_FILE.exists():
            return []
        data = json.loads(BINDING_FILE.read_text())
        lessons = data.get("lessons") or []
        return list(reversed(lessons))[:limit]
    except Exception:
        return []


def brain_facing_exit_lessons(limit: int = 10) -> dict:
    """Compact recent exit autopsies for the context bundle.

    Shape: {"note": "...", "recent": [...], "ungraded_count": N, "binding_lessons": [...]}
    """
    try:
        recent = recent_exit_autopsies(limit=limit)
        compact = [_compact_autopsy(r) for r in recent]
        wider = recent_exit_autopsies(limit=max(limit, 50))
        ungraded = sum(
            1 for r in wider
            if r.get("needs_brain_grade") is True
            or (r.get("status") == "system_stub" and not r.get("brain_grade"))
        )
        binding = load_binding_exit_lessons(12)
        # Let-winners-run study (15/30/60d after exit)
        try:
            from tools.post_exit_runners import brain_facing_runner_learning
            runner = brain_facing_runner_learning(12)
        except Exception:
            runner = {"status": "unavailable"}
        return {
            "note": (
                "Recent position closes. Graded rows carry process_win/fail tags and "
                "lessons. BINDING lessons are process failures you must not repeat "
                "without an explicit exception. "
                "RUNNER LEARNING (post_exit): after winners, we mark the stock 15/30/60d — "
                "left_on_table means more upside after you sold (learn partials/trails); "
                "good_lock_in means price faded (banking was right). Process win and "
                "left_on_table can both be true."
            ),
            "recent": compact,
            "ungraded_count": ungraded,
            "binding_lessons": binding,
            "n_binding": len(binding),
            "runner_learning": runner,
        }
    except Exception:
        return {
            "note": "exit lessons unavailable",
            "recent": [],
            "ungraded_count": 0,
            "binding_lessons": [],
            "n_binding": 0,
            "runner_learning": {},
        }
