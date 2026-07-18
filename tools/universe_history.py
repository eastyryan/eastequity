"""Point-in-time universe membership — what was tradeable on a given date.

WHY THIS EXISTS

data/universe.json is a LIVE POINTER with no history: git shows 5 commits spanning
~9 days, and the weekly review rewrites it wholesale, computing removals as a set
difference from a model-supplied replacement map. Names that leave vanish without
trace.

They are not leaving at random. Measured across the existing history, the removals
were INTC, PYPL, MDB, ON, NXPI, MCHP, IREN, APLD, COIN, MU, NBIS — the laggard
cohort of the AI trade — while additions came from names the radar surfaced *because
they had already moved*. The curation input on both sides is recent relative
performance. That is a momentum strategy applied BEFORE the momentum score is
computed, which makes it invisible to every downstream measurement, and it means
every backward-looking study runs on a pool whose losers have been deleted.

scripts/sepa_study.py already names this and mitigates it as far as it can with
same-day cross-sectional differencing — but differencing cancels the LEVEL, not the
fact that the surviving pool's dispersion, volatility and factor loading are all
themselves conditioned on having won.

DESIGN

Append-only JSONL, one record per membership change. Deliberately NOT a schema
migration of universe.json: ~10 modules read `sectors` as the active universe
(validator's hard gate, the scanner, demand_drivers, sepa_study...), and changing
that shape to carry per-ticker metadata would touch all of them for no gain. The
active view stays exactly as it is; history accumulates beside it.

Value is entirely FORWARD-looking — the past is already unrecoverable — which is
precisely why it must start now. Every week without it is a week of measurement
capability permanently lost.

CLI: python -m tools.universe_history [--as-of YYYY-MM-DD]
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "universe_history.jsonl"
UNIVERSE_FILE = ROOT / "data" / "universe.json"

ADDED, REMOVED = "added", "removed"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _append(records: list[dict]) -> int:
    """Append-only, never rewritten — the whole point is that nothing is lost."""
    if not records:
        return 0
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return len(records)


def load_history() -> list[dict]:
    """All membership events, oldest first. Fail-soft; a bad line is skipped, not fatal."""
    if not HISTORY_FILE.is_file():
        return []
    out = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("ticker") and rec.get("date"):
            out.append(rec)
    out.sort(key=lambda r: (r["date"], r.get("ticker", "")))
    return out


def record_changes(added=(), removed=(), *, sector_map=None, run_id=None,
                   reason=None, date=None, source="weekly_review") -> int:
    """Record membership changes. Returns the number of events written.

    Called from BOTH universe writers in runlib/reviews.py — the weekly review and
    the mid-run dynamic-add path — so no change can bypass the log.
    """
    day = date or _today()
    sector_map = sector_map or {}
    records = []
    # strip() before upper(): a stray-whitespace ticker would never match on replay,
    # so the name would silently drop out of every point-in-time reconstruction.
    for t in sorted({str(x).strip().upper() for x in (added or []) if str(x).strip()}):
        records.append({"ticker": t, "action": ADDED, "date": day,
                        "sector": sector_map.get(t), "run_id": run_id,
                        "reason": (str(reason)[:300] if reason else None),
                        "source": source})
    for t in sorted({str(x).strip().upper() for x in (removed or []) if str(x).strip()}):
        records.append({"ticker": t, "action": REMOVED, "date": day,
                        "sector": sector_map.get(t), "run_id": run_id,
                        "reason": (str(reason)[:300] if reason else None),
                        "source": source})
    return _append(records)


def universe_as_of(date: str, history=None) -> set:
    """The set of tickers tradeable on `date` — the point-in-time universe.

    Replays events with date <= `date`. A name added then removed then re-added
    resolves correctly because events are applied in order. Pure given `history`.
    """
    active = set()
    for rec in (history if history is not None else load_history()):
        if rec["date"] > date:
            break
        if rec.get("action") == ADDED:
            active.add(rec["ticker"])
        elif rec.get("action") == REMOVED:
            active.discard(rec["ticker"])
    return active


def membership_spans(history=None) -> dict:
    """{TICKER: [{"added", "removed"}]} — when each name was in the universe.

    A backtest can use this to include a name only over the window it was actually
    tradeable, instead of assuming today's survivors were always members.
    """
    spans: dict = {}
    for rec in (history if history is not None else load_history()):
        t = rec["ticker"]
        cur = spans.setdefault(t, [])
        if rec.get("action") == ADDED:
            if not cur or cur[-1].get("removed") is not None:
                cur.append({"added": rec["date"], "removed": None})
        elif rec.get("action") == REMOVED and cur and cur[-1].get("removed") is None:
            cur[-1]["removed"] = rec["date"]
    return spans


def current_sectors() -> dict:
    try:
        return json.loads(UNIVERSE_FILE.read_text()).get("sectors") or {}
    except Exception:
        return {}


def seed_from_current(date: str | None = None, run_id=None) -> int:
    """One-time bootstrap: record today's universe as the opening membership.

    Idempotent — a second call adds nothing. Honest about what it is: these names
    were NOT all added on this date, and the record says so via source="seed", so
    nobody later mistakes the seed date for a real entry date.
    """
    if load_history():
        return 0
    sectors = current_sectors()
    sector_map = {str(t).upper(): s for s, ts in sectors.items() for t in ts}
    return record_changes(added=sector_map.keys(), sector_map=sector_map,
                          run_id=run_id, date=date or _today(), source="seed",
                          reason="initial membership snapshot; true entry dates for "
                                 "these names predate the history log and are "
                                 "unrecoverable")


def drift_report() -> dict:
    """Does the replayed history match the live universe file?

    A mismatch means a write bypassed record_changes and point-in-time
    reconstruction has silently gone wrong — the failure this whole module exists
    to prevent, so it must be visible rather than inferred.
    """
    live = {str(t).upper() for ts in current_sectors().values() for t in ts}
    replayed = universe_as_of(_today())
    missing = sorted(live - replayed)      # in the file, never logged
    extra = sorted(replayed - live)        # logged as active, gone from the file
    return {"ok": not missing and not extra,
            "n_live": len(live), "n_replayed": len(replayed),
            "unlogged_additions": missing, "unlogged_removals": extra,
            "detail": None if not (missing or extra) else
            "universe.json and the history log disagree — a write bypassed "
            "record_changes, so point-in-time reconstruction is unreliable"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args()
    if args.seed:
        print(json.dumps({"seeded": seed_from_current()}, indent=1))
    elif args.as_of:
        names = sorted(universe_as_of(args.as_of))
        print(json.dumps({"as_of": args.as_of, "n": len(names),
                          "tickers": names}, indent=1))
    else:
        print(json.dumps(drift_report(), indent=1))
