"""Lesson scorecard — does each lesson actually help? (closes the learning loop)

The knowledge base (KB-*) and the adopted-lessons pipeline (LP-*) both WRITE
lessons every week, and both inject them into every trading run. Until this
module nothing measured whether a lesson made decisions better or worse, so
lessons only accumulated. This is the measuring half and the acting half:

  1. ATTRIBUTION — which decisions did each lesson shape?
       * dashboard/data/run_*.json archives (every routine/Actions run since
         July): BUY proposals (proposal text + run prose paragraphs naming the
         ticker — the same paragraph rule as knowledge_base.citations_near_ticker),
         rejected_ideas[].reason (a SKIP), watchlist[] text (a WAIT).
       * journal/lesson_citations/*.jsonl — the per-run ledger written by the
         act-on path from 2026-10-05 (structured `lessons_applied` + the same
         item-level scan), the precise source going forward.
       * shadow-book rows whose own reason carries an id, and KB linked_trades.
  2. OUTCOMES — hindsight for each attributed decision.
       * act  -> the closed trade it opened (R-multiple; win = R > 0).
       * skip / wait -> the shadow-book row for that ticker/window:
         good_skip = the lesson kept us out of a loser (correct),
         regret_miss = the lesson kept us out of a winner (incorrect).
         Market-wide verdicts (shadow_portfolio._verdict_attribution) are
         EXCLUDED: beta is not lesson skill. Mixed/flat paths are neutral.
  3. VERDICT per lesson vs the book's own baseline, conservatively:
       n_graded < MIN_GRADED            -> insufficient_data (no action, ever)
       rate >= baseline + MARGIN AND one-sided 90% Wilson LOWER bound > baseline
                                         -> helping
       rate <= baseline - MARGIN AND Wilson UPPER bound < baseline
                                         -> hurting
       otherwise                         -> no_clear_edge
  4. ACTIONS (apply mode only, all capped and reversible; nothing is deleted):
       * hurting  -> superseded (superseded_by="LESSON-SCORECARD"); the brain
         sees a [SUPERSEDED ... by lesson scorecard] marker in its pack.
         Max MAX_RETIREMENTS_PER_WEEK per ISO week across both stores; never a
         lesson younger than MIN_AGE_DAYS_TO_RETIRE, never an evidence-
         VALIDATED KB lesson, never a risk_management lesson (flagged for
         review instead — a capital-protection lesson always "costs" upside on
         skips, so skip-regret evidence is biased against it).
       * helping  -> scorecard_priority="boost": ranked first in the brain pack.
       * stale    -> scorecard_priority="deprioritize": ranked last. Only when
         citation capture is HEALTHY (a citation recorded in the last
         CAPTURE_HEALTH_DAYS) — an outage in capture is not evidence a lesson
         is unused, and that outage is exactly what 9/23-10/5 looked like.
       * adopted pipeline log lines / KB duplicates -> deprioritized as noise.
       * a scorecard-retired lesson whose later evidence turns `helping` is
         reinstated (counts against the same weekly cap).

This module edits LESSON METADATA ONLY. It never reads or writes
autonomy_config.json, validator caps, stops, or position sizing. Deterministic,
no LLM. Fail-soft public APIs except where noted.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # `python tools/lesson_scorecard.py`
    sys.path.insert(0, str(ROOT))
SCORECARD_JSON = ROOT / "data" / "lesson_scorecard.json"
SCORECARD_MD = ROOT / "data" / "lesson_scorecard.md"
RUN_ARCHIVE_GLOB = str(ROOT / "dashboard" / "data" / "run_*.json")
SHADOW_JSON = ROOT / "data" / "shadow_portfolio.json"
EXIT_AUTOPSY_DIR = ROOT / "journal" / "exit_autopsies"
CITATION_LEDGER_SUBDIR = "lesson_citations"

LESSON_ID_RE = re.compile(r"\b(?:KB|LP)-\d{10}\b")
SCORECARD_SUPERSEDER = "LESSON-SCORECARD"

# --- conservative thresholds -------------------------------------------------
MIN_GRADED = 8                    # graded decisions before ANY verdict
EDGE_MARGIN = 0.15                # |rate - baseline| needed for a verdict
WILSON_Z = 1.2816                 # one-sided 90%
MAX_RETIREMENTS_PER_WEEK = 3      # across KB + adopted, per ISO week
MIN_AGE_DAYS_TO_RETIRE = 14
STALE_AFTER_DAYS = 21             # age AND no citation within this window
CAPTURE_HEALTH_DAYS = 7           # capture "healthy" = a citation this recent
SKIP_MATCH_WINDOW_DAYS = 7        # a skip on D matches a shadow opened D-7..D
ACT_MATCH_WINDOW_DAYS = 5         # a BUY citation on D matches a trade opened D..D+5
SKIP_DEDUPE_DAYS = 14             # one graded shadow per (lesson, ticker) per 14d:
                                  # a name re-watched every run is ONE decision
DEFAULT_TRADE_BASELINE = 0.50     # until >= 5 closed trades exist
HISTORY_KEEP = 26                 # weekly history rows kept
PROTECTED_DISCIPLINES = ("risk_management",)

DECISION_ACT, DECISION_SKIP, DECISION_WAIT = "act", "skip", "wait"
CORRECT_SKIP, WRONG_SKIP = "good_skip", "regret_miss"

# Adopted-pipeline rows that are logs, not lessons (they were auto-adopted from
# improvement notes): kept, but never worth a seat in the 12-line brain pack.
_ADOPTED_NOISE_PREFIXES = ("[learning-adopt]", "[lesson-scorecard]",
                           "dynamic universe add:")
_ADOPTED_KB_DUP_PREFIX = "daily study ("


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _run_date(run_id: str | None) -> date | None:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-", str(run_id or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def wilson_bounds(correct: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """(lower, upper) Wilson score interval. (0, 1) for n == 0. Pure."""
    if n <= 0:
        return 0.0, 1.0
    p = correct / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom)


def classify(correct: int, n: int, baseline: float) -> str:
    """helping | hurting | no_clear_edge | insufficient_data. Pure."""
    if n < MIN_GRADED:
        return "insufficient_data"
    rate = correct / n
    lo, hi = wilson_bounds(correct, n)
    if rate >= baseline + EDGE_MARGIN and lo > baseline:
        return "helping"
    if rate <= baseline - EDGE_MARGIN and hi < baseline:
        return "hurting"
    return "no_clear_edge"


def ids_in(text) -> list:
    return sorted(set(LESSON_ID_RE.findall(str(text or ""))))


def _strings(obj) -> list:
    out: list = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_strings(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_strings(v))
    return out


def _ids_near_ticker(text: str, ticker: str) -> list:
    """KB-/LP- ids in a paragraph of `text` that names `ticker` (same rule as
    knowledge_base.citations_near_ticker, extended to adopted-lesson ids)."""
    t = str(ticker or "").upper().strip()
    if not t or not text:
        return []
    tick_re = re.compile(r"(?<![A-Za-z0-9])\$?" + re.escape(t) + r"(?![A-Za-z0-9])")
    found: set = set()
    for para in re.split(r"\n\s*\n", str(text)):
        if tick_re.search(para):
            found.update(LESSON_ID_RE.findall(para))
    return sorted(found)


# ---------------------------------------------------------------------------
# 1. citation extraction from ONE run (used by the act-on ledger AND archives)
# ---------------------------------------------------------------------------
_DECISION_ALIASES = {
    "buy": DECISION_ACT, "act": DECISION_ACT, "add": DECISION_ACT,
    "starter": DECISION_ACT, "probe": DECISION_ACT,
    "skip": DECISION_SKIP, "reject": DECISION_SKIP, "pass": DECISION_SKIP,
    "wait": DECISION_WAIT, "watch": DECISION_WAIT, "hold_off": DECISION_WAIT,
}


def extract_run_citations(parsed: dict | None, response: str | None = None,
                          reasoning: str | None = None) -> list:
    """Per-decision lesson citations in one brain response. Never raises.

    Rows: {lesson_id, ticker, decision (act|skip|wait|other), source}.
      * lessons_applied[] — the structured field (preferred; explicit decision)
      * proposals[] BUY   — ids in the proposal text, plus ids in a prose
                            paragraph naming the ticker (`reasoning`, else the
                            full response) -> act
      * rejected_ideas[]  — ids in that row -> skip
      * watchlist[]       — ids in that row (status != buy) -> wait
      * any other id anywhere in the response -> decision "other", ticker None
        (counted as a citation for staleness, never graded)
    Deduped on (lesson_id, ticker, decision).
    """
    rows: dict = {}

    def _add(lid, ticker, decision, source):
        if not LESSON_ID_RE.fullmatch(str(lid or "")):
            return
        t = str(ticker or "").upper().strip() or None
        key = (lid, t, decision)
        if key not in rows:
            rows[key] = {"lesson_id": lid, "ticker": t, "decision": decision,
                         "source": source}

    try:
        parsed = parsed if isinstance(parsed, dict) else {}
        prose = reasoning if reasoning is not None else (response or "")
        for row in parsed.get("lessons_applied") or []:
            if not isinstance(row, dict):
                continue
            dec = _DECISION_ALIASES.get(
                str(row.get("decision") or row.get("effect") or "").lower().strip(),
                "other")
            for lid in ids_in(row.get("id") or row.get("lesson_id")):
                _add(lid, row.get("ticker"), dec, "lessons_applied")
        for item in parsed.get("proposals") or []:
            if not isinstance(item, dict):
                continue
            p = item.get("proposal") if isinstance(item.get("proposal"), dict) else item
            if str(p.get("action", "")).upper() != "BUY":
                continue
            t = p.get("ticker")
            found = set(ids_in("\n".join(_strings(
                {k: v for k, v in p.items() if k not in ("ticker",)}))))
            found.update(_ids_near_ticker(prose, t))
            for lid in found:
                _add(lid, t, DECISION_ACT, "proposal")
        for r in parsed.get("rejected_ideas") or []:
            if isinstance(r, dict):
                for lid in ids_in("\n".join(_strings(
                        {k: v for k, v in r.items() if k != "ticker"}))):
                    _add(lid, r.get("ticker"), DECISION_SKIP, "rejected_idea")
        for w in parsed.get("watchlist") or []:
            if not isinstance(w, dict) or str(w.get("status") or "").lower() == "buy":
                continue
            for lid in ids_in("\n".join(_strings(
                    {k: v for k, v in w.items() if k != "ticker"}))):
                _add(lid, w.get("ticker"), DECISION_WAIT, "watchlist")
        seen = {k[0] for k in rows}
        for lid in ids_in(response):
            if lid not in seen:
                _add(lid, None, "other", "prose")
    except Exception:
        pass
    return list(rows.values())


def record_run_citations(parsed: dict | None, response: str | None,
                         run_id: str | None) -> dict:
    """Act-on path hook: write this run's per-decision citations to the
    append-only ledger (journal/lesson_citations/) and bump times_cited on
    cited ADOPTED lessons (KB citations are bumped by
    knowledge_base.record_citations). Always writes a ledger row — a run with
    ZERO citations is itself the signal the 2026-09-23..10-05 outage never
    produced. Never raises."""
    try:
        rows = extract_run_citations(parsed, response)
        try:
            import journal  # leaf module; tests redirect journal.JOURNAL
            journal._write(CITATION_LEDGER_SUBDIR, {
                "run_id": run_id, "n_citations": len(rows),
                "n_structured": sum(1 for r in rows if r["source"] == "lessons_applied"),
                "citations": rows})
        except Exception:
            pass
        n_lp = 0
        try:
            from tools.learning_adopt import record_adopted_citations
            n_lp = record_adopted_citations(
                [r["lesson_id"] for r in rows if r["lesson_id"].startswith("LP-")])
        except Exception:
            pass
        return {"n_citations": len(rows), "n_adopted_bumped": n_lp,
                "n_decision_linked": sum(1 for r in rows if r["ticker"])}
    except Exception as e:
        return {"n_citations": 0, "error": str(e)[:150]}


# ---------------------------------------------------------------------------
# 2. loaders (every path injectable for tests)
# ---------------------------------------------------------------------------
def _load_json(path, default):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default


def load_archive_attributions(archive_glob: str = None) -> list:
    """Attributions mined from the dashboard run archives. Each row:
    {lesson_id, ticker, decision, run_id, date, source}."""
    out = []
    for f in sorted(glob.glob(archive_glob or RUN_ARCHIVE_GLOB)):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        run_id = str(d.get("run_id") or Path(f).stem.replace("run_", ""))
        day = _d(d.get("as_of_et")) or _run_date(run_id) or _d(d.get("generated_at"))
        if day is None:
            continue
        parsed = {"proposals": d.get("proposals") or [],
                  "rejected_ideas": d.get("rejected_ideas") or [],
                  "watchlist": d.get("watchlist") or []}
        reasoning = str(d.get("latest_reasoning") or "")
        for r in extract_run_citations(parsed, response=reasoning, reasoning=reasoning):
            if r["decision"] == "other":
                r = dict(r, source="archive_prose")
            out.append({**r, "run_id": run_id, "date": day.isoformat(),
                        "source": "archive:" + r["source"]})
    return out


def load_ledger_attributions(journal_dir=None) -> list:
    jdir = Path(journal_dir) if journal_dir else _journal_dir()
    out = []
    for f in sorted((jdir / CITATION_LEDGER_SUBDIR).glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            run_id = rec.get("run_id")
            day = _run_date(run_id) or _d(rec.get("ts"))
            if day is None:
                continue
            for r in rec.get("citations") or []:
                if isinstance(r, dict) and r.get("lesson_id"):
                    out.append({**r, "run_id": run_id, "date": day.isoformat(),
                                "source": "ledger:" + str(r.get("source"))})
    return out


def _journal_dir() -> Path:
    try:
        import journal
        return Path(journal.JOURNAL)
    except Exception:
        return ROOT / "journal"


def load_closed_trades(exit_autopsy_dir=None, closed_trades: list | None = None) -> list:
    """Closed trades with an R-multiple where computable. Sources: the broker
    history (runlib.analytics.compute_closed_trades) and journal/exit_autopsies
    (which survived the 2026-09-22 book reset). Ghost-reconcile write-offs are
    excluded — their exit price is a bookkeeping value, not a market outcome."""
    trades = []
    if closed_trades is None:
        try:
            from runlib.analytics import compute_closed_trades
            closed_trades = compute_closed_trades()
        except Exception:
            closed_trades = []
    for t in closed_trades or []:
        if not isinstance(t, dict) or not t.get("ticker"):
            continue
        pnl = t.get("total_pnl_usd", t.get("pnl_usd"))
        trades.append({"ticker": str(t["ticker"]).upper(),
                       "opened_at": str(t.get("opened_at") or "")[:10],
                       "closed_at": str(t.get("closed_at") or "")[:10],
                       "r": t.get("r_multiple"), "pnl": pnl, "src": "history"})
    seen = {(t["ticker"], t["closed_at"]) for t in trades}
    adir = Path(exit_autopsy_dir) if exit_autopsy_dir else EXIT_AUTOPSY_DIR
    for f in sorted(adir.glob("*.jsonl")) if adir.exists() else []:
        for line in f.read_text().splitlines():
            try:
                a = json.loads(line)
            except Exception:
                continue
            if str(a.get("action", "")).upper() != "SELL_TO_CLOSE":
                continue
            reason = str(a.get("forced_reason") or "").lower()
            if "ghost" in reason or "writeoff" in reason or "write_off" in reason:
                continue
            tk = str(a.get("ticker") or "").upper()
            closed = str(a.get("filled_at") or a.get("ts") or "")[:10]
            if not tk or (tk, closed) in seen:
                continue
            try:
                exit_px = float(a.get("fill_price"))
                entry = float(a.get("avg_cost"))
            except (TypeError, ValueError):
                continue
            stop = (a.get("entry_plan") or {}).get("stop_loss")
            r = None
            try:
                if stop and entry > float(stop):
                    r = round((exit_px - entry) / (entry - float(stop)), 2)
            except (TypeError, ValueError):
                r = None
            pnl = a.get("realized_pnl_usd")
            trades.append({"ticker": tk,
                           "opened_at": str(a.get("position_opened_at") or "")[:10],
                           "closed_at": closed, "r": r,
                           "pnl": pnl if pnl is not None else (exit_px - entry),
                           "src": "exit_autopsy"})
            seen.add((tk, closed))
    return trades


def _trade_win(t: dict) -> bool | None:
    if isinstance(t.get("r"), (int, float)):
        return t["r"] > 0
    if isinstance(t.get("pnl"), (int, float)):
        return t["pnl"] > 0
    return None


def load_shadows(shadow_json=None) -> list:
    doc = _load_json(shadow_json or SHADOW_JSON, {})
    if not isinstance(doc, dict):
        return []
    rows = []
    for key in ("closed", "positions"):
        for s in doc.get(key) or []:
            if isinstance(s, dict) and s.get("ticker"):
                rows.append(s)
    return rows


def shadow_attributions(shadows: list) -> list:
    """Ids written into a shadow row's own reason attach to THAT row."""
    out = []
    for s in shadows:
        for lid in ids_in(s.get("reason")):
            day = _d(s.get("opened_at")) or _run_date(s.get("run_id"))
            if day is None:
                continue
            dec = DECISION_SKIP if s.get("source") == "rejected_idea" else DECISION_WAIT
            out.append({"lesson_id": lid, "ticker": str(s["ticker"]).upper(),
                        "decision": dec, "run_id": s.get("run_id"),
                        "date": day.isoformat(), "source": "shadow_reason",
                        "shadow_id": s.get("id")})
    return out


