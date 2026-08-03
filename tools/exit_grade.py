"""Exit grading — the plan an exit was judged against, and what it actually cost.

WHY THIS EXISTS (audited 2026-08-03)
------------------------------------
Three trades, two closes, and exit quality was ungraded end to end. Entries carry
a validator, a risk desk, a stop-engineering floor and a 2:1 gate; the exit — the
half of the trade that decides what the book actually keeps — carried a
deterministic process_win/fail tag and nothing else. Three measured defects, all
on the live record:

  * `state/portfolio.json` history, HPE 2026-07-15: `stop_loss: None` and
    `forced_exit_reason: None` on the ledger row. Nothing on that row says what
    plan the exit was judged against or whether a human-equivalent judgement or a
    mechanical rule closed it. The DELL row two lines above it carries both. The
    same close therefore grades on one name and is unfalsifiable on the other,
    for no reason other than which code path placed the order.

  * DELL 2026-07-15: plan stop 408.00, FILLED AT 389.7542. That is 18.2458 per
    share, 4.47% of the stop level, 0.55 ATR THROUGH the level that was supposed
    to cap the loss. A position sized for 1% risk realized -13.5% — 1.43x the
    risk it was sized against. `autonomy_config.execution_costs` models this
    exact behaviour (`stop_gap_atr_fraction: 0.5`) and book_risk spends real heat
    budget on the assumption. NOTHING compared the realized gap against the
    modelled one on a single exit, so the assumption was never once tested by the
    thing it claims to describe.

  * The claimed risk/reward that got the trade past the 2:1 gate was never put
    next to the realized R on the exit record. DELL claimed +1.74R and realized
    -1.43R; HPE claimed +1.80R and realized -0.54R.

WHAT THIS MODULE IS
-------------------
PURE computation. No IO except an optional config read for the gap model, and
that is injectable. Persistence, the ledger sweep and the brain-facing surface
live in tools/post_exit_runners.py — which already owns the post-exit study store
and already reaches the brain's context, so a grade written here arrives where
decisions are made instead of in a file nobody reads.

THE HONESTY CONSTRAINT THAT SHAPES THE WHOLE FILE
-------------------------------------------------
On the simulation backend the stop fill is PRODUCED BY the gap model
(`simulated_broker._sell_fill_price`), so "realized vs modelled" there is a check
on arithmetic, not evidence about markets. Every slippage grade therefore carries
`slippage_source`: `modelled` when the fill came out of our own model
(`gap_modeled: true` on the fill) and `realized` when a real broker filled it.
Aggregates count the two separately. Grading a simulated gap as though it were
market evidence is the same failure class as the fabricated HPE runner record of
2026-07-16: a loop reporting healthy while studying fiction.

Everything here is fail-soft and returns a record; nothing raises. A grading bug
must never touch an exit — exits reduce risk and are sacred.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Realized through-stop cost above this multiple of the MODELLED gap counts as
# the model having understated the true cost. 1.15 rather than 1.0 because the
# vol-scaled sell slippage legitimately stacks on top of the gap leg (see
# _sell_fill_price) and a few percent of that is not a model miss.
GAP_MODEL_TOLERANCE = 1.15
# Realized loss above this multiple of planned risk means the stop did not hold
# the line the position was sized against. Mirrors exit_autopsy's own threshold
# so the two graders cannot disagree about the same trade.
OVER_RISK_MULTIPLE = 1.25

# Fallback gap model, used only when autonomy_config cannot be read. Same values
# as the config so a config read failure degrades to today's behaviour rather
# than to a silently different assumption.
DEFAULT_GAP_MODEL = {
    "model_stop_gaps": True,
    "stop_gap_atr_fraction": 0.5,
    "slippage_bps": 10.0,
    "slippage_atr_fraction": 0.05,
}


def _f(v):
    """float or None. Never raises, never coerces None to 0.0 — a missing number
    and a zero are different claims and this whole layer exists because they got
    conflated."""
    if v is None:
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN -> None


def _s(v) -> str:
    return "" if v is None else str(v)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_gap_model(config: dict | None = None, *, root: Path | None = None) -> dict:
    """execution_costs values the grade is measured against. Injectable for tests.

    Reads the LIVE config rather than a constant, because the number being graded
    is the number the simulator and book_risk actually use. A hard-coded copy here
    would drift and the grade would quietly start measuring the wrong assumption.
    """
    if isinstance(config, dict):
        costs = config.get("execution_costs") if "execution_costs" in config else config
        if isinstance(costs, dict):
            return {**DEFAULT_GAP_MODEL, **costs}
    try:
        base = Path(root) if root is not None else ROOT
        cfg = json.loads((base / "autonomy_config.json").read_text())
        costs = cfg.get("execution_costs")
        if isinstance(costs, dict):
            return {**DEFAULT_GAP_MODEL, **costs}
    except Exception:
        pass
    return dict(DEFAULT_GAP_MODEL)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def exit_key(row: dict) -> str:
    """Stable identity for one exit, so a sweep can be re-run without duplicating.

    order_id first because it is unique per broker order; the ticker+timestamp
    fallback covers legacy rows and hand-reconciled fills. A partial exit gets its
    own key by construction (its own order), which is correct: selling half is its
    own decision and deserves its own grade.
    """
    row = row if isinstance(row, dict) else {}
    oid = _s(row.get("order_id") or row.get("client_order_id")).strip()
    tkr = _s(row.get("ticker")).upper()
    ts = _s(row.get("filled_at") or row.get("ts") or row.get("submitted_at"))
    if oid:
        return f"{tkr}|{oid}"
    return f"{tkr}|{ts}"


# --------------------------------------------------------------------------- #
# The two facts an exit record must carry to be gradeable at all
# --------------------------------------------------------------------------- #
def classify_exit_reason(row: dict, autopsy: dict | None = None) -> dict:
    """Was this exit FORCED (and by which rule) or DISCRETIONARY, and why.

    THE DEFECT (2026-08-03). The HPE ledger row has no `forced_exit_reason` and no
    top-level stop, so read on its own it is indistinguishable from a row whose
    reason was lost. The narrative was not gone — it sat in the exit autopsy as
    `brain_exit_thesis`, four fields away — but no consumer joined the two, so the
    close read as unattributable. `kind` is never left blank: an exit we genuinely
    cannot classify says `unknown` out loud rather than defaulting to
    discretionary, because defaulting would quietly move every lost forced exit
    into the bucket that grades hardest.
    """
    row = row if isinstance(row, dict) else {}
    ap = autopsy if isinstance(autopsy, dict) else {}

    reason = _s(row.get("forced_exit_reason") or ap.get("forced_reason")).strip()
    forced_flag = row.get("forced")
    if forced_flag is None:
        forced_flag = ap.get("forced")

    low = reason.lower()
    if low.startswith("trailing_stop"):
        kind = "forced_trailing_stop"
    elif low.startswith("stop"):
        kind = "forced_stop"
    elif "horizon" in low:
        kind = "forced_horizon"
    elif reason and bool(forced_flag):
        kind = "forced_other"
    elif forced_flag is True:
        kind = "forced_other"
    elif forced_flag is False or ap.get("brain_exit_thesis") or ap.get("action"):
        kind = "discretionary"
    else:
        kind = "unknown"

    text = reason
    if kind in ("discretionary", "unknown") and not text:
        text = _s(ap.get("brain_exit_thesis") or row.get("exit_thesis")).strip()
    return {
        "exit_reason_kind": kind,
        "exit_reason": text[:400] or None,
        "is_stop_exit": kind in ("forced_stop", "forced_trailing_stop"),
        "forced": kind.startswith("forced"),
    }


_PLAN_FIELDS = ("stop_loss", "target_price", "holding_horizon_days",
                "entry_price_max", "confidence")


def recover_entry_plan(row: dict, autopsy: dict | None = None,
                       fallback_plan: dict | None = None) -> dict:
    """The plan this exit is judged against, with its PROVENANCE stated.

    Order of preference is authority order: the plan stamped on the fill by the
    broker (it came off the position itself and cannot have been overwritten by a
    later proposal), then the autopsy's copy, then the position/order plan, then
    the journal's BUY proposal. `plan_source` travels with the answer so a later
    audit can tell a plan that survived execution from one reconstructed after the
    fact — reconstruction is legitimate but it is weaker evidence, and a grade
    that hides which one it used is the unfalsifiable branch this system already
    got burned by once (exit_autopsy.grade_exit_autopsy, 2026-07-19).
    """
    row = row if isinstance(row, dict) else {}
    ap = autopsy if isinstance(autopsy, dict) else {}
    candidates = (
        ("fill_entry_plan", row.get("entry_plan")),
        ("autopsy_entry_plan", ap.get("entry_plan")),
        ("order_plan", row.get("plan")),
        ("proposal_journal", fallback_plan),
    )
    for source, cand in candidates:
        if not isinstance(cand, dict):
            continue
        plan = {k: _f(cand.get(k)) if k != "holding_horizon_days" else cand.get(k)
                for k in _PLAN_FIELDS}
        if plan.get("stop_loss") is not None or plan.get("target_price") is not None:
            plan["thesis_invalidators"] = cand.get("thesis_invalidators")
            return {"plan": plan, "plan_source": source}
    return {"plan": {k: None for k in _PLAN_FIELDS}, "plan_source": "none"}


# --------------------------------------------------------------------------- #
# Stop slippage — the thing autonomy_config models and nothing measured
# --------------------------------------------------------------------------- #
def grade_stop_slippage(*, stop_level, fill_price, atr_pct=None,
                        reference_price=None, quantity=None,
                        effective_slippage_pct=None, gap_modeled=False,
                        model: dict | None = None) -> dict:
    """How far through the stop the fill actually landed, in $ and in ATR.

    THE MEASUREMENT (DELL, 2026-07-15). Plan stop 408.00, fill 389.7542:

        through the stop   18.2458/share = 4.47% of the stop level
        ATR                8.3% of the 400.52 reference = 33.24/share
        realized           0.549 ATR through
        modelled           0.500 ATR through (stop_gap_atr_fraction)

    So the model was ~10% light — not because the gap fraction is wrong, but
    because the vol-scaled SELL SLIPPAGE stacks on top of the gap leg while
    book_risk's heat adjustment reasons about the gap fraction alone. That is a
    small, specific, checkable finding, and it is the kind this system could not
    produce at all before: `stop_gap_atr_fraction` was an assumption that had
    never been confronted with a fill.

    ATR is applied to `reference_price` when present because that is the base
    `_sell_fill_price` itself uses; falling back to the stop level keeps the
    number comparable rather than refusing to answer. Every derived field is None
    — never 0 — when its input is missing, so "we did not measure" can never be
    read downstream as "there was no slippage".
    """
    model = model if isinstance(model, dict) else load_gap_model()
    stop = _f(stop_level)
    fill = _f(fill_price)
    atrp = _f(atr_pct)
    ref = _f(reference_price)
    qty = _f(quantity)

    out: dict = {
        "applicable": True,
        "stop_level": stop,
        "fill_price": fill,
        "reference_price": ref,
        "atr_pct": atrp,
        # `modelled` = our own simulator produced this fill from the gap model, so
        # it is arithmetic, not market evidence. Counted separately everywhere.
        "slippage_source": "modelled" if gap_modeled else "realized",
        "gap_usd_per_share": None,
        "gap_pct_of_stop": None,
        "gap_usd_total": None,
        "atr_usd": None,
        "gap_in_atr": None,
        "modelled_gap_atr_fraction": _f(model.get("stop_gap_atr_fraction")),
        "modelled_gap_usd_per_share": None,
        "modelled_fill_price": None,
        "excess_vs_model_usd_per_share": None,
        "excess_vs_model_ratio": None,
        "verdict": "unmeasurable",
    }
    if stop is None or fill is None or stop <= 0:
        out["verdict"] = "unmeasurable"
        out["note"] = "no stop level or no fill price on the record"
        return out

    gap = round(stop - fill, 4)                 # positive = filled BELOW the stop
    out["gap_usd_per_share"] = gap
    out["gap_pct_of_stop"] = round(gap / stop * 100.0, 4)
    if qty is not None:
        out["gap_usd_total"] = round(gap * qty, 4)

    atr_base = ref if (ref is not None and ref > 0) else stop
    out["atr_base"] = atr_base
    if atrp is not None and atrp > 0 and atr_base > 0:
        atr_usd = atrp / 100.0 * atr_base
        out["atr_usd"] = round(atr_usd, 4)
        if atr_usd > 0:
            out["gap_in_atr"] = round(gap / atr_usd, 4)
        frac = _f(model.get("stop_gap_atr_fraction"))
        if frac is not None:
            modelled_gap = frac * atr_usd
            out["modelled_gap_usd_per_share"] = round(modelled_gap, 4)
            slip = _f(effective_slippage_pct)
            modelled_fill = stop - modelled_gap
            if slip is not None:
                # The simulator applies sell slippage AFTER the gap; including it
                # here is what makes "worse than modelled" mean the model missed
                # something rather than that we forgot a leg it always charged.
                modelled_fill = modelled_fill * (1 - slip / 100.0)
            out["modelled_fill_price"] = round(modelled_fill, 4)
            modelled_total = round(stop - modelled_fill, 4)
            out["modelled_gap_with_slippage_usd_per_share"] = modelled_total
            out["excess_vs_model_usd_per_share"] = round(gap - modelled_total, 4)
            if modelled_total > 0:
                out["excess_vs_model_ratio"] = round(gap / modelled_total, 4)

    if gap <= 0:
        out["verdict"] = "filled_at_or_above_stop"
        out["note"] = ("Fill landed at or above the stop level — the stop held the "
                       "line it was sized against.")
    else:
        ratio = out.get("excess_vs_model_ratio")
        if ratio is None:
            out["verdict"] = "gap_unmodelled"
            out["note"] = ("Filled through the stop, but ATR was unavailable so the "
                           "gap could not be compared against "
                           "execution_costs.stop_gap_atr_fraction. Unverified is "
                           "NOT within-model.")
        elif ratio > GAP_MODEL_TOLERANCE:
            out["verdict"] = "gap_worse_than_model"
            out["note"] = (
                f"Filled {gap:.4f}/share ({out['gap_pct_of_stop']:.2f}% of the stop, "
                f"{out.get('gap_in_atr')} ATR) through the level — "
                f"{ratio:.2f}x the modelled through-stop cost. Portfolio heat is "
                f"gap-adjusted off stop_gap_atr_fraction; when this repeats, the "
                f"assumption is too generous and every position is sized larger "
                f"than its true risk.")
        else:
            out["verdict"] = "gap_within_model"
            out["note"] = (
                f"Filled {gap:.4f}/share ({out.get('gap_in_atr')} ATR) through the "
                f"stop, inside the modelled allowance "
                f"({out.get('modelled_gap_atr_fraction')} ATR + slippage).")
    if out["slippage_source"] == "modelled":
        out["disclosure"] = (
            "This fill was PRODUCED by simulated_broker's gap model, so this "
            "compares the model with itself. It is arithmetic, not evidence about "
            "how stops fill in the market.")
    return out


def not_a_stop_exit(kind: str) -> dict:
    return {"applicable": False,
            "reason": f"exit_reason_kind={kind or 'unknown'} — not a stop exit",
            "verdict": "not_applicable"}


# --------------------------------------------------------------------------- #
# Claimed vs realized R — grading the number that cleared the 2:1 gate
# --------------------------------------------------------------------------- #
def grade_r_multiple(*, entry_price, exit_price, stop_loss, target_price,
                     claimed_rr=None) -> dict:
    """Claimed R at entry vs realized R at exit, on the IDENTICAL risk unit.

    Both are divided by entry-to-stop, so they are directly comparable; dividing
    the realized move by anything else lets a trade look good against a risk it
    never took. Deliberately mirrors exit_autopsy.grade_target_attainment rather
    than inventing a second definition of R — two graders that disagree about the
    same trade are worse than one.

    `risk_used_fraction` is the piece that only makes sense on an exit: how much
    of the planned risk the trade actually spent. HPE 2026-07-15 exited at 0.54 —
    the brain closed at just over half the loss it had underwritten. That is a
    defensible decision and it is also a measurable habit, and neither could be
    said before because the exit carried no plan to measure against.
    """
    e, x = _f(entry_price), _f(exit_price)
    s, tp = _f(stop_loss), _f(target_price)
    out = {
        "gradeable": False, "entry_price": e, "exit_price": x,
        "stop_loss": s, "target_price": tp,
        "risk_unit_usd": None, "claimed_rr": None, "claimed_rr_stated": _f(claimed_rr),
        "realized_r": None, "r_gap": None, "risk_used_fraction": None,
        "capture_fraction": None, "ungradeable_reason": None,
    }
    missing = []
    if e is None or x is None:
        missing.append("missing_entry_or_exit")
    if s is None:
        missing.append("missing_stop")
    risk = (e - s) if (e is not None and s is not None) else None
    if risk is not None and risk <= 0:
        missing.append("non_positive_risk_unit")
    if missing:
        out["ungradeable_reason"] = ",".join(missing)
        return out

    out["gradeable"] = True
    out["risk_unit_usd"] = round(risk, 4)
    out["realized_r"] = round((x - e) / risk, 2)
    out["risk_used_fraction"] = round((e - x) / risk, 3)
    if tp is not None and tp > e:
        out["claimed_rr"] = round((tp - e) / risk, 2)
        out["capture_fraction"] = round((x - e) / (tp - e), 3)
        out["r_gap"] = round(out["claimed_rr"] - out["realized_r"], 2)
    return out


# --------------------------------------------------------------------------- #
# The whole record
# --------------------------------------------------------------------------- #
def grade_exit(row: dict, *, autopsy: dict | None = None,
               fallback_plan: dict | None = None, atr_pct=None,
               model: dict | None = None) -> dict:
    """One gradeable exit record: plan + reason + realized-vs-claimed R + slippage.

    `row` is a ledger SELL fill (state/portfolio.json history) or anything with the
    same shape. `autopsy` is the matching journal/exit_autopsies record when one
    exists — it supplies the discretionary narrative the ledger row does not carry.

    Tolerates old records by construction: every field it cannot recover comes back
    None and is named in `missing`, and the record still persists. A record that
    says what it could not measure is usable; one that omits the question is not.
    """
    try:
        row = row if isinstance(row, dict) else {}
        ap = autopsy if isinstance(autopsy, dict) else {}
        model = model if isinstance(model, dict) else load_gap_model()

        ticker = _s(row.get("ticker") or ap.get("ticker")).upper()
        reason = classify_exit_reason(row, ap)
        planned = recover_entry_plan(row, ap, fallback_plan)
        plan = planned["plan"]

        entry = _f(row.get("avg_cost")) or _f(ap.get("avg_cost"))
        fill = _f(row.get("fill_price")) or _f(ap.get("fill_price"))
        qty = _f(row.get("quantity")) or _f(ap.get("quantity"))
        ref = _f(row.get("reference_price"))
        pnl = _f(row.get("realized_pnl_usd"))
        if pnl is None:
            pnl = _f(ap.get("realized_pnl_usd"))
        days_held = ap.get("days_held") if ap.get("days_held") is not None \
            else row.get("days_held")
        atr = _f(atr_pct) if atr_pct is not None else _f(row.get("atr_pct"))
        fees = row.get("fees_usd") if isinstance(row.get("fees_usd"), dict) else {}
        slip_pct = _f(fees.get("effective_slippage_pct"))

        # The stop the exit was JUDGED AGAINST is the effective one — a trailing
        # stop that ratcheted above the plan stop is the level that actually
        # fired, and grading a trail exit against the plan stop would report a
        # gap that never happened. Both are kept; only the binding one is graded.
        plan_stop = plan.get("stop_loss")
        binding_stop = _f(row.get("stop_loss"))
        trail = _f(row.get("trailing_stop"))
        if binding_stop is None and reason["is_stop_exit"]:
            binding_stop = trail if trail is not None else plan_stop

        r = grade_r_multiple(
            entry_price=entry, exit_price=fill,
            stop_loss=plan_stop if plan_stop is not None else binding_stop,
            target_price=plan.get("target_price"),
            claimed_rr=(fallback_plan or {}).get("claimed_risk_reward_ratio"),
        )

        if reason["is_stop_exit"]:
            slippage = grade_stop_slippage(
                stop_level=binding_stop, fill_price=fill, atr_pct=atr,
                reference_price=ref, quantity=qty,
                effective_slippage_pct=slip_pct,
                gap_modeled=bool(row.get("gap_modeled")), model=model)
        else:
            slippage = not_a_stop_exit(reason["exit_reason_kind"])

        missing = []
        if plan.get("stop_loss") is None:
            missing.append("plan_stop_loss")
        if plan.get("target_price") is None:
            missing.append("plan_target_price")
        if plan.get("holding_horizon_days") is None:
            missing.append("holding_horizon_days")
        if reason["exit_reason_kind"] == "unknown":
            missing.append("exit_reason")
        if entry is None:
            missing.append("entry_price")
        if reason["is_stop_exit"] and atr is None:
            missing.append("atr_pct")

        grade = {
            "exit_key": exit_key(row),
            "ticker": ticker,
            "exit_date": _s(row.get("filled_at") or ap.get("filled_at") or
                            ap.get("ts"))[:10] or None,
            "exit_ts": row.get("filled_at") or ap.get("filled_at") or ap.get("ts"),
            "order_id": row.get("order_id"),
            "graded_at": _now_iso(),
            "graded_by": "exit_grade_v1",
            **reason,
            "entry_plan": plan,
            "plan_source": planned["plan_source"],
            "plan_stop_loss": plan_stop,
            "binding_stop_loss": binding_stop,
            "trailing_stop": trail,
            "entry_price": entry,
            "exit_price": fill,
            "quantity": qty,
            "reference_price": ref,
            "days_held": days_held,
            "horizon_days": plan.get("holding_horizon_days"),
            "realized_pnl_usd": pnl,
            "atr_pct": atr,
            "r_grade": r,
            "stop_slippage": slippage,
            "process_grade": (ap.get("brain_grade") or {}).get("process"),
            "missing": missing,
            "gradeable": bool(r.get("gradeable")) or slippage.get("applicable") is True,
        }
        grade["headline"] = format_exit_grade_line(grade)
        return grade
    except Exception as e:  # a grading bug must never look like a graded exit
        return {
            "exit_key": exit_key(row if isinstance(row, dict) else {}),
            "ticker": _s((row or {}).get("ticker")).upper(),
            "graded_at": _now_iso(),
            "graded_by": "exit_grade_v1",
            "gradeable": False,
            "error": str(e)[:200],
            "missing": ["grade_failed"],
            "headline": "exit grade failed",
        }


def format_exit_grade_line(g: dict) -> str:
    """One line the brain can actually read in its context budget.

    The whole point of this file is that an exit stops being a silent event, and a
    30-line JSON blob inside a slim pack is silent in practice. This is the
    sentence a trader would say out loud about the close.
    """
    try:
        g = g if isinstance(g, dict) else {}
        t = g.get("ticker") or "?"
        when = g.get("exit_date") or "?"
        kind = g.get("exit_reason_kind") or "unknown"
        bits = [f"{t} {when} {kind}"]

        r = g.get("r_grade") or {}
        if r.get("gradeable"):
            realized = r.get("realized_r")
            claimed = r.get("claimed_rr")
            if claimed is not None:
                bits.append(f"realized {realized:+.2f}R vs claimed {claimed:+.2f}R")
            else:
                bits.append(f"realized {realized:+.2f}R (no target on plan)")
            if kind == "discretionary" and r.get("risk_used_fraction") is not None:
                bits.append(f"spent {r['risk_used_fraction']:.0%} of planned risk")
        else:
            bits.append(f"UNGRADEABLE ({r.get('ungradeable_reason') or 'no plan'})")

        s = g.get("stop_slippage") or {}
        if s.get("applicable") and s.get("gap_usd_per_share") is not None:
            gap = s["gap_usd_per_share"]
            if gap > 0:
                atr_bit = (f", {s['gap_in_atr']:.2f} ATR"
                           if s.get("gap_in_atr") is not None else "")
                bits.append(
                    f"stop {s.get('stop_level')} filled {s.get('fill_price')} "
                    f"({s.get('gap_pct_of_stop'):.2f}% through{atr_bit}; "
                    f"{s.get('verdict')})")
            else:
                bits.append(f"stop {s.get('stop_level')} held ({s.get('verdict')})")
            if s.get("slippage_source") == "modelled":
                bits.append("gap MODELLED not measured")
        if g.get("missing"):
            bits.append("missing: " + ",".join(g["missing"][:4]))
        return " | ".join(bits)[:400]
    except Exception:
        return "exit grade unavailable"


def compact_exit_grade(g: dict) -> dict:
    """Brain-facing row: the grade, not the working."""
    g = g if isinstance(g, dict) else {}
    r = g.get("r_grade") or {}
    s = g.get("stop_slippage") or {}
    return {
        "ticker": g.get("ticker"),
        "exit_date": g.get("exit_date"),
        "exit_reason_kind": g.get("exit_reason_kind"),
        "exit_reason": (g.get("exit_reason") or "")[:160] or None,
        "plan_stop": g.get("plan_stop_loss"),
        "plan_target": (g.get("entry_plan") or {}).get("target_price"),
        "horizon_days": g.get("horizon_days"),
        "days_held": g.get("days_held"),
        "realized_r": r.get("realized_r"),
        "claimed_rr": r.get("claimed_rr"),
        "r_gap": r.get("r_gap"),
        "risk_used_fraction": r.get("risk_used_fraction"),
        "realized_pnl_usd": g.get("realized_pnl_usd"),
        "process_grade": g.get("process_grade"),
        "stop_gap_pct_of_stop": s.get("gap_pct_of_stop"),
        "stop_gap_in_atr": s.get("gap_in_atr"),
        "stop_gap_verdict": s.get("verdict"),
        "stop_gap_source": s.get("slippage_source"),
        "plan_source": g.get("plan_source"),
        "missing": g.get("missing"),
        "headline": g.get("headline"),
    }


def summarize_exit_grades(grades: list) -> dict:
    """Aggregate across graded exits, with modelled and realized gaps kept APART.

    Mixing them would let simulation arithmetic vote on how real stops fill, which
    is exactly the fabricated-input failure this system has already had once. The
    counts are published so a reader can see which sample any number came from.
    """
    rows = [g for g in (grades or []) if isinstance(g, dict)]
    r_rows = [g for g in rows if (g.get("r_grade") or {}).get("gradeable")]
    stop_rows = [g for g in rows if (g.get("stop_slippage") or {}).get("applicable")]
    realized_gaps = [g for g in stop_rows
                     if (g["stop_slippage"].get("slippage_source") == "realized"
                         and g["stop_slippage"].get("gap_in_atr") is not None)]
    modelled_gaps = [g for g in stop_rows
                     if (g["stop_slippage"].get("slippage_source") == "modelled"
                         and g["stop_slippage"].get("gap_in_atr") is not None)]

    def _mean(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "n_exits": len(rows),
        "n_r_gradeable": len(r_rows),
        "n_ungradeable": len(rows) - len(r_rows),
        "mean_realized_r": _mean([(g["r_grade"]).get("realized_r") for g in r_rows]),
        "mean_claimed_rr": _mean([(g["r_grade"]).get("claimed_rr") for g in r_rows]),
        "n_stop_exits": len(stop_rows),
        "n_stop_gaps_realized": len(realized_gaps),
        "n_stop_gaps_modelled": len(modelled_gaps),
        "mean_gap_in_atr_realized": _mean(
            [g["stop_slippage"].get("gap_in_atr") for g in realized_gaps]),
        "mean_gap_in_atr_modelled": _mean(
            [g["stop_slippage"].get("gap_in_atr") for g in modelled_gaps]),
        "n_worse_than_model": sum(
            1 for g in stop_rows
            if g["stop_slippage"].get("verdict") == "gap_worse_than_model"),
        "n_missing_plan": sum(1 for g in rows if "plan_stop_loss" in (g.get("missing") or [])),
        "n_unknown_reason": sum(1 for g in rows if g.get("exit_reason_kind") == "unknown"),
    }
