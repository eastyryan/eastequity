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

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_JSON = ROOT / "data" / "knowledge_base.json"
KB_MD = ROOT / "data" / "knowledge_base.md"
JOURNAL_JSON = ROOT / "dashboard" / "data" / "learning_journal.json"

MAX_ACTIVE = 60           # active (non-superseded) lessons kept in rotation
MAX_HISTORY = 200         # total records kept in the JSON store
JOURNAL_LIMIT = 60        # entries published to the dashboard journal

# --- evidence lifecycle (same sample-size philosophy as the Learning Protocol:
# under MIN_GRADED_TRADES linked outcomes a lesson is an anecdote and gets NO
# judgment; only real evidence promotes or demotes) ---------------------------
CITATION_RE = re.compile(r"KB-\d{10}")
MIN_GRADED_TRADES = 3     # linked closed trades before any evidence verdict
# Run ids look like 20260731-098397 (YYYYMMDD-hex). The date half is the only
# honest source for a learned_at we never stamped — see _learned_at_from_run_id.
RUN_ID_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-[0-9a-f]{4,}$")
# The proposal fields a lesson citation can legitimately live in. Scanning the
# WHOLE proposal dict (json.dumps) also works, but naming the fields keeps a
# stray id in an unrelated key (a ticker, a price note) from creating a link.
PROPOSAL_TEXT_FIELDS = (
    "thesis", "variant_perception", "risk_map", "macro_context", "catalysts",
    "thesis_invalidators", "earnings_case", "conviction_case", "scenarios",
)
VALIDATE_WIN_RATE = 0.60  # >= this over >= MIN trades -> "validated"
UNDERPERFORM_WIN_RATE = 0.40  # < this over >= MIN trades -> "underperforming"
MAX_LINKS_PER_LESSON = 20
MAX_SUPERSEDES_PER_STUDY = 2      # contradiction budget per study session
MAX_CONSOLIDATION_ACTIONS = 5     # merge/retire budget per Friday consolidation
CONSOLIDATION_MIN_AGE_DAYS = 7    # too-fresh lessons are untouchable

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


def _learned_at_from_run_id(run_id: str | None) -> str | None:
    """The study run's DATE, recovered from its run_id (20260731-098397).

    Why this exists: through 2026-07-19 the single lesson on disk carried
    `learned_at: None` (tests/test_learning_loop_honesty.py pins that fact), and
    a null there is not cosmetic — `apply_consolidation._too_fresh` compares
    today against it, `_date_gap_days` returns its 999 sentinel, and the lesson
    reads as ancient, so the 7-day untouchable window silently stops protecting
    it. `_selection_rank` also sorts a null to the bottom of the brain-facing
    pack, so the lesson stops being injected at all.

    The run_id's date half is DERIVED, not invented: it is the day the study
    session that wrote the lesson actually ran. Midnight UTC is used because the
    id carries no time — a date we can source beats a timestamp we cannot.
    Returns None when the id is not in the expected shape; callers must not
    substitute _now(), which would date a July lesson to whenever the store was
    last repaired.
    """
    m = RUN_ID_DATE_RE.match(str(run_id or "").strip())
    if not m:
        return None
    try:
        y, mo, d = (int(x) for x in m.groups())
        return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


def _load_raw() -> dict:
    """The store EXACTLY as it sits on disk. Never heals, never writes.

    store_health() must read through THIS and not _load(): a diagnostic that
    repairs what it measures reports a permanent green over a subsystem that
    dies after every deploy.
    """
    try:
        if KB_JSON.exists():
            doc = json.loads(KB_JSON.read_text())
            if isinstance(doc, dict):
                doc.setdefault("entries", [])
                return doc
    except Exception:
        pass
    return {"entries": [], "updated_at": None}


