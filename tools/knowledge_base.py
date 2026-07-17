"""Knowledge base — the SIXTH learning system: proactive daily study.

The other five learning systems are reactive (they grade what already
happened). This one is a curriculum: every weekday after the close a dedicated
study session picks ONE topic — weighted toward the least-covered discipline
and whatever the trade feedback says is currently weakest — researches it with
web search, and writes a durable, structured lesson here. Lessons are injected
into every trading run through the learning pack, so the agent's craft
compounds the way a person's does: bit by bit, day by day.

Storage (mirrors the adopted-lessons dual-file pattern):
  * data/knowledge_base.json — structured store, read into the brain prompt
  * data/knowledge_base.md   — human-readable append log
  * dashboard/data/learning_journal.json — public journal for the site

Fail-soft; never raises from public APIs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_JSON = ROOT / "data" / "knowledge_base.json"
KB_MD = ROOT / "data" / "knowledge_base.md"
JOURNAL_JSON = ROOT / "dashboard" / "data" / "learning_journal.json"

MAX_ACTIVE = 60           # active (non-superseded) lessons kept in rotation
MAX_HISTORY = 200         # total records kept in the JSON store
JOURNAL_LIMIT = 60        # entries published to the dashboard journal

# The curriculum. Keys are what the study prompt and rotation use; values are
# the scope hint shown to the studying brain.
DISCIPLINES = {
    "technical_analysis": "chart structure, trend, volume, entry/exit timing, indicator craft",
    "fundamental_analysis": "financial statements, valuation, estimate revisions, quality signals",
    "risk_management": "position sizing, stop placement, portfolio heat, correlation, drawdown math",
    "strategy_playbooks": "swing setups, earnings drift, momentum vs reversal, playbook design",
    "market_microstructure": "liquidity, options positioning, flows, auctions, market mechanics",
    "trading_psychology": "behavioral biases, discipline, process-over-outcome, decision hygiene",
    "macro_regimes": "rates, inflation, cycles, sector rotation, regime identification",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        if KB_JSON.exists():
            doc = json.loads(KB_JSON.read_text())
            if isinstance(doc, dict):
                doc.setdefault("entries", [])
                return doc
    except Exception:
        pass
    return {"entries": [], "updated_at": None}


def _save(doc: dict) -> None:
    doc["updated_at"] = _now()
    KB_JSON.parent.mkdir(parents=True, exist_ok=True)
    KB_JSON.write_text(json.dumps(doc, indent=2, default=str))


def _fingerprint(text: str, n: int = 48) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()[:n]


def _active(entries: list) -> list:
    return [e for e in entries if isinstance(e, dict) and not e.get("superseded_by")]


def discipline_counts(entries: list | None = None) -> dict:
    """{discipline: n_active_lessons} across the whole curriculum. Pure-ish."""
    entries = entries if entries is not None else _load()["entries"]
    counts = {d: 0 for d in DISCIPLINES}
    for e in _active(entries):
        d = e.get("discipline")
        if d in counts:
            counts[d] += 1
    return counts


def next_discipline(weakness_hints: list | None = None,
                    entries: list | None = None) -> str:
    """Which discipline to study next: any hinted weakness discipline first
    (least-covered among hints), else the least-covered overall. Pure given
    entries — deterministic so tests can pin it."""
    counts = discipline_counts(entries)
    hints = [h for h in (weakness_hints or []) if h in counts]
    pool = hints or list(counts)
    return min(pool, key=lambda d: (counts[d], list(counts).index(d)))


def add_lesson(lesson: dict, run_id: str | None = None) -> dict:
    """Validate and persist one studied lesson. Near-duplicate topics are
    rejected (status=duplicate) so the curriculum keeps moving instead of
    re-learning the same page. Never raises."""
    try:
        if not isinstance(lesson, dict):
            return {"status": "invalid", "reason": "not_a_dict"}
        topic = str(lesson.get("topic") or "").strip()
        summary = str(lesson.get("summary") or "").strip()
        how = str(lesson.get("how_to_apply") or "").strip()
        if not topic or len(summary) < 80 or len(how) < 60:
            return {"status": "invalid",
                    "reason": "topic/summary/how_to_apply missing or too thin"}
        discipline = lesson.get("discipline")
        if discipline not in DISCIPLINES:
            discipline = "strategy_playbooks"
        key_points = [str(k).strip()[:300] for k in (lesson.get("key_points") or [])
                      if str(k).strip()][:8]
        sources = [str(s).strip()[:300] for s in (lesson.get("sources") or [])
                   if str(s).strip()][:6]

        doc = _load()
        fp = _fingerprint(topic)
        for e in _active(doc["entries"]):
            if _fingerprint(e.get("topic")) == fp:
                return {"status": "duplicate", "existing_id": e.get("id"),
                        "topic": topic}

        entry = {
            "id": f"KB-{abs(hash(fp + _now()[:10])) % 10**10:010d}",
            "discipline": discipline,
            "topic": topic[:200],
            "summary": summary[:1500],
            "key_points": key_points,
            "how_to_apply": how[:1200],
            "sources": sources,
            "learned_at": _now(),
            "run_id": run_id,
        }
        doc["entries"] = (doc["entries"] + [entry])[-MAX_HISTORY:]
        _save(doc)
        _append_md(entry)
        publish_learning_journal(doc)
        return {"status": "ok", "id": entry["id"], "discipline": discipline,
                "topic": entry["topic"]}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150]}


def _append_md(entry: dict) -> None:
    try:
        prev = KB_MD.read_text() if KB_MD.exists() else (
            "# Knowledge base (daily study)\n\n"
            "One lesson per weekday study session. Injected into every trading run.\n\n")
        block = (
            f"### [{entry['learned_at'][:10]}] {entry['discipline']} — {entry['id']}\n\n"
            f"**{entry['topic']}**\n\n{entry['summary']}\n\n"
            + "".join(f"- {k}\n" for k in entry.get("key_points") or [])
            + f"\n*Apply here:* {entry['how_to_apply']}\n\n"
            + (f"*Sources:* {'; '.join(entry['sources'])}\n\n" if entry.get("sources") else "")
        )
        KB_MD.write_text(prev + block)
    except Exception:
        pass


def publish_learning_journal(doc: dict | None = None) -> None:
    """Write the public dashboard journal (newest first). Fail-soft."""
    try:
        doc = doc or _load()
        entries = [e for e in reversed(_active(doc["entries"]))][:JOURNAL_LIMIT]
        JOURNAL_JSON.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL_JSON.write_text(json.dumps({
            "updated_at": _now(),
            "note": "Daily study journal: one researched lesson per weekday, "
                    "injected into every trading run.",
            "discipline_counts": discipline_counts(doc["entries"]),
            "entries": entries,
        }, indent=2, default=str))
    except Exception:
        pass


def prune_knowledge_base(max_active: int = MAX_ACTIVE) -> dict:
    """Cap active lessons: oldest beyond the cap are superseded by the newest
    active id (same housekeeping semantics as prune_adopted_lessons)."""
    try:
        doc = _load()
        active = _active(doc["entries"])
        pruned = 0
        if len(active) > max_active:
            newest = active[-1].get("id")
            for e in active[:-max_active]:
                e["superseded_by"] = newest
                e["superseded_at"] = _now()
                e["supersede_reason"] = "cap_max_active"
                pruned += 1
            _save(doc)
            publish_learning_journal(doc)
        return {"pruned": pruned, "active": min(len(active), max_active),
                "total": len(doc["entries"])}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "pruned": 0}


def brain_facing_knowledge_base(limit: int = 8) -> dict:
    """Inject into trading context: the most recent studied lessons plus
    curriculum coverage. Compact by design (learning-pack discipline)."""
    try:
        doc = _load()
        active = _active(doc["entries"])
        recent = list(reversed(active))[:limit]
        return {
            "note": ("KNOWLEDGE BASE from the daily study sessions (system 6). "
                     "Durable craft lessons — apply the how_to_apply lines when the "
                     "situation matches; cite a lesson id when one drives a decision."),
            "recent": [
                {"id": e.get("id"), "discipline": e.get("discipline"),
                 "topic": e.get("topic"), "summary": e.get("summary"),
                 "how_to_apply": e.get("how_to_apply"),
                 "learned_at": str(e.get("learned_at") or "")[:10]}
                for e in recent
            ],
            "n_active": len(active),
            "discipline_counts": discipline_counts(doc["entries"]),
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "recent": []}


if __name__ == "__main__":
    print(json.dumps(brain_facing_knowledge_base(), indent=2))
