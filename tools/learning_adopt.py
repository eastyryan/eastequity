"""Weekly adopt pipeline — turn improvement notes into durable lessons.

Flow:
  1. harvest_improvement_notes() — recent journal improvements
  2. propose_adoptions() — structured proposals (soft CLAUDE/text lessons)
  3. auto_adopt_soft() — write approved/auto soft lessons to data/adopted_lessons.md
     and data/learning_proposals.json (audit trail)
  4. brain_facing_adopted_lessons() — inject into every trading context

Hard code/validator changes are proposed but NEVER auto-applied (owner/CI only).

Fail-soft; never raises from public APIs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROPOSALS_FILE = ROOT / "data" / "learning_proposals.json"
ADOPTED_FILE = ROOT / "data" / "adopted_lessons.md"
ADOPTED_JSON = ROOT / "data" / "adopted_lessons.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def harvest_improvement_notes(limit: int = 40) -> list[dict]:
    """Newest-first improvement notes from journal/improvements/*.jsonl."""
    rows: list[dict] = []
    try:
        jdir = ROOT / "journal" / "improvements"
        if not jdir.exists():
            return []
        for f in sorted(jdir.glob("*.jsonl"), reverse=True):
            for line in reversed(f.read_text().splitlines()):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("note"):
                    rows.append(rec)
                if len(rows) >= limit:
                    return rows
    except Exception:
        return rows
    return rows


def _classify_note(note: str) -> dict:
    """Heuristic classification of an improvement note into an adoption proposal."""
    n = str(note or "")
    low = n.lower()
    kind = "soft_lesson"
    target = "adopted_lessons"
    if any(k in low for k in ("validator", "hard rule", "autonomy_config", "reject")):
        kind = "hard_proposal"
        target = "needs_owner_code_change"
    elif any(k in low for k in ("claude.md", "process", "checklist", "watchlist")):
        kind = "soft_process"
        target = "adopted_lessons"
    elif any(k in low for k in ("data", "bundle", "scanner", "feed", "chart")):
        kind = "data_engineering"
        target = "needs_owner_code_change"
    elif n.startswith("Weekly self-review:"):
        kind = "self_review_commitment"
        target = "adopted_lessons"
    # Extract a short title
    title = re.sub(r"\s+", " ", n)[:120]
    return {
        "kind": kind,
        "target": target,
        "title": title,
        "auto_adoptable": kind in ("soft_lesson", "soft_process", "self_review_commitment"),
    }


def propose_adoptions(notes: list[dict] | None = None, limit: int = 20) -> list[dict]:
    """Build adoption proposals from notes."""
    notes = notes if notes is not None else harvest_improvement_notes(limit)
    proposals = []
    seen = set()
    for rec in notes:
        note = str(rec.get("note") or "")
        if len(note) < 40:
            continue
        # Fingerprint
        fp = note[:80]
        if fp in seen:
            continue
        seen.add(fp)
        meta = _classify_note(note)
        proposals.append({
            "id": f"LP-{abs(hash(fp)) % 10**10:010d}",
            "ts": rec.get("ts") or _now(),
            "run_id": rec.get("run_id"),
            "note_excerpt": note[:600],
            **meta,
            "status": "proposed",
        })
        if len(proposals) >= limit:
            break
    return proposals


def _load_proposals_state() -> dict:
    if not PROPOSALS_FILE.exists():
        return {"proposals": [], "adopted_ids": []}
    try:
        return json.loads(PROPOSALS_FILE.read_text())
    except Exception:
        return {"proposals": [], "adopted_ids": []}


def auto_adopt_soft(proposals: list[dict] | None = None) -> dict:
    """Auto-adopt soft proposals not already adopted; append to adopted_lessons.md."""
    try:
        state = _load_proposals_state()
        adopted_ids = set(state.get("adopted_ids") or [])
        proposals = proposals if proposals is not None else propose_adoptions()
        newly = []
        for p in proposals:
            if not p.get("auto_adoptable"):
                continue
            if p.get("id") in adopted_ids:
                continue
            newly.append(p)
            adopted_ids.add(p["id"])
            p["status"] = "auto_adopted"
            p["adopted_at"] = _now()

        # Persist proposals audit
        all_p = list(state.get("proposals") or [])
        # Upsert by id
        by_id = {x.get("id"): x for x in all_p if isinstance(x, dict)}
        for p in proposals:
            by_id[p["id"]] = {**by_id.get(p["id"], {}), **p}
        state["proposals"] = list(by_id.values())[-100:]
        state["adopted_ids"] = list(adopted_ids)[-200:]
        state["updated_at"] = _now()
        PROPOSALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROPOSALS_FILE.write_text(json.dumps(state, indent=2, default=str))

        if newly:
            ADOPTED_FILE.parent.mkdir(parents=True, exist_ok=True)
            block = []
            for p in newly:
                block.append(
                    f"### [{p.get('adopted_at', _now())[:10]}] {p.get('kind')} — {p.get('id')}\n\n"
                    f"{p.get('note_excerpt')}\n\n"
                    f"*Source run: {p.get('run_id') or 'n/a'}*\n"
                )
            prev = ADOPTED_FILE.read_text() if ADOPTED_FILE.exists() else (
                "# Adopted lessons (auto + weekly pipeline)\n\n"
                "Soft lessons only. Hard code changes require owner approval.\n\n"
            )
            ADOPTED_FILE.write_text(prev + "\n".join(block))

            # JSON mirror for structured inject
            j = {"lessons": [], "updated_at": _now()}
            if ADOPTED_JSON.exists():
                try:
                    j = json.loads(ADOPTED_JSON.read_text())
                except Exception:
                    pass
            lessons = j.get("lessons") if isinstance(j, dict) else []
            if not isinstance(lessons, list):
                lessons = []
            for p in newly:
                lessons.append({
                    "id": p.get("id"),
                    "kind": p.get("kind"),
                    "text": p.get("note_excerpt"),
                    "adopted_at": p.get("adopted_at"),
                    "run_id": p.get("run_id"),
                })
            ADOPTED_JSON.write_text(json.dumps({
                "updated_at": _now(),
                "lessons": lessons[-80:],
            }, indent=2))

        return {
            "proposed": len(proposals),
            "auto_adopted": len(newly),
            "hard_pending": sum(1 for p in proposals if not p.get("auto_adoptable")),
            "new_ids": [p.get("id") for p in newly],
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "auto_adopted": 0}


def run_weekly_adopt_pipeline() -> dict:
    """Full harvest → propose → auto-adopt. Call from self_review."""
    notes = harvest_improvement_notes(50)
    proposals = propose_adoptions(notes, limit=25)
    result = auto_adopt_soft(proposals)
    result["notes_harvested"] = len(notes)
    return result


def brain_facing_adopted_lessons(limit: int = 12) -> dict:
    """Inject into trading context — durable soft lessons (active only)."""
    try:
        lessons = []
        if ADOPTED_JSON.exists():
            j = json.loads(ADOPTED_JSON.read_text())
            raw = list(j.get("lessons") or [])
            # Newest first; skip superseded
            active = [
                L for L in reversed(raw)
                if isinstance(L, dict) and not L.get("superseded_by")
            ]
            lessons = active[:limit]
        hard = []
        if PROPOSALS_FILE.exists():
            st = json.loads(PROPOSALS_FILE.read_text())
            hard = [
                p for p in (st.get("proposals") or [])
                if isinstance(p, dict) and not p.get("auto_adoptable")
                and p.get("status") == "proposed"
            ][:8]
        return {
            "note": (
                "ADOPTED LESSONS from the weekly pipeline (improvement notes → durable "
                "text). Treat as standing process commitments until a later review "
                "supersedes them. hard_pending items need owner/code — do not invent "
                "validator changes yourself. Superseded lessons are omitted."
            ),
            "lessons": [
                {"id": L.get("id"), "kind": L.get("kind"),
                 "text": L.get("text"), "adopted_at": L.get("adopted_at")}
                for L in lessons
            ],
            "hard_pending": [
                {"id": p.get("id"), "kind": p.get("kind"),
                 "title": p.get("title"), "target": p.get("target")}
                for p in hard
            ],
            "n_adopted": len(lessons),
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "lessons": [],
                "hard_pending": []}


def _lesson_fingerprint(text: str, n: int = 48) -> str:
    t = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    return t[:n]


def prune_adopted_lessons(max_active: int = 40,
                          near_dup_chars: int = 48) -> dict:
    """Cap active adopted lessons and mark near-duplicates as superseded_by.

    Older near-dup of a newer lesson gets superseded_by=<newer_id>.
    Excess oldest active lessons beyond max_active are superseded by the
    newest active lesson id (housekeeping, not semantic merge).

    Fail-soft; never raises.
    """
    try:
        if not ADOPTED_JSON.exists():
            return {"pruned": 0, "active": 0, "status": "empty"}
        j = json.loads(ADOPTED_JSON.read_text())
        lessons = list(j.get("lessons") or [])
        if not lessons:
            return {"pruned": 0, "active": 0, "status": "empty"}

        # Work newest-last so we can walk reverse for "keep newest"
        by_id = {}
        ordered = []
        for L in lessons:
            if not isinstance(L, dict) or not L.get("id"):
                continue
            by_id[L["id"]] = dict(L)
            ordered.append(L["id"])

        pruned = 0
        # Near-duplicate: older supersedes under newer with same fingerprint
        seen_fp: dict[str, str] = {}  # fp -> newest id kept
        for lid in reversed(ordered):
            L = by_id[lid]
            if L.get("superseded_by"):
                continue
            fp = _lesson_fingerprint(L.get("text") or "", near_dup_chars)
            if not fp or len(fp) < 12:
                continue
            if fp in seen_fp:
                # This is older (we're walking newest→oldest); supersede it
                L["superseded_by"] = seen_fp[fp]
                L["superseded_at"] = _now()
                L["supersede_reason"] = "near_duplicate"
                pruned += 1
            else:
                seen_fp[fp] = lid

        # Cap active count: supersede oldest active beyond max_active
        active_ids = [
            lid for lid in ordered
            if not by_id[lid].get("superseded_by")
        ]
        if len(active_ids) > max_active:
            keep = set(active_ids[-max_active:])  # newest N (ordered is old→new)
            newest = active_ids[-1]
            for lid in active_ids:
                if lid in keep:
                    continue
                by_id[lid]["superseded_by"] = newest
                by_id[lid]["superseded_at"] = _now()
                by_id[lid]["supersede_reason"] = "cap_max_active"
                pruned += 1

        new_lessons = [by_id[lid] for lid in ordered if lid in by_id]
        # Archive deep history but keep file bounded
        if len(new_lessons) > 120:
            # Prefer keeping all non-superseded + recent superseded
            active = [L for L in new_lessons if not L.get("superseded_by")]
            super_ed = [L for L in new_lessons if L.get("superseded_by")][-40:]
            new_lessons = (active + super_ed)[-120:]

        ADOPTED_JSON.write_text(json.dumps({
            "updated_at": _now(),
            "pruned_at": _now(),
            "lessons": new_lessons,
        }, indent=2))

        n_active = sum(1 for L in new_lessons if not L.get("superseded_by"))
        return {
            "pruned": pruned,
            "active": n_active,
            "total": len(new_lessons),
            "max_active": max_active,
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "pruned": 0}


if __name__ == "__main__":
    print(json.dumps(run_weekly_adopt_pipeline(), indent=2))