def kb_link_attributions(kb_entries: list) -> list:
    out = []
    for e in kb_entries:
        for L in e.get("linked_trades") or []:
            if isinstance(L, dict) and L.get("ticker"):
                day = _d(L.get("linked_on")) or _run_date(L.get("run_id"))
                if day:
                    out.append({"lesson_id": e.get("id"), "ticker": L["ticker"],
                                "decision": DECISION_ACT, "run_id": L.get("run_id"),
                                "date": day.isoformat(), "source": "kb_linked_trade"})
    return out


# ---------------------------------------------------------------------------
# 3. outcome join + per-lesson evidence (pure given inputs)
# ---------------------------------------------------------------------------
def _shadow_grade(s: dict) -> str:
    """correct | incorrect | neutral | beta | pending | acted."""
    v = s.get("verdict")
    if s.get("status") == "open" or not v:
        return "pending"
    if v == "acted_bought":
        return "acted"
    if v not in (CORRECT_SKIP, WRONG_SKIP):
        return "neutral"
    if s.get("verdict_attribution") == "market_wide":
        return "beta"
    return "correct" if v == CORRECT_SKIP else "incorrect"


def _match_shadow(att: dict, by_ticker: dict, by_id: dict) -> dict | None:
    if att.get("shadow_id") and att["shadow_id"] in by_id:
        return by_id[att["shadow_id"]]
    day = _d(att.get("date"))
    cands = by_ticker.get(str(att.get("ticker") or "").upper()) or []
    same_run = [s for s in cands if s.get("run_id") and s.get("run_id") == att.get("run_id")]
    if same_run:
        return same_run[0]
    if day is None:
        return None
    best, gap = None, None
    for s in cands:
        opened = _d(s.get("opened_at"))
        if opened is None:
            continue
        g = (day - opened).days
        if g < 0 or g > SKIP_MATCH_WINDOW_DAYS:
            continue
        closed = _d(s.get("closed_at"))
        if closed is not None and closed < day:
            continue
        if gap is None or g < gap:
            best, gap = s, g
    return best


