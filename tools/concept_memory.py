"""Concept memory — durable per-ticker understanding that evolves over time.

Each ticker gets data/concept_memory/{TICKER}.json:
  what_they_do, unit_economics, what_moves_stock, business_model_type,
  lessons[] (append-only), sources, updated_at

Updated automatically from stack cards, financial checklists, deep research
summaries, exit grades, and shadow lessons. Injected into the context bundle
for focus names so the brain compounds understanding — not just re-reads
raw filings every time.

Fail-soft; never raises from public APIs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEM_DIR = ROOT / "data" / "concept_memory"
MAX_LESSONS = 40


def _path(ticker: str) -> Path:
    return MEM_DIR / f"{str(ticker).upper()}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_memory(ticker: str) -> dict:
    p = _path(ticker)
    if not p.exists():
        return _empty(ticker)
    try:
        d = json.loads(p.read_text())
        if not isinstance(d, dict):
            return _empty(ticker)
        d.setdefault("ticker", str(ticker).upper())
        d.setdefault("lessons", [])
        return d
    except Exception:
        return _empty(ticker)


def _empty(ticker: str) -> dict:
    return {
        "ticker": str(ticker).upper(),
        "what_they_do": None,
        "unit_economics": None,
        "what_moves_stock": [],
        "business_model_type": None,
        "demand_driver": None,
        "financial_weight": None,
        "lessons": [],
        "updated_at": None,
        "version": 1,
    }


def save_memory(mem: dict) -> None:
    try:
        MEM_DIR.mkdir(parents=True, exist_ok=True)
        t = str(mem.get("ticker") or "").upper()
        if not t:
            return
        mem["ticker"] = t
        mem["updated_at"] = _now()
        _path(t).write_text(json.dumps(mem, indent=2, default=str))
    except Exception:
        pass


def append_lesson(ticker: str, lesson: str, *, source: str = "system",
                  tags: list | None = None) -> None:
    try:
        lesson = str(lesson or "").strip()
        if not lesson or len(lesson) < 12:
            return
        mem = load_memory(ticker)
        entry = {
            "ts": _now(),
            "source": source,
            "text": lesson[:500],
            "tags": tags or [],
        }
        # Dedupe near-identical
        for old in mem.get("lessons") or []:
            if old.get("text") == entry["text"]:
                return
        mem.setdefault("lessons", []).append(entry)
        mem["lessons"] = mem["lessons"][-MAX_LESSONS:]
        save_memory(mem)
    except Exception:
        pass


def update_from_research(
    ticker: str,
    *,
    stack_card: dict | None = None,
    checklist: dict | None = None,
    digest_bits: dict | None = None,
) -> dict:
    """Merge stack card + financial checklist into concept memory."""
    try:
        t = str(ticker).upper()
        mem = load_memory(t)
        sc = stack_card if isinstance(stack_card, dict) else {}
        cl = checklist if isinstance(checklist, dict) else {}
        if sc.get("what_they_do"):
            mem["what_they_do"] = str(sc["what_they_do"])[:800]
        elif sc.get("layer") and not mem.get("what_they_do"):
            mem["what_they_do"] = f"Operates in: {sc.get('layer')}"
        if sc.get("business_model_note"):
            mem["unit_economics"] = str(sc["business_model_note"])[:600]
        if sc.get("demand_driver"):
            mem["demand_driver"] = sc["demand_driver"]
        if sc.get("differential_question"):
            moves = list(mem.get("what_moves_stock") or [])
            q = str(sc["differential_question"])
            if q not in moves:
                moves.append(q)
            mem["what_moves_stock"] = moves[-8:]
        if sc.get("typical_customers"):
            mem["typical_customers"] = sc["typical_customers"]
        if sc.get("peer_substitutes"):
            mem["peer_substitutes"] = sc["peer_substitutes"]
        if cl.get("model_type"):
            mem["business_model_type"] = cl["model_type"]
        if cl.get("weight"):
            mem["financial_weight"] = cl["weight"]
        if cl.get("label"):
            mem["model_label"] = cl["label"]
        # Key lines as reminders
        if cl.get("lines"):
            mem["key_financial_lines"] = [
                {"line": x.get("line"), "red_flag": x.get("red_flag")}
                for x in cl["lines"][:8] if isinstance(x, dict)
            ]
        if isinstance(digest_bits, dict) and digest_bits:
            mem["last_digest"] = {
                k: digest_bits[k] for k in list(digest_bits)[:12]
            }
        mem["sources"] = list({*(mem.get("sources") or []), "stack_card", "checklist"})
        save_memory(mem)
        return mem
    except Exception as e:
        return {"ticker": ticker, "status": "error", "reason": str(e)[:100]}


def update_many_from_focus(
    focus: list[str],
    stack_cards: dict | None = None,
    checklists: dict | None = None,
) -> int:
    """Refresh concept memory for all focus names. Returns count updated."""
    n = 0
    try:
        by_sc = (stack_cards or {}).get("by_ticker") or {}
        by_cl = (checklists or {}).get("by_ticker") or {}
        for t in focus or []:
            tu = str(t).upper()
            if not tu:
                continue
            update_from_research(
                tu,
                stack_card=by_sc.get(tu),
                checklist=by_cl.get(tu),
            )
            n += 1
    except Exception:
        return n
    return n


def brain_facing_concept_memory(tickers: list[str] | None, limit_lessons: int = 5) -> dict:
    """Compact memory cards for the context bundle."""
    out = {}
    try:
        for t in tickers or []:
            tu = str(t).upper()
            if not tu:
                continue
            mem = load_memory(tu)
            if not mem.get("what_they_do") and not mem.get("lessons"):
                continue
            lessons = list(reversed(mem.get("lessons") or []))[:limit_lessons]
            out[tu] = {
                "what_they_do": mem.get("what_they_do"),
                "unit_economics": mem.get("unit_economics"),
                "business_model_type": mem.get("business_model_type"),
                "demand_driver": mem.get("demand_driver"),
                "financial_weight": mem.get("financial_weight"),
                "what_moves_stock": mem.get("what_moves_stock") or [],
                "key_financial_lines": mem.get("key_financial_lines") or [],
                "recent_lessons": [
                    {"text": L.get("text"), "source": L.get("source"), "ts": L.get("ts")}
                    for L in lessons
                ],
                "updated_at": mem.get("updated_at"),
            }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "by_ticker": {}}
    return {
        "note": "Durable concept memory for focus names — what they do, how to score "
                "them, and lessons appended from research/exits/shadows. Prefer this "
                "over reinventing the business each run; update via new lessons only.",
        "by_ticker": out,
        "n": len(out),
    }


if __name__ == "__main__":
    import sys
    for t in (sys.argv[1:] or ["NBIS"]):
        print(json.dumps(load_memory(t), indent=2)[:800])