def _load() -> dict:
    """READ path, self-healing IN MEMORY from the published journal.

    The incident this guards: data/knowledge_base.json was untracked (and not
    gitignored) while its derived public view dashboard/data/learning_journal.json
    WAS committed. An ephemeral runner therefore lost the source of truth and kept
    the derived view — after which record_citations / link_lessons_to_trade /
    update_lesson_outcomes each iterated an empty list and returned 0. Silently.
    Forever. A lesson could be cited and never graded.

    Healing here is in-memory only; callers that mutate persist through _save(),
    and ensure_store() is the one place that materializes the repair on disk. That
    keeps reads side-effect free while making the grading APIs work again.
    """
    doc = _load_raw()
    try:
        pub = published_entries()
        if pub:
            have = {e.get("id") for e in doc.get("entries") or []
                    if isinstance(e, dict)}
            missing = [_normalize_published(e) for e in pub
                       if e.get("id") not in have]
            if missing:
                doc["entries"] = (list(doc.get("entries") or []) + missing)[-MAX_HISTORY:]
    except Exception:
        pass
    return doc


def _published_rows() -> list:
    """EVERY row in the public journal, well-formed or not.

    Counting happens here and recovery happens in published_entries(), and the
    two must stay separate: n_published is a measure of what the journal CLAIMS
    to hold, so filtering it down to what happens to be recoverable would let a
    corrupted journal report a cheerful zero — the silent-zero failure this whole
    check exists to catch.
    """
    try:
        if not JOURNAL_JSON.exists():
            return []
        doc = json.loads(JOURNAL_JSON.read_text())
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict):
            # The journal writes {"updated_at", "note", "discipline_counts",
            # "entries"}. Guessing one key name would report zero here too.
            return list(doc.get("entries") or doc.get("lessons") or [])
        return []
    except Exception:
        return []


def published_entries() -> list:
    """Lesson records recoverable from the tracked PUBLIC journal — the record of
    last resort, because it is the copy that survives an ephemeral runner.

    Only rows carrying an id are recoverable: the id IS the citation key, and a
    lesson restored without one could never be graded, so it would be furniture.
    """
    return [dict(e) for e in _published_rows()
            if isinstance(e, dict) and e.get("id")]


def _normalize_published(entry: dict) -> dict:
    """A published row back into store shape. The journal carries the lesson
    text but not the private bookkeeping, so provenance is stamped: a recovered
    lesson's citation/outcome history is genuinely gone, and pretending
    otherwise would fabricate evidence."""
    out = dict(entry)
    out.setdefault("discipline", "strategy_playbooks")
    # setdefault does NOT replace an explicit null, and the journal carries
    # `learned_at: null` on rows written before add_lesson stamped it (see
    # tests/test_learning_loop_honesty.py, which pins that the real store's one
    # lesson had learned_at: None). A null survives every recovery and then
    # silently ages the lesson to "ancient" in _too_fresh and sorts it to the
    # bottom of the brain-facing pack, so it is repaired here, in this order:
    # the run_id's own date if we have it, else the store timestamp already on
    # the row, else the recovery time — which is the honest last resort, since
    # "when we found it" is the only date we can actually source for a row that
    # never carried one.
    if not out.get("learned_at"):
        out["learned_at"] = (_learned_at_from_run_id(out.get("run_id"))
                             or out.get("updated_at") or _now())
    out["recovered_from_published"] = True
    return out


def ensure_store(persist: bool = True) -> dict:
    """Materialize data/knowledge_base.json, rebuilding it from the published
    journal when the store was lost.

    Called from runlib/publish.py BEFORE the git-add path list, because that
    list gates every entry on .exists() — so listing knowledge_base.json there
    was purely decorative for a file that had never existed.

    Statuses: rebuilt_from_published | present | empty. "empty" is NOT a
    rebuild — no published lessons means a quiet curriculum, not a loss, and
    reporting it as recovery would invent a store out of nothing.
    """
    try:
        raw = _load_raw()
        existed = KB_JSON.exists()
        have = {e.get("id") for e in raw.get("entries") or [] if isinstance(e, dict)}
        pub = published_entries()
        missing = [e for e in pub if e.get("id") not in have]

        if not existed and not pub:
            return {"status": "empty", "n_entries": 0, "n_published": 0, "ids": [],
                    "detail": "no store and no published lessons — nothing to recover"}
        if missing:
            raw["entries"] = (list(raw.get("entries") or [])
                              + [_normalize_published(e) for e in missing])[-MAX_HISTORY:]
            if persist:
                _save(raw)
            return {"status": "rebuilt_from_published",
                    "n_entries": len(raw["entries"]),
                    "n_published": len(pub),
                    "ids": [e.get("id") for e in missing],
                    "detail": (f"recovered {len(missing)} lesson(s) from "
                               f"{JOURNAL_JSON.name}; citation grading is live again")}
        return {"status": "present", "n_entries": len(raw.get("entries") or []),
                "n_published": len(pub), "ids": [], "detail": None}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "n_entries": 0, "ids": []}


