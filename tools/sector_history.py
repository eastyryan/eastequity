"""Durable sector-momentum history — so a rotation can be seen, not just noticed.

WHY (2026-07-19 weekly run)
---------------------------
The run's sharpest finding was that every AI-buildout sector was strongly
positive on 3-month and sharply negative on 1-month while SPY was flat — the
signature of a factor unwind. `sector_relative_strength` is recomputed from
scratch every run and then discarded. So that observation existed only inside one
brain response's prose, and nothing in the system could later answer "was the AI
stack unwinding on 2026-07-19?", let alone "when did it start?" or "how long do
these last?".

A snapshot tells you where sectors are. A history tells you what is HAPPENING to
them, which is the actual question — and it is the input a future study session
would need to measure whether sector rotation is tradeable here at all.

Design notes:
  * Append-only JSONL, one row per (run, sector). Cheap, greppable, diffable, and
    survives a partial write without corrupting earlier rows.
  * ONE row per sector per DAY. Seven runs a day would otherwise weight a single
    Tuesday seven times against a single Sunday in any later study.
  * Deltas are computed on READ, never stored: a stored delta silently rots the
    moment history is backfilled, deduped or corrected.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = ROOT / "data" / "sector_momentum_history.jsonl"

# Keep the file small enough to load and grep for years. ~15 sectors x 250
# trading days is ~3,750 rows/year.
MAX_ROWS = 40_000


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def record_sector_momentum(sector_rs: list[dict], *, run_id: str = "",
                           as_of: str | None = None,
                           benchmark_1m_pct: float | None = None) -> dict:
    """Append today's sector momentum snapshot. Idempotent per (day, sector).

    Never raises: a history write must not be able to take down a trading run.
    """
    out = {"written": 0, "skipped_existing": 0, "status": "ok"}
    try:
        rows = [r for r in (sector_rs or []) if isinstance(r, dict) and r.get("sector")]
        if not rows:
            out["status"] = "empty_input"
            return out
        day = as_of or _today_utc()

        existing = load_history()
        have = {(r.get("date"), r.get("sector")) for r in existing}

        new: list[dict] = []
        for r in rows:
            key = (day, r["sector"])
            if key in have:
                out["skipped_existing"] += 1
                continue
            rec = {
                "date": day,
                "sector": r["sector"],
                "avg_momentum_1m_pct": r.get("avg_momentum_1m_pct"),
                "avg_momentum_3m_pct": r.get("avg_momentum_3m_pct"),
                "names": r.get("names"),
                "run_id": run_id,
            }
            # Stored so a later study can separate sector moves from the tape's.
            if benchmark_1m_pct is not None:
                rec["benchmark_1m_pct"] = benchmark_1m_pct
            new.append(rec)
            have.add(key)

        if not new:
            return out

        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a") as f:
            for rec in new:
                f.write(json.dumps(rec) + "\n")
        out["written"] = len(new)

        if len(existing) + len(new) > MAX_ROWS:
            keep = (existing + new)[-MAX_ROWS:]
            HISTORY_PATH.write_text(
                "".join(json.dumps(r) + "\n" for r in keep))
            out["trimmed_to"] = MAX_ROWS
    except Exception as e:  # pragma: no cover - telemetry must never break a run
        out["status"] = f"error:{type(e).__name__}:{str(e)[:100]}"
    return out


def load_history() -> list[dict]:
    """All rows, oldest first. Corrupt lines are skipped, never fatal."""
    if not HISTORY_PATH.exists():
        return []
    rows = []
    try:
        for line in HISTORY_PATH.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("sector"):
                rows.append(r)
    except Exception:
        return rows
    return rows


def sector_trends(lookback_days: int = 30, *, min_observations: int = 2) -> dict:
    """What has been HAPPENING to each sector, computed on read.

    Returns per-sector first/last 1m momentum over the window and the change
    between them, plus the sectors moving most in each direction. A sector seen
    fewer than `min_observations` times is reported as insufficient rather than
    given a delta from a single point — one observation is a level, not a trend.
    """
    hist = load_history()
    if not hist:
        return {"status": "no_history", "note": (
            "sector momentum history is empty — it accrues one snapshot per "
            "trading day from the universe scan")}

    days = sorted({r.get("date") for r in hist if r.get("date")})
    window = set(days[-lookback_days:]) if lookback_days else set(days)

    by_sector: dict[str, list[dict]] = {}
    for r in hist:
        if r.get("date") in window:
            by_sector.setdefault(r["sector"], []).append(r)

    trends, thin = {}, []
    for sector, rows in by_sector.items():
        rows.sort(key=lambda r: r.get("date") or "")
        if len(rows) < min_observations:
            thin.append(sector)
            continue
        first, last = rows[0], rows[-1]
        f1, l1 = first.get("avg_momentum_1m_pct"), last.get("avg_momentum_1m_pct")
        entry = {
            "observations": len(rows),
            "from_date": first.get("date"),
            "to_date": last.get("date"),
            "momentum_1m_pct_first": f1,
            "momentum_1m_pct_last": l1,
            "momentum_3m_pct_last": last.get("avg_momentum_3m_pct"),
            "names": last.get("names"),
        }
        if isinstance(f1, (int, float)) and isinstance(l1, (int, float)):
            entry["momentum_1m_change_pp"] = round(l1 - f1, 1)
        trends[sector] = entry

    moving = [(s, e["momentum_1m_change_pp"]) for s, e in trends.items()
              if e.get("momentum_1m_change_pp") is not None]
    moving.sort(key=lambda p: p[1], reverse=True)

    return {
        "status": "ok",
        "lookback_days": lookback_days,
        "days_observed": len(window),
        "sectors": trends,
        "insufficient_history": sorted(thin) or None,
        "accelerating": [{"sector": s, "change_pp": c} for s, c in moving[:5]] or None,
        "decelerating": [{"sector": s, "change_pp": c}
                         for s, c in moving[-5:][::-1]] or None,
        "note": ("Change is in PERCENTAGE POINTS of 1-month average momentum "
                 "between the first and last observation in the window. Deltas "
                 "are computed on read, never stored, so backfills and "
                 "corrections cannot leave a stale number behind."),
    }