def _match_trade(att: dict, trades: list) -> dict | None:
    day = _d(att.get("date"))
    if day is None:
        return None
    best, gap = None, None
    for t in trades:
        if t["ticker"] != str(att.get("ticker") or "").upper():
            continue
        opened = _d(t.get("opened_at"))
        if opened is None:
            continue
        g = (opened - day).days
        if g < -1 or g > ACT_MATCH_WINDOW_DAYS:
            continue
        if gap is None or abs(g) < gap:
            best, gap = t, abs(g)
    return best


def baselines(shadows: list, trades: list) -> dict:
    graded = [g for g in (_shadow_grade(s) for s in shadows) if g in ("correct", "incorrect")]
    n_skip = len(graded)
    skip_rate = (sum(1 for g in graded if g == "correct") / n_skip) if n_skip else 0.5
    wins = [w for w in (_trade_win(t) for t in trades) if w is not None]
    trade_rate = (sum(wins) / len(wins)) if len(wins) >= 5 else DEFAULT_TRADE_BASELINE
    return {"skip_correct_rate": round(skip_rate, 4), "n_skip_graded": n_skip,
            "trade_win_rate": round(trade_rate, 4), "n_trades": len(wins),
            "trade_baseline_source": ("book" if len(wins) >= 5 else "default_0.50")}