def _save(doc: dict) -> None:
    """Atomic write — the store is the only record of what the system has learned."""
    doc["updated_at"] = _now()
    KB_JSON.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(KB_JSON.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(doc, indent=2, default=str))
        os.replace(tmp, KB_JSON)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def store_health() -> dict:
    """Is the knowledge base actually persisting? Deliberately NOT fail-soft.

    Every public API here is fail-soft and returns 0 or [] on a missing store, so a
    DEAD loop is indistinguishable from a QUIET one. That is exactly how this failed:
    data/knowledge_base.json was never committed (untracked, and not gitignored)
    while its derived public view dashboard/data/learning_journal.json was — so a
    study session ran, published a lesson, and the source of truth vanished. Every
    subsequent citation graded against an empty list and returned 0, silently.

    Returns {"ok", "status", "n_entries", "n_published", "recoverable", "detail"}
    so a caller can surface the mismatch instead of inferring health from silence.

    Reads the store RAW, through _load_raw(). _load() self-heals from the
    published journal, and a check that repairs what it measures would report
    permanent green over a loop that dies on every deploy — the outage would
    survive precisely because the instrument hid it. `recoverable` says whether
    ensure_store() can restore what is missing.
    """
    raw = _load_raw()
    entries = [e for e in (raw.get("entries") or []) if isinstance(e, dict)]
    n_entries = len(entries)
    # COUNT every published row; RECOVER only the well-formed ones. A journal
    # row we cannot restore still proves the store is behind.
    n_published = len(_published_rows())
    have = {e.get("id") for e in entries}
    missing = [e.get("id") for e in published_entries() if e.get("id") not in have]

    if not KB_JSON.exists() and n_published:
        return {"ok": False, "status": "store_missing_but_lessons_published",
                "n_entries": 0, "n_published": n_published,
                "recoverable": True, "recoverable_ids": missing,
                "detail": (f"{n_published} lesson(s) are published but "
                           f"{KB_JSON.name} does not exist — citations grade against "
                           f"nothing. ensure_store() rebuilds it from the journal; "
                           f"runlib/publish.py must commit it.")}
    if n_published > n_entries or missing:
        return {"ok": False, "status": "store_behind_published",
                "n_entries": n_entries, "n_published": n_published,
                "recoverable": bool(missing), "recoverable_ids": missing,
                "detail": (f"published journal has {n_published} lesson(s) but the "
                           f"store has {n_entries} — the store is not persisting.")}
    return {"ok": True, "status": "ok", "n_entries": n_entries,
            "n_published": n_published, "recoverable": False, "detail": None}


def _fingerprint(text: str, n: int = 48) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()[:n]


def _lesson_id(fingerprint: str) -> str:
    """The citation key, as a pure function of the lesson's content.

    ROOT CAUSE of the dead grading loop: this was
    `KB-{abs(hash(fp + today)) % 10**10:010d}`. Python SALTS hash() per process
    (PYTHONHASHSEED is random unless pinned), so the SAME lesson received a
    different id in every process. The id is what record_citations,
    link_lessons_to_trade and update_lesson_outcomes all join on, so a citation
    written by one run could never match the lesson stored by another — and
    because every one of those APIs is fail-soft, the mismatch surfaced as a
    return value of 0, indistinguishable from "no lessons matched".

    sha1 is used as a content digest, not a security primitive.
    """
    digest = hashlib.sha1(str(fingerprint or "").encode("utf-8")).hexdigest()
    return f"KB-{int(digest[:15], 16) % 10 ** 10:010d}"


