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

import hashlib
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


def _proposal_id(fingerprint: str) -> str:
    """Stable proposal id, a pure function of the note's fingerprint.

    This was `LP-{abs(hash(fp)) % 10**10:010d}`. Python salts hash() per process,
    so the id assigned when a note was adopted never matched the id computed for
    the same note a week later. auto_adopt_soft() dedupes by membership in
    `adopted_ids`, so that check could not match across runs: every Sunday would
    have re-adopted the entire backlog under fresh ids, and the dedupe that was
    supposed to prevent it was structurally incapable of firing.

    sha1 is a content digest here, not a security primitive.
    """
    digest = hashlib.sha1(str(fingerprint or "").encode("utf-8")).hexdigest()
    return f"LP-{int(digest[:15], 16) % 10 ** 10:010d}"


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
            "id": _proposal_id(fp),
            # Carried so dedupe survives any future change to the id scheme.
            "note_fp": fp,
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


def _adopted_fingerprints() -> set:
    """Note fingerprints already living in adopted_lessons.json.

    Dedupe cannot rely on the id alone across the hash()->sha1 change: every id
    written before that fix came from a PROCESS-SALTED hash and is therefore
    unreproducible by construction. Matching those rows by id is impossible, so
    replaying the backlog would re-adopt the whole thing under fresh ids — the
    exact duplicate storm the dedupe existed to prevent, just one generation
    later.

    Content is the stable key. `text` is note_excerpt (note[:600]) and the
    fingerprint is note[:80], so text[:80] recovers the fingerprint of any
    already-adopted lesson regardless of which id scheme wrote it.
    """
    try:
        if not ADOPTED_JSON.exists():
            return set()
        j = json.loads(ADOPTED_JSON.read_text())
        out = set()
        for L in (j.get("lessons") or []):
            if not isinstance(L, dict):
                continue
            fp = L.get("note_fp") or str(L.get("text") or "")[:80]
            if fp:
                out.add(fp)
        return out
    except Exception:
        return set()


def _already_adopted(p: dict, adopted_ids: set, adopted_fps: set) -> bool:
    return (p.get("id") in adopted_ids
            or (p.get("note_fp") and p["note_fp"] in adopted_fps))


def auto_adopt_soft(proposals: list[dict] | None = None) -> dict:
    """Auto-adopt soft proposals not already adopted; append to adopted_lessons.md."""
    try:
        state = _load_proposals_state()
        adopted_ids = set(state.get("adopted_ids") or [])
        adopted_fps = _adopted_fingerprints()
        proposals = proposals if proposals is not None else propose_adoptions()
        newly = []
        for p in proposals:
            if not p.get("auto_adoptable"):
                continue
            if _already_adopted(p, adopted_ids, adopted_fps):
                continue
            newly.append(p)
            adopted_fps.add(p.get("note_fp") or "")
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
                    "note_fp": p.get("note_fp"),
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


def _n_adopted_lessons() -> int:
    try:
        if not ADOPTED_JSON.exists():
            return 0
        j = json.loads(ADOPTED_JSON.read_text())
        return len([L for L in (j.get("lessons") or []) if isinstance(L, dict)])
    except Exception:
        return 0


def pipeline_health() -> dict:
    """Is the adopt pipeline DEAD, or merely QUIET? They looked identical.

    The live symptom: 26 improvement notes on disk, 6 of them auto-adoptable, no
    data/adopted_lessons.json at all, and brain_facing_adopted_lessons()
    returning [] — exactly what "nothing was worth adopting this week" looks
    like. Every API in this module is fail-soft, so the distinction has to be
    measured explicitly and reported, or a broken pipeline reads as a quiet one
    indefinitely.

    Returns {"ok", "status", "n_pending_soft", ...}. Never raises.
    """
    try:
        state = _load_proposals_state()
        adopted_ids = {i for i in (state.get("adopted_ids") or [])}
        adopted_fps = _adopted_fingerprints()
        notes = harvest_improvement_notes(40)
        proposals = propose_adoptions(notes, limit=25)
        pending = [p for p in proposals
                   if p.get("auto_adoptable")
                   and not _already_adopted(p, adopted_ids, adopted_fps)]
        n_adopted = _n_adopted_lessons()
        ever_ran = bool(adopted_ids) or n_adopted > 0

        if pending and not ever_ran:
            ok, status = False, "never_ran_with_adoptable_notes"
            detail = (f"{len(pending)} auto-adoptable note(s) on disk and nothing has "
                      f"EVER been adopted — the pipeline is dead, not quiet.")
        elif pending:
            ok, status = False, "adoptable_notes_pending"
            detail = (f"{len(pending)} auto-adoptable note(s) have not been adopted "
                      f"since the last pipeline run.")
        elif not notes:
            ok, status = True, "quiet_no_notes"
            detail = "no improvement notes harvested — nothing to adopt."
        else:
            ok, status = True, "ok"
            detail = "every auto-adoptable note has been adopted."
        return {
            "ok": ok,
            "status": status,
            "detail": detail,
            "n_notes": len(notes),
            "n_proposals": len(proposals),
            "n_pending_soft": len(pending),
            "n_hard_pending": sum(1 for p in proposals if not p.get("auto_adoptable")),
            "n_adopted_lessons": n_adopted,
            "pending_ids": [p.get("id") for p in pending][:10],
        }
    except Exception as e:
        return {"ok": False, "status": "health_check_failed",
                "reason": str(e)[:150], "n_pending_soft": 0}


def run_weekly_adopt_pipeline() -> dict:
    """Full harvest → propose → auto-adopt. Call from self_review.

    Returns its errors instead of raising. runlib/reviews.py wraps this in a
    bare except whose only output is a print, and stdout is not captured on the
    cloud runner — so a throw here left no record anywhere, in any file, which
    is why the 2026-07-17 failure was undiagnosable after the fact. A returned
    error at least reaches the caller's result dict.
    """
    try:
        notes = harvest_improvement_notes(50)
        proposals = propose_adoptions(notes, limit=25)
        result = auto_adopt_soft(proposals)
        result["notes_harvested"] = len(notes)
        result.setdefault("status", "ok")
        result["health"] = pipeline_health()
        return result
    except Exception as e:
        return {"status": "error", "reason": str(e)[:300], "auto_adopted": 0,
                "proposed": 0, "hard_pending": 0, "notes_harvested": 0}


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