def build_evidence(attributions: list, shadows: list, trades: list) -> dict:
    """{lesson_id: evidence dict}. Each (lesson, shadow) and (lesson, trade)
    pair counts ONCE however many runs repeated the citation."""
    by_ticker: dict = {}
    by_id: dict = {}
    for s in shadows:
        by_ticker.setdefault(str(s["ticker"]).upper(), []).append(s)
        if s.get("id"):
            by_id[s["id"]] = s
    ev: dict = {}
    used: set = set()
    last_counted: dict = {}  # (lesson, ticker) -> opened date of last graded shadow
    for a in sorted(attributions, key=lambda x: str(x.get("date") or "")):
        lid = a.get("lesson_id")
        if not lid:
            continue
        e = ev.setdefault(lid, {
            "n_citations": 0, "decisions": {}, "last_cited": None,
            "act": {"n": 0, "wins": 0, "rs": [], "pending": 0, "trades": []},
            "skip": {"correct": 0, "incorrect": 0, "neutral": 0, "beta": 0,
                     "pending": 0, "unmatched": 0, "correlated_dupes": 0,
                     "examples": []}})
        e["n_citations"] += 1
        dec = a.get("decision") or "other"
        e["decisions"][dec] = e["decisions"].get(dec, 0) + 1
        if a.get("date") and (e["last_cited"] is None or a["date"] > e["last_cited"]):
            e["last_cited"] = a["date"]
        if dec == DECISION_ACT and a.get("ticker"):
            t = _match_trade(a, trades)
            if t is None:
                k = (lid, "act-pending", a.get("ticker"), a.get("date"))
                if k not in used:
                    used.add(k)
                    e["act"]["pending"] += 1
                continue
            k = (lid, "trade", t["ticker"], t["closed_at"])
            if k in used:
                continue
            used.add(k)
            w = _trade_win(t)
            if w is None:
                continue
            e["act"]["n"] += 1
            e["act"]["wins"] += int(w)
            if isinstance(t.get("r"), (int, float)):
                e["act"]["rs"].append(float(t["r"]))
            e["act"]["trades"].append(f"{t['ticker']} {t['closed_at']} R={t.get('r')}")
        elif dec in (DECISION_SKIP, DECISION_WAIT) and a.get("ticker"):
            s = _match_shadow(a, by_ticker, by_id)
            if s is None:
                e["skip"]["unmatched"] += 1
                continue
            k = (lid, "shadow", s.get("id") or (s.get("ticker"), s.get("opened_at")))
            if k in used:
                continue
            used.add(k)
            g = _shadow_grade(s)
            if g == "acted":
                continue
            if g in ("correct", "incorrect"):
                pair = (lid, str(s.get("ticker")).upper())
                opened = _d(s.get("opened_at"))
                prev = last_counted.get(pair)
                if prev and opened and abs((opened - prev).days) < SKIP_DEDUPE_DAYS:
                    e["skip"]["correlated_dupes"] = e["skip"].get("correlated_dupes", 0) + 1
                    continue
                if opened:
                    last_counted[pair] = opened
            e["skip"][g] = e["skip"].get(g, 0) + 1
            if g in ("correct", "incorrect") and len(e["skip"]["examples"]) < 6:
                e["skip"]["examples"].append(
                    f"{s.get('ticker')} {s.get('opened_at')} {s.get('verdict')}")
    return ev


def score_lesson(e: dict | None, base: dict) -> dict:
    """Combine act + skip evidence into one decision-correctness score against
    a blended baseline (each decision type weighted by its own count)."""
    e = e or {}
    act = e.get("act") or {}
    skip = e.get("skip") or {}
    n_act = int(act.get("n") or 0)
    n_skip = int(skip.get("correct") or 0) + int(skip.get("incorrect") or 0)
    n = n_act + n_skip
    correct = int(act.get("wins") or 0) + int(skip.get("correct") or 0)
    if n:
        baseline = (n_act * base["trade_win_rate"]
                    + n_skip * base["skip_correct_rate"]) / n
    else:
        baseline = base["skip_correct_rate"]
    lo, hi = wilson_bounds(correct, n)
    rs = act.get("rs") or []
    return {
        "n_graded": n, "n_correct": correct,
        "correct_rate": round(correct / n, 3) if n else None,
        "baseline": round(baseline, 3),
        "wilson90": [round(lo, 3), round(hi, 3)],
        "trades": {"n": n_act, "wins": int(act.get("wins") or 0),
                   "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                   "pending": int(act.get("pending") or 0),
                   "detail": (act.get("trades") or [])[:6]},
        "skips": {"good_skip": int(skip.get("correct") or 0),
                  "regret_miss": int(skip.get("incorrect") or 0),
                  "neutral": int(skip.get("neutral") or 0),
                  "market_wide_excluded": int(skip.get("beta") or 0),
                  "pending": int(skip.get("pending") or 0),
                  "unmatched": int(skip.get("unmatched") or 0),
                  "correlated_dupes": int(skip.get("correlated_dupes") or 0),
                  "examples": (skip.get("examples") or [])[:6]},
        "verdict": classify(correct, n, baseline),
    }