def _active(entries: list) -> list:
    return [e for e in entries
            if isinstance(e, dict) and not e.get("superseded_by")
            and not e.get("retired")]


# ---------------------------------------------------------------------------
# Evidence lifecycle: citations -> trade links -> graded outcomes -> status
# ---------------------------------------------------------------------------
def _evidence_status(outcome: dict | None) -> str | None:
    """validated / underperforming / mixed once MIN_GRADED_TRADES outcomes
    exist; None (anecdote — no judgment) below that. Pure."""
    if not isinstance(outcome, dict):
        return None
    n = int(outcome.get("n") or 0)
    if n < MIN_GRADED_TRADES:
        return None
    wr = float(outcome.get("wins") or 0) / n
    if wr >= VALIDATE_WIN_RATE:
        return "validated"
    if wr < UNDERPERFORM_WIN_RATE:
        return "underperforming"
    return "mixed"


def _iter_strings(obj) -> list:
    """Every string reachable in a nested dict/list. Used to read a proposal's
    named text fields without caring whether a field is a str, a list of
    catalysts, or the thesis_invalidators dict."""
    out: list = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_iter_strings(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_iter_strings(v))
    return out


def citations_in_proposal(proposal: dict | None) -> list:
    """Lesson ids cited inside a proposal's own reasoning fields, sorted.

    This is the STRICT half of the link: an id here was written into the trade's
    own thesis, so the lesson demonstrably drove that decision.
    """
    try:
        if not isinstance(proposal, dict):
            return []
        blob = "\n".join(_iter_strings(
            {k: proposal.get(k) for k in PROPOSAL_TEXT_FIELDS if k in proposal}))
        return sorted(set(CITATION_RE.findall(blob)))
    except Exception:
        return []


def citations_near_ticker(text: str, ticker: str) -> list:
    """Lesson ids cited in a PARAGRAPH of the run's prose that names `ticker`.

    THIS IS THE FIX FOR THE DEAD GRADING LOOP, and the measurement that forced
    it (audited 2026-08-03): across 165 runs the brain cited lessons 64 times and
    `linked_trades` stayed empty on all 11 lessons, because the two halves of the
    loop read different text. `record_citations` scans the whole brain response
    (so times_cited works, 1-14 per lesson), while the trade link only ever
    scanned the proposal object — and NOT ONE of the 19 logged proposals in the
    entire journal contains a KB- id. Every citation landed in prose:
    latest_reasoning, watchlist[].thoughts, no_trade_reason, rejected_ideas[].
    A lesson could therefore be cited every run and never be graded.

    Paragraph scoping is what keeps this honest rather than a guess. Crediting a
    trade with every lesson mentioned anywhere in the run would link lessons the
    brain cited while REJECTING a different name — CLAUDE.md is explicit that
    "citing a lesson that did not actually drive the decision corrupts your own
    grading loop", and a fabricated link is worse than a missing one because the
    win rate it feeds looks identical to a real one. So an id counts only when
    the ticker is named in the same paragraph. A paragraph naming several
    tickers credits each, but only tickers that actually FILLED are ever linked.
    """
    try:
        t = str(ticker or "").upper().strip()
        if not t or not str(text or ""):
            return []
        tick_re = re.compile(r"(?<![A-Za-z0-9])\$?" + re.escape(t)
                             + r"(?![A-Za-z0-9])")
        found: set = set()
        for para in re.split(r"\n\s*\n", str(text)):
            if tick_re.search(para):
                found.update(CITATION_RE.findall(para))
        return sorted(found)
    except Exception:
        return []


def citations_for_trade(proposal: dict | None, response_text: str | None = None) -> list:
    """The ids to link to an executed BUY: cited in the proposal itself, PLUS
    cited in run prose that names the ticker. Union, sorted. Never raises.

    Orchestrator-facing entry point — it replaces the bare
    `CITATION_RE.findall(json.dumps(prop))` that produced zero links in 165 runs.
    """
    try:
        ids = set(citations_in_proposal(proposal))
        ticker = str((proposal or {}).get("ticker") or "")
        if response_text and ticker:
            ids.update(citations_near_ticker(response_text, ticker))
        return sorted(ids)
    except Exception:
        return []


