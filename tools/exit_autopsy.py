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