def sample_confidence(n: int) -> str:
    if n < MIN_GRADED:
        return "too_small"
    if n < 20:
        return "low"
    if n < 40:
        return "moderate"
    return "high"


# ---------------------------------------------------------------------------
# 4. lessons under review
# ---------------------------------------------------------------------------
def _kb_entries(kb_json=None) -> list:
    if kb_json is None:
        try:
            from tools import knowledge_base as kbm
            return list(kbm._load().get("entries") or [])
        except Exception:
            return []
    doc = _load_json(kb_json, {})
    return list((doc or {}).get("entries") or [])


def _adopted_doc(adopted_json=None) -> dict:
    if adopted_json is None:
        from tools import learning_adopt as la
        adopted_json = la.ADOPTED_JSON
    doc = _load_json(adopted_json, {})
    return doc if isinstance(doc, dict) else {}


def adopted_noise_kind(text: str) -> str | None:
    low = str(text or "").strip().lower()
    if any(low.startswith(p) for p in _ADOPTED_NOISE_PREFIXES):
        return "pipeline_log"
    if low.startswith(_ADOPTED_KB_DUP_PREFIX):
        return "duplicate_of_kb_lesson"
    return None


def _is_scorecard_retired(e: dict) -> bool:
    return e.get("superseded_by") == SCORECARD_SUPERSEDER


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ---------------------------------------------------------------------------
# 5. the scorecard
# ---------------------------------------------------------------------------
def build_scorecard(today: date | None = None, *, kb_json=None, adopted_json=None,
                    archive_glob=None, journal_dir=None, shadow_json=None,
                    exit_autopsy_dir=None, closed_trades=None,
                    prior: dict | None = None) -> dict:
    """Compute everything; change nothing. Deterministic given inputs."""
    today = today or datetime.now(timezone.utc).date()
    kb_all = [e for e in _kb_entries(kb_json) if isinstance(e, dict) and e.get("id")]
    adopted = [L for L in (_adopted_doc(adopted_json).get("lessons") or [])
               if isinstance(L, dict) and L.get("id")]
    shadows = load_shadows(shadow_json)
    trades = load_closed_trades(exit_autopsy_dir, closed_trades)
    atts = (load_archive_attributions(archive_glob)
            + load_ledger_attributions(journal_dir)
            + shadow_attributions(shadows) + kb_link_attributions(kb_all))
    # One row per (lesson, ticker, decision, run) whatever the source.
    seen, dedup = set(), []
    for a in atts:
        k = (a.get("lesson_id"), a.get("ticker"), a.get("decision"), a.get("run_id"))
        if k in seen:
            continue
        seen.add(k)
        dedup.append(a)
    atts = dedup
    base = baselines(shadows, trades)
    ev = build_evidence(atts, shadows, trades)

    cited_dates = sorted({a["date"] for a in atts if a.get("date")})
    last_any = cited_dates[-1] if cited_dates else None
    capture_healthy = bool(last_any and _d(last_any)
                           and (today - _d(last_any)).days <= CAPTURE_HEALTH_DAYS)
    run_dates = sorted({a["date"] for a in atts if a.get("date")
                        and (today - _d(a["date"])).days <= 14})

    try:
        from tools.learning_adopt import brain_facing_adopted_lessons
        facing_lp = {L.get("id") for L in
                     (brain_facing_adopted_lessons().get("lessons") or [])}
    except Exception:
        facing_lp = set()
    try:
        from tools.knowledge_base import brain_facing_knowledge_base
        facing_kb = {r.get("id") for r in
                     (brain_facing_knowledge_base().get("recent") or [])}
    except Exception:
        facing_kb = set()

    lessons = []

    def _row(lid, store, e, title, discipline, learned, active, extra=None):
        sc = score_lesson(ev.get(lid), base)
        evd = ev.get(lid) or {}
        learned_d = _d(learned)
        age = (today - learned_d).days if learned_d else None
        last_cited = max([x for x in (evd.get("last_cited"), str(e.get("last_cited") or "")[:10]) if x],
                         default=None)
        recent_cite = bool(last_cited and _d(last_cited)
                           and (today - _d(last_cited)).days <= STALE_AFTER_DAYS)
        stale = bool(active and age is not None and age >= STALE_AFTER_DAYS
                     and not recent_cite)
        row = {"id": lid, "store": store, "title": str(title or "")[:140],
               "discipline": discipline, "learned_at": str(learned or "")[:10],
               "age_days": age, "active": active,
               "scorecard_retired": _is_scorecard_retired(e),
               "brain_facing": lid in (facing_kb if store == "kb" else facing_lp),
               "times_cited_total": int(e.get("times_cited") or 0),
               "n_attributed_citations": int(evd.get("n_citations") or 0),
               "decisions": evd.get("decisions") or {},
               "last_cited": last_cited, "stale": stale,
               "evidence_status": e.get("evidence_status"),
               "sample_confidence": sample_confidence(sc["n_graded"]), **sc}
        if extra:
            row.update(extra)
        lessons.append(row)

    for e in kb_all:
        active = not e.get("superseded_by") and not e.get("retired")
        if not active and not _is_scorecard_retired(e):
            continue
        _row(e["id"], "kb", e, e.get("topic"), e.get("discipline"),
             e.get("learned_at"), active)
    for L in adopted:
        active = not L.get("superseded_by")
        if not active and not _is_scorecard_retired(L):
            continue
        _row(L["id"], "adopted", L, L.get("text"), L.get("kind"),
             L.get("adopted_at"), active,
             {"noise": adopted_noise_kind(L.get("text"))})

    plan = plan_actions(lessons, today, capture_healthy, prior)
    verdicts: dict = {}
    for r in lessons:
        if r["active"]:
            verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    return {
        "generated_at": _now(), "as_of": today.isoformat(),
        "params": {"min_graded": MIN_GRADED, "edge_margin": EDGE_MARGIN,
                   "wilson_one_sided": 0.90,
                   "max_retirements_per_week": MAX_RETIREMENTS_PER_WEEK,
                   "min_age_days_to_retire": MIN_AGE_DAYS_TO_RETIRE,
                   "stale_after_days": STALE_AFTER_DAYS,
                   "protected_disciplines": list(PROTECTED_DISCIPLINES)},
        "baseline": base,
        "coverage": {
            "n_lessons_active": sum(1 for r in lessons if r["active"]),
            "n_kb_active": sum(1 for r in lessons if r["active"] and r["store"] == "kb"),
            "n_adopted_active": sum(1 for r in lessons if r["active"] and r["store"] == "adopted"),
            "n_attributions": len(atts),
            "n_graded_decisions": sum(r["n_graded"] for r in lessons),
            "n_closed_trades": len(trades),
            "n_shadows": len(shadows),
            "last_citation_date": last_any,
            "citation_days_last_14": len(run_dates),
            "citation_capture_healthy": capture_healthy,
        },
        "verdict_counts": verdicts,
        "lessons": sorted(lessons, key=lambda r: (not r["active"], r["store"], r["id"])),
        "actions": plan,
    }