def record_citations(text: str, run_id: str | None = None) -> int:
    """Scan brain output for KB-id citations and bump times_cited/last_cited on
    the cited active lessons. Returns how many lessons matched. Never raises."""
    try:
        ids = set(CITATION_RE.findall(str(text or "")))
        if not ids:
            return 0
        doc = _load()
        n = 0
        for e in _active(doc["entries"]):
            if e.get("id") in ids:
                e["times_cited"] = int(e.get("times_cited") or 0) + 1
                e["last_cited"] = _now()
                n += 1
        if n:
            _save(doc)
        return n
    except Exception:
        return 0


def link_lessons_to_trade(ticker: str, lesson_ids: list, run_id: str | None = None,
                          linked_on: str | None = None,
                          trade_ref: str | None = None) -> int:
    """A BUY was executed whose proposal cited these lessons: remember the link
    so the eventual closed-trade outcome can grade them. Never raises.

    `trade_ref` is the order/proposal id of the fill this link belongs to, kept
    so a grade is auditable back to a specific trade rather than to a ticker and
    a date.

    IDEMPOTENT on (ticker, run_id, trade_ref). Appending blindly double-counted
    in two live situations: a re-run of the same run_id (manual reruns are a
    documented workflow, scripts/manual_run.sh), and scale-in adds, which the
    validator explicitly permits up to 2 per position — each add is a separate
    fill on the same ticker in the same run. Both inflate `outcome.n` with
    duplicate rows that the grader then matches against DIFFERENT closed trades,
    so a lesson could cross the 3-trade evidence threshold on one real trade.
    """
    try:
        t = str(ticker or "").upper()
        ids = {i for i in (lesson_ids or []) if CITATION_RE.fullmatch(str(i))}
        if not t or not ids:
            return 0
        doc = _load()
        n = 0
        for e in doc.get("entries") or []:
            if not isinstance(e, dict) or e.get("id") not in ids:
                continue
            if e.get("superseded_by") or e.get("retired"):
                continue
            links = [L for L in (e.get("linked_trades") or []) if isinstance(L, dict)]
            key = (t, run_id, trade_ref)
            if any((L.get("ticker"), L.get("run_id"), L.get("trade_ref")) == key
                   for L in links):
                continue
            row = {"ticker": t, "linked_on": linked_on or _now()[:10],
                   "run_id": run_id}
            if trade_ref:
                row["trade_ref"] = str(trade_ref)[:60]
            links.append(row)
            e["linked_trades"] = links[-MAX_LINKS_PER_LESSON:]
            n += 1
        if n:
            _save(doc)
        return n
    except Exception:
        return 0


def update_lesson_outcomes(closed_trades: list) -> dict:
    """Grade lesson links against closed trades (match on ticker + entry date
    within 5 days of the link) and refresh each lesson's outcome stats and
    evidence_status. Called from the daily study session. Never raises."""
    try:
        trades = [t for t in (closed_trades or []) if isinstance(t, dict)]
        if not trades:
            return {"graded": 0}
        doc = _load()
        graded = 0
        # EVERY entry carrying links, not just the active ones. A lesson can be
        # superseded or retired in the days between its trade opening and that
        # trade closing (Friday consolidation runs weekly; swing horizons are
        # 3-90 days), and grading only _active() froze that evidence forever —
        # exactly the outcome that would have justified the retirement, or
        # overturned it. Superseded lessons stay in history precisely so their
        # record can be completed.
        for e in doc.get("entries") or []:
            if not isinstance(e, dict) or not e.get("linked_trades"):
                continue
            links = [L for L in (e.get("linked_trades") or []) if isinstance(L, dict)]
            changed = False
            used: set[int] = set()  # a trade grades at most ONE link per lesson
            for L in links:
                if L.get("outcome") is not None:
                    continue
                best, best_gap = None, 999
                for idx, tr in enumerate(trades):
                    if idx in used or str(tr.get("ticker", "")).upper() != L.get("ticker"):
                        continue
                    opened, linked = str(tr.get("opened_at") or ""), str(L.get("linked_on") or "")
                    gap = abs(_date_gap_days(opened, linked)) if opened and linked else 999
                    if gap > 5 or gap >= best_gap:
                        continue
                    if tr.get("total_pnl_usd", tr.get("pnl_usd")) is None:
                        continue
                    best, best_gap = idx, gap
                if best is None:
                    continue
                tr = trades[best]
                used.add(best)
                pnl = tr.get("total_pnl_usd", tr.get("pnl_usd"))
                L["outcome"] = {"win": pnl > 0, "pnl_usd": round(float(pnl), 2),
                                "closed_at": tr.get("closed_at"),
                                "verdict": tr.get("verdict")}
                changed = True
                graded += 1
            if changed or links:
                done = [L for L in links if isinstance(L.get("outcome"), dict)]
                if done:
                    e["outcome"] = {"n": len(done),
                                    "wins": sum(1 for L in done if L["outcome"].get("win")),
                                    "graded_at": _now()}
                    e["evidence_status"] = _evidence_status(e["outcome"])
                if changed:
                    e["linked_trades"] = links
        if graded:
            _save(doc)
            publish_learning_journal(doc)
        return {"graded": graded}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "graded": 0}


def _date_gap_days(a: str, b: str) -> int:
    try:
        from datetime import date
        da = date.fromisoformat(str(a)[:10])
        db = date.fromisoformat(str(b)[:10])
        return (da - db).days
    except Exception:
        return 999


def apply_conflicts(conflicts: list, new_id: str, run_id: str | None = None) -> dict:
    """A study session judged the new lesson against existing ones. Code applies
    at most MAX_SUPERSEDES_PER_STUDY justified 'supersede' verdicts; 'coexist'
    verdicts are recorded as tension notes on both lessons. Guardrails:
    a supersede needs a real why (>=40 chars) and cannot touch a lesson the
    evidence has VALIDATED — data beats a fresh opinion. Never raises."""
    out = {"superseded": [], "coexist": [], "rejected": []}
    try:
        doc = _load()
        by_id = {e.get("id"): e for e in _active(doc["entries"])}
        new = by_id.get(new_id)
        budget = MAX_SUPERSEDES_PER_STUDY
        changed = False
        for c in (conflicts or []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            why = str(c.get("why") or "").strip()
            res = str(c.get("resolution") or "")
            tgt = by_id.get(cid)
            if tgt is None or cid == new_id:
                out["rejected"].append({"id": cid, "why": "unknown_or_self"})
            elif res == "supersede":
                if len(why) < 40:
                    out["rejected"].append({"id": cid, "why": "justification_too_thin"})
                elif tgt.get("evidence_status") == "validated":
                    out["rejected"].append({"id": cid, "why": "target_validated_by_evidence"})
                elif budget <= 0:
                    out["rejected"].append({"id": cid, "why": "supersede_budget_exhausted"})
                else:
                    tgt["superseded_by"] = new_id
                    tgt["superseded_at"] = _now()
                    tgt["supersede_reason"] = "contradicted_by_study"
                    tgt["supersede_why"] = why[:300]
                    budget -= 1
                    changed = True
                    out["superseded"].append(cid)
            elif res == "coexist":
                note = {"with": cid, "why": why[:300], "noted_at": _now()[:10]}
                if new is not None:
                    new.setdefault("tensions", []).append(note)
                    new["tensions"] = new["tensions"][-5:]
                    changed = True
                out["coexist"].append(cid)
        if changed:
            _save(doc)
            publish_learning_journal(doc)
    except Exception as e:
        out["error"] = str(e)[:150]
    return out


def apply_consolidation(plan: dict, run_id: str | None = None) -> dict:
    """Friday consolidation: the study session reviews the whole active base
    (with citation/outcome stats) and proposes merges and retirements; code
    applies them under hard caps so the agent can never churn its own memory:
    max MAX_CONSOLIDATION_ACTIONS total, lessons younger than
    CONSOLIDATION_MIN_AGE_DAYS untouchable, evidence-VALIDATED lessons cannot
    be retired (only merged, which preserves their content), retire needs a
    real why. Nothing is ever deleted — superseded/retired stay in history."""
    out = {"merged": [], "retired": [], "rejected": []}
    try:
        doc = _load()
        by_id = {e.get("id"): e for e in _active(doc["entries"])}
        actions = 0

        def _too_fresh(e) -> bool:
            # An UNKNOWN age counts as too fresh. _date_gap_days returns its 999
            # sentinel for a missing/unparseable learned_at, and 999 < 7 is
            # False — so a lesson with no date read as ancient and became
            # instantly retirable, which inverts the guard on precisely the rows
            # most likely to be missing one (journal-recovered lessons). Nothing
            # here is ever deleted, so failing closed costs a week's delay;
            # failing open costs the lesson.
            learned = str(e.get("learned_at") or "")[:10]
            if not learned:
                return True
            gap = _date_gap_days(_now()[:10], learned)
            return gap >= 999 or gap < CONSOLIDATION_MIN_AGE_DAYS

        for m in (plan.get("merges") or []):
            if actions >= MAX_CONSOLIDATION_ACTIONS:
                out["rejected"].append({"ids": m.get("ids"), "why": "action_budget_exhausted"})
                continue
            ids = [i for i in (m.get("ids") or []) if i in by_id]
            lesson = m.get("lesson")
            if len(ids) < 2 or not isinstance(lesson, dict):
                out["rejected"].append({"ids": m.get("ids"), "why": "need_2plus_known_ids_and_lesson"})
                continue
            if any(_too_fresh(by_id[i]) for i in ids):
                out["rejected"].append({"ids": ids, "why": "member_too_fresh"})
                continue
            res = add_lesson({**lesson, "topic": str(lesson.get("topic") or "")},
                             run_id)
            if res.get("status") != "ok":
                out["rejected"].append({"ids": ids, "why": f"merged_lesson_{res.get('status')}"})
                continue
            doc = _load()  # re-read: add_lesson wrote the store
            by_id = {e.get("id"): e for e in _active(doc["entries"])}
            merged_entry = by_id.get(res["id"])
            if merged_entry is not None:
                merged_entry["merged_from"] = ids
            for i in ids:
                tgt = by_id.get(i)
                if tgt is not None:
                    tgt["superseded_by"] = res["id"]
                    tgt["superseded_at"] = _now()
                    tgt["supersede_reason"] = "merged"
            actions += 1
            out["merged"].append({"into": res["id"], "from": ids})
            _save(doc)
            by_id = {e.get("id"): e for e in _active(doc["entries"])}

        for r in (plan.get("retires") or []):
            if actions >= MAX_CONSOLIDATION_ACTIONS:
                out["rejected"].append({"id": r.get("id"), "why": "action_budget_exhausted"})
                continue
            cid = str((r or {}).get("id") or "")
            why = str((r or {}).get("why") or "").strip()
            tgt = by_id.get(cid)
            if tgt is None:
                out["rejected"].append({"id": cid, "why": "unknown_id"})
            elif len(why) < 40:
                out["rejected"].append({"id": cid, "why": "justification_too_thin"})
            elif tgt.get("evidence_status") == "validated":
                out["rejected"].append({"id": cid, "why": "validated_by_evidence"})
            elif _too_fresh(tgt):
                out["rejected"].append({"id": cid, "why": "too_fresh"})
            else:
                tgt["retired"] = True
                tgt["retired_at"] = _now()
                tgt["retire_why"] = why[:300]
                actions += 1
                out["retired"].append(cid)
        if out["merged"] or out["retired"]:
            _save(doc)
            publish_learning_journal(doc)
    except Exception as e:
        out["error"] = str(e)[:150]
    return out


def backfill_learned_at(persist: bool = True) -> dict:
    """Stamp `learned_at` on stored lessons that never got one, DERIVING the date
    from the lesson's own run_id (20260731-098397 -> 2026-07-31).

    Deliberately narrow. A lesson whose run_id is missing or malformed is left
    alone and reported under `unresolvable` — the recency logic being repaired
    here (selection rank, the 7-day consolidation guard, learning_loop_freshness)
    is only worth repairing with a date that is TRUE. Guessing _now() would date
    a July lesson to whenever maintenance happened to run and would quietly make
    a stale curriculum look freshly studied, which is the exact failure
    health.learning_loop exists to catch.

    Idempotent: entries that already carry a learned_at are never rewritten.
    """
    try:
        doc = _load()
        filled, unresolvable = [], []
        for e in doc.get("entries") or []:
            if not isinstance(e, dict) or e.get("learned_at"):
                continue
            derived = _learned_at_from_run_id(e.get("run_id"))
            if derived:
                e["learned_at"] = derived
                e["learned_at_backfilled_from"] = "run_id"
                filled.append({"id": e.get("id"), "learned_at": derived[:10]})
            else:
                unresolvable.append(e.get("id"))
        if filled and persist:
            _save(doc)
            publish_learning_journal(doc)
        return {"filled": len(filled), "entries": filled,
                "unresolvable": unresolvable}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "filled": 0,
                "entries": [], "unresolvable": []}