def plan_actions(lessons: list, today: date, capture_healthy: bool,
                 prior: dict | None = None) -> dict:
    """Decide (not apply) retirements, reinstatements, boosts and
    deprioritizations under the caps. Pure."""
    week = _iso_week(today)
    used = 0
    for h in ((prior or {}).get("history") or []):
        if isinstance(h, dict) and h.get("week") == week and h.get("applied"):
            used += len(h.get("retired") or []) + len(h.get("reinstated") or [])
    budget = max(0, MAX_RETIREMENTS_PER_WEEK - used)
    out = {"week": week, "budget_before": budget, "retire": [], "reinstate": [],
           "boost": [], "deprioritize": [], "flag_for_review": [],
           "deferred_by_cap": [], "stale_skipped_reason": None}

    hurting = sorted([r for r in lessons if r["active"] and r["verdict"] == "hurting"],
                     key=lambda r: (r["wilson90"][1] - r["baseline"], -r["n_graded"]))
    for r in hurting:
        why = (f"{r['n_correct']}/{r['n_graded']} decisions right "
               f"({round((r['correct_rate'] or 0) * 100)}%) vs {round(r['baseline'] * 100)}% "
               f"baseline; 90% upper bound {round(r['wilson90'][1] * 100)}%")
        if r["store"] == "kb" and r.get("evidence_status") == "validated":
            out["flag_for_review"].append({"id": r["id"], "why": "validated_by_trades; " + why})
        elif r.get("discipline") in PROTECTED_DISCIPLINES:
            out["flag_for_review"].append({"id": r["id"], "why": "protected_risk_lesson; " + why})
        elif r["age_days"] is None or r["age_days"] < MIN_AGE_DAYS_TO_RETIRE:
            out["flag_for_review"].append({"id": r["id"], "why": "too_fresh; " + why})
        elif budget <= 0:
            out["deferred_by_cap"].append({"id": r["id"], "why": why})
        else:
            out["retire"].append({"id": r["id"], "store": r["store"], "why": why})
            budget -= 1

    for r in lessons:
        if r["scorecard_retired"] and not r["active"] and r["verdict"] == "helping":
            if budget > 0:
                out["reinstate"].append({"id": r["id"], "store": r["store"],
                                         "why": "later evidence: helping"})
                budget -= 1
            else:
                out["deferred_by_cap"].append({"id": r["id"], "why": "reinstate"})

    retiring = {x["id"] for x in out["retire"]}
    for r in lessons:
        if not r["active"] or r["id"] in retiring:
            continue
        if r["verdict"] == "helping":
            out["boost"].append({"id": r["id"], "store": r["store"],
                                 "why": (f"{r['n_correct']}/{r['n_graded']} right vs "
                                         f"{round(r['baseline'] * 100)}% baseline")})
        elif r.get("noise"):
            out["deprioritize"].append({"id": r["id"], "store": r["store"],
                                        "why": r["noise"]})
        elif r["stale"] and capture_healthy:
            out["deprioritize"].append({"id": r["id"], "store": r["store"],
                                        "why": f"no citation in {STALE_AFTER_DAYS}d, "
                                               f"age {r['age_days']}d"})
    if not capture_healthy:
        out["stale_skipped_reason"] = (
            f"citation capture unhealthy (no lesson citation recorded in the last "
            f"{CAPTURE_HEALTH_DAYS} days) — staleness is not judged on an outage")
    out["budget_after"] = budget
    return out


# ---------------------------------------------------------------------------
# 6. apply (writes lesson metadata only)
# ---------------------------------------------------------------------------
def _priority_map(plan: dict) -> dict:
    m = {}
    for x in plan.get("deprioritize") or []:
        m[x["id"]] = "deprioritize"
    for x in plan.get("boost") or []:
        m[x["id"]] = "boost"
    return m