def active_lessons() -> list:
    """Full active (non-superseded, non-retired) lesson records, oldest first.
    Used by the Friday consolidation session, which needs complete text plus
    the citation/outcome evidence."""
    try:
        return [dict(e) for e in _active(_load()["entries"])]
    except Exception:
        return []


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
            "id": _lesson_id(fp),
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


def _selection_rank(e: dict) -> tuple:
    """Evidence beats recency: validated first, then ungraded/mixed by recency,
    underperforming last (still shown — with a warning — until consolidation
    retires them). Pure."""
    status = e.get("evidence_status")
    tier = {"validated": 0, None: 1, "mixed": 1, "underperforming": 2}.get(status, 1)
    return (tier, str(e.get("learned_at") or ""))


def _lesson_row(e: dict) -> dict:
    row = {"id": e.get("id"), "discipline": e.get("discipline"),
           "topic": e.get("topic"), "summary": e.get("summary"),
           "how_to_apply": e.get("how_to_apply"),
           "learned_at": str(e.get("learned_at") or "")[:10]}
    if e.get("evidence_status"):
        row["evidence_status"] = e["evidence_status"]
        row["evidence"] = e.get("outcome")
    if e.get("evidence_status") == "underperforming":
        row["warning"] = ("trades citing this lesson have mostly LOST - weigh it "
                          "skeptically; it is a retirement candidate")
    if e.get("times_cited"):
        row["times_cited"] = e["times_cited"]
    if e.get("tensions"):
        row["tensions"] = e["tensions"][-2:]
    return row


def brain_facing_knowledge_base(limit: int = 8, include_index: bool = False) -> dict:
    """Inject into trading context: evidence-ranked lessons plus curriculum
    coverage. Compact by design (learning-pack discipline). include_index adds
    a one-line id+topic index of the WHOLE active base (study sessions use it
    to avoid re-learning and to name conflicts)."""
    try:
        doc = _load()
        active = _active(doc["entries"])
        by_tier: dict = {}
        for e in active:
            by_tier.setdefault(_selection_rank(e)[0], []).append(e)
        picked = []
        for tier in sorted(by_tier):
            picked.extend(sorted(by_tier[tier],
                                 key=lambda e: str(e.get("learned_at") or ""),
                                 reverse=True))
        out = {
            "note": ("KNOWLEDGE BASE from the daily study sessions (system 6). "
                     "Durable craft lessons, EVIDENCE-RANKED: validated first "
                     "(citing trades won), underperforming last (citing trades "
                     "lost - weigh skeptically). Apply the how_to_apply lines "
                     "when the situation matches; CITE the lesson id when one "
                     "drives a decision - citations are how lessons get graded."),
            "recent": [_lesson_row(e) for e in picked[:limit]],
            "n_active": len(active),
            "discipline_counts": discipline_counts(doc["entries"]),
        }
        if include_index:
            out["topics_index"] = [
                {"id": e.get("id"), "discipline": e.get("discipline"),
                 "topic": e.get("topic"), "evidence_status": e.get("evidence_status"),
                 "times_cited": e.get("times_cited") or 0}
                for e in reversed(active)
            ]
        return out
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150], "recent": []}


if __name__ == "__main__":
    print(json.dumps(brain_facing_knowledge_base(), indent=2))