def apply_actions(card: dict, *, kb_json=None, adopted_json=None) -> dict:
    """Stamp every reviewed lesson with its scorecard line and priority, and
    apply the capped retire/reinstate plan. Returns what changed. Not
    fail-soft on write errors (the CLI reports them)."""
    from tools import knowledge_base as kbm
    from tools import learning_adopt as la
    plan = card["actions"]
    prio = _priority_map(plan)
    retire = {x["id"]: x for x in plan.get("retire") or []}
    reinstate = {x["id"] for x in plan.get("reinstate") or []}
    rows = {r["id"]: r for r in card["lessons"]}
    stamp_at = card["as_of"]
    changed = {"retired": [], "reinstated": [], "boosted": [], "deprioritized": [],
               "stamped": 0}

    def _stamp(e):
        """Returns True only when the entry actually changed, so a quiet week
        rewrites no store."""
        r = rows.get(e.get("id"))
        if not r:
            return False
        before = json.dumps(e, sort_keys=True, default=str)
        # Stamp only what carries information: a real verdict, a non-normal
        # priority. 80+ "insufficient_data" stamps rewritten every week would
        # be churn in the stores and the public journal, not evidence.
        if r["verdict"] in ("helping", "hurting", "no_clear_edge"):
            e["scorecard"] = {"verdict": r["verdict"], "n_graded": r["n_graded"],
                              "correct_rate": r["correct_rate"],
                              "baseline": r["baseline"], "as_of": stamp_at}
        else:
            e.pop("scorecard", None)
        new_p = prio.get(e["id"], "normal")
        if e.get("scorecard_priority", "normal") != new_p:
            if new_p == "boost":
                changed["boosted"].append(e["id"])
            elif new_p == "deprioritize":
                changed["deprioritized"].append(e["id"])
        if new_p == "normal":
            e.pop("scorecard_priority", None)
        else:
            e["scorecard_priority"] = new_p
        if e["id"] in retire and not e.get("superseded_by"):
            e["superseded_by"] = SCORECARD_SUPERSEDER
            e["superseded_at"] = _now()
            e["supersede_reason"] = "scorecard_hurting"
            e["supersede_why"] = retire[e["id"]]["why"][:300]
            changed["retired"].append(e["id"])
        elif e["id"] in reinstate and _is_scorecard_retired(e):
            for k in ("superseded_by", "superseded_at", "supersede_reason", "supersede_why"):
                e.pop(k, None)
            e["reinstated_at"] = _now()
            changed["reinstated"].append(e["id"])
        after = json.dumps({k: v for k, v in e.items() if k != "scorecard"},
                           sort_keys=True, default=str)
        before_cmp = json.dumps({k: v for k, v in json.loads(before).items()
                                 if k != "scorecard"}, sort_keys=True, default=str)
        sc_old = (json.loads(before).get("scorecard") or {})
        sc_new = e.get("scorecard") or {}
        strip = lambda d: {k: v for k, v in d.items() if k != "as_of"}  # noqa: E731
        did = after != before_cmp or strip(sc_old) != strip(sc_new)
        if not did and sc_old:
            e["scorecard"] = sc_old  # unchanged verdict: keep its original date
        changed["stamped"] += int(did)
        return did

    # Knowledge base — through its own loader/saver so the published journal
    # stays in sync.
    kb_path = Path(kb_json) if kb_json else kbm.KB_JSON
    if kb_json:
        doc = _load_json(kb_path, {"entries": []})
    else:
        doc = kbm._load()
    touched = False
    for e in doc.get("entries") or []:
        if isinstance(e, dict) and e.get("id") in rows:
            touched = _stamp(e) or touched
    if touched:
        if kb_json:
            _atomic_write(kb_path, doc)
        else:
            kbm._save(doc)
            kbm.publish_learning_journal(doc)

    ad_path = Path(adopted_json) if adopted_json else la.ADOPTED_JSON
    adoc = _load_json(ad_path, {})
    if isinstance(adoc, dict) and adoc.get("lessons"):
        t2 = False
        for L in adoc["lessons"]:
            if isinstance(L, dict) and L.get("id") in rows:
                t2 = _stamp(L) or t2
        if t2:
            adoc["updated_at"] = _now()
            _atomic_write(ad_path, adoc)
    return changed


def _atomic_write(path: Path, doc) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(doc, indent=2, default=str))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# 7. brain-facing helpers (used by knowledge_base / learning_adopt packs)
# ---------------------------------------------------------------------------
def priority_tier(e: dict) -> int:
    """0 boost, 1 normal, 2 deprioritize. Pure."""
    return {"boost": 0, "deprioritize": 2}.get(str(e.get("scorecard_priority") or ""), 1)


def retired_markers(entries: list, days: int = 30, today: date | None = None,
                    title_key: str = "topic") -> list:
    """[SUPERSEDED ...] lines for lessons the scorecard retired recently, so
    the brain knows a lesson it may remember no longer binds. Pure."""
    today = today or datetime.now(timezone.utc).date()
    out = []
    for e in entries or []:
        if not isinstance(e, dict) or not _is_scorecard_retired(e):
            continue
        at = _d(e.get("superseded_at"))
        if at is None or (today - at).days > days:
            continue
        out.append({"id": e.get("id"),
                    "marker": (f"[SUPERSEDED {at.isoformat()} by lesson scorecard - no "
                               f"longer binding: {str(e.get(title_key) or '')[:90]}] "
                               f"{str(e.get('supersede_why') or '')[:160]}")})
    return out[-8:]


def citation_health(journal_dir=None, today: date | None = None, days: int = 5) -> dict:
    """How many recent act-on runs cited ANY lesson. Fed to the brain so a
    silent citation outage (2026-09-23..10-05: 0 citations in ~60 runs) is
    visible at decision time. Never raises."""
    try:
        today = today or datetime.now(timezone.utc).date()
        jdir = Path(journal_dir) if journal_dir else _journal_dir()
        runs = cited = structured = 0
        for f in sorted((jdir / CITATION_LEDGER_SUBDIR).glob("*.jsonl")):
            day = _d(f.stem)
            if day is None or (today - day).days > days:
                continue
            for line in f.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                runs += 1
                cited += int((rec.get("n_citations") or 0) > 0)
                structured += int((rec.get("n_structured") or 0) > 0)
        out = {"window_days": days, "runs": runs, "runs_citing": cited,
               "runs_with_lessons_applied": structured}
        if runs and cited / runs < 0.5:
            out["nudge"] = (f"Only {cited}/{runs} recent runs cited a lesson. When a "
                            "KB-/LP- lesson shapes a buy, skip or wait, put its id in "
                            "that item AND in lessons_applied — uncited lessons can "
                            "never be graded, boosted or retired.")
        return out
    except Exception as e:
        return {"status": "error", "reason": str(e)[:120]}


# ---------------------------------------------------------------------------
# 8. outputs + CLI
# ---------------------------------------------------------------------------
def _title(r: dict, n: int = 70) -> str:
    t = re.sub(r"\s+", " ", str(r.get("title") or "")).strip()
    return t[:n] + ("…" if len(t) > n else "")


def summary_lines(card: dict, changed: dict | None, applied: bool) -> list:
    cov, base, plan = card["coverage"], card["baseline"], card["actions"]
    vc = card.get("verdict_counts") or {}
    lines = [
        f"Lesson scorecard {card['as_of']} ({'APPLIED' if applied else 'dry run'}): "
        f"{cov['n_lessons_active']} active lessons ({cov['n_kb_active']} KB, "
        f"{cov['n_adopted_active']} adopted), {cov['n_attributions']} attributed "
        f"citations, {cov['n_graded_decisions']} graded lesson-decisions.",
        f"Verdicts: {vc.get('helping', 0)} helping, {vc.get('hurting', 0)} hurting, "
        f"{vc.get('no_clear_edge', 0)} no clear edge, "
        f"{vc.get('insufficient_data', 0)} not enough data (need {MIN_GRADED}+ graded).",
        f"Baselines: skips were right {round(base['skip_correct_rate'] * 100)}% of "
        f"{base['n_skip_graded']} graded shadows; trade win baseline "
        f"{round(base['trade_win_rate'] * 100)}% ({base['n_trades']} closed trades).",
        f"Citation capture: last citation {cov['last_citation_date'] or 'never'}, "
        f"{'healthy' if cov['citation_capture_healthy'] else 'UNHEALTHY'}.",
    ]
    ch = changed or {}
    if applied:
        lines.append(f"Changes: retired {ch.get('retired') or 'none'}, reinstated "
                     f"{ch.get('reinstated') or 'none'}, newly boosted "
                     f"{ch.get('boosted') or 'none'}, newly deprioritized "
                     f"{len(ch.get('deprioritized') or [])}.")
    else:
        lines.append(f"Would retire {[x['id'] for x in plan['retire']] or 'none'}, "
                     f"boost {[x['id'] for x in plan['boost']] or 'none'}, "
                     f"deprioritize {len(plan['deprioritize'])}.")
    for x in plan["retire"]:
        lines.append(f"  retire {x['id']}: {x['why']}")
    for x in plan["flag_for_review"]:
        lines.append(f"  review {x['id']}: {x['why']}")
    if plan.get("stale_skipped_reason"):
        lines.append(f"  staleness: {plan['stale_skipped_reason']}")
    # Rank by evidence WEIGHT (gap x sqrt(n)), and only lessons with 3+ graded
    # decisions: a 1/1 is a coin flip, not the "best lesson".
    ranked = [r for r in card["lessons"] if r["active"] and r["n_graded"] >= 3]
    top = sorted(ranked, key=lambda r: (-(r["correct_rate"] - r["baseline"])
                                        * math.sqrt(r["n_graded"]), r["id"]))
    best = [r for r in top if r["correct_rate"] > r["baseline"]][:3]
    worst = [r for r in reversed(top) if r["correct_rate"] < r["baseline"]][:3]
    if best:
        lines.append("Best evidence so far:")
        for r in best:
            lines.append(f"  {r['id']} {r['n_correct']}/{r['n_graded']} right "
                         f"(base {round(r['baseline'] * 100)}%) [{r['verdict']}] {_title(r)}")
    if worst:
        lines.append("Worst evidence so far:")
        for r in worst:
            lines.append(f"  {r['id']} {r['n_correct']}/{r['n_graded']} right "
                         f"(base {round(r['baseline'] * 100)}%) [{r['verdict']}] {_title(r)}")
    return lines


def render_markdown(card: dict, lines: list) -> str:
    md = ["# Lesson scorecard", "", f"_Generated {card['generated_at']}_", ""]
    md += [f"- {ln.strip()}" if not ln.startswith("  ") else f"  - {ln.strip()}"
           for ln in lines]
    md += ["", "| id | store | verdict | right/graded | base | trades (avg R) | "
           "skips good/regret | cites | title |", "|---|---|---|---|---|---|---|---|---|"]
    for r in card["lessons"]:
        if not r["active"]:
            continue
        tr = r["trades"]
        md.append(
            f"| {r['id']} | {r['store']} | {r['verdict']} | {r['n_correct']}/{r['n_graded']} "
            f"| {round(r['baseline'] * 100)}% | {tr['n']} ({tr['avg_r']}) | "
            f"{r['skips']['good_skip']}/{r['skips']['regret_miss']} | "
            f"{r['n_attributed_citations']} | {_title(r, 60).replace('|', '/')} |")
    return "\n".join(md) + "\n"


def run(apply: bool = False, today: date | None = None, write_outputs: bool = True,
        run_id: str | None = None) -> dict:
    """Build the scorecard; in apply mode act on it. Writes
    data/lesson_scorecard.json + .md and a journal line when write_outputs."""
    prior = _load_json(SCORECARD_JSON, {}) if SCORECARD_JSON.exists() else {}
    card = build_scorecard(today, prior=prior)
    changed = apply_actions(card) if apply else None
    lines = summary_lines(card, changed, apply)
    card["mode"] = "applied" if apply else "dry_run"
    card["changed"] = changed
    card["summary"] = lines
    hist = list((prior or {}).get("history") or [])
    if apply:
        hist.append({"date": card["as_of"], "week": card["actions"]["week"],
                     "applied": True,
                     "retired": (changed or {}).get("retired") or [],
                     "reinstated": (changed or {}).get("reinstated") or [],
                     "boosted": [x["id"] for x in card["actions"]["boost"]],
                     "n_deprioritized": len(card["actions"]["deprioritize"]),
                     "verdict_counts": card.get("verdict_counts")})
    card["history"] = hist[-HISTORY_KEEP:]
    if write_outputs:
        if apply:
            _atomic_write(SCORECARD_JSON, card)
            SCORECARD_MD.write_text(render_markdown(card, lines))
        else:
            # A dry run never writes the applied card (its history is the
            # weekly cap ledger); it writes a gitignored sibling preview.
            prev = SCORECARD_JSON.with_name("lesson_scorecard_preview.json")
            _atomic_write(prev, card)
        if apply:  # dry runs leave no journal trace
            try:
                # Its own journal stream, NOT journal.log_improvement: improvement
                # notes are harvested by the weekly adopt pipeline, which would
                # auto-adopt this status line as a "lesson".
                import journal
                journal._write("lesson_scorecard", {
                    "run_id": run_id, "mode": card["mode"], "summary": lines[:5],
                    "changed": changed, "verdict_counts": card.get("verdict_counts"),
                    "coverage": card["coverage"]})
            except Exception:
                pass
    return card


def main(argv: list | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Lesson scorecard (learning loop)")
    ap.add_argument("--apply", action="store_true",
                    help="act on the scorecard (capped retirements/boosts)")
    ap.add_argument("--no-write", action="store_true",
                    help="print only; write no files")
    ap.add_argument("--json", action="store_true", help="print the full card")
    a = ap.parse_args(argv)
    card = run(apply=a.apply, write_outputs=not a.no_write)
    if a.json:
        print(json.dumps(card, indent=2, default=str))
    print("\n".join(card["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
