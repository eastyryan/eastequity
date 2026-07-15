"""Earnings-calendar deep-dive trigger.

When a company in the universe reports earnings, the scheduled system forces a
FULL deep dive on the next relevant slot and promotes the reporter(s) into the
deep-research focus set:

  * 9:00am ET slot  -> catches OVERNIGHT + pre-market (BMO) reporters
  * 5:30pm ET slot  -> catches that afternoon's after-hours (AMC) reporters
    (5:30 rather than 4:00 because after-hours prints are often not out by 4).

Why memoryless
--------------
The cloud gather node runs on ephemeral GitHub runners with a fresh checkout and
commits only data/, so there is no durable "already handled" state to lean on.
Reporter selection is therefore MEMORYLESS: it uses yfinance's most-recent
("last") earnings timestamp plus a per-session time window keyed off the current
ET clock. yfinance splits earnings_dates into past/future, so a name reporting
today after the close is still "future" at the 9am gather and only becomes
"last" by the 5:30pm gather - the split does most of the work.

Everything network-touching is wrapped and degrades to the cached (or empty)
calendar so a blocked feed never crashes a run. The selection helpers are pure
and offline (unit-tested without yfinance).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# Keep module import light (pure helpers stay offline-testable): resolve ROOT
# locally and pull et_now lazily inside the network paths, so importing this
# module never requires dotenv/pandas.
ROOT = Path(__file__).resolve().parent.parent
_CACHE = ROOT / "data" / "earnings_calendar.json"
_CACHE_MAX_AGE_MIN = 90  # at most ~one refresh per slot


def _et_now() -> datetime:
    from runlib.core import et_now
    return et_now()


# --------------------------------------------------------------------------- #
# Pure helpers (no network, unit-tested offline)
# --------------------------------------------------------------------------- #
def classify_when(dt: datetime | None) -> str:
    """bmo / amc / unknown from an ET-localized earnings timestamp.

    Midnight (00:00:00) is treated as date-only -> unknown: yfinance omits the
    session time for some names. Pure.
    """
    if dt is None:
        return "unknown"
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return "unknown"
    if dt.hour < 12:
        return "bmo"
    if dt.hour >= 16:
        return "amc"
    return "unknown"


def _parse_et(iso: str | None) -> datetime | None:
    """Parse an ISO timestamp and localize/convert to America/New_York. Pure."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        return dt.astimezone(et) if dt.tzinfo is not None else dt.replace(tzinfo=et)
    except Exception:
        return dt


def session_window_start(session: str, now_et: datetime) -> datetime | None:
    """Lower bound for the 'morning' window: the prior trading day's market close
    (16:00 ET). This catches overnight + pre-market prints AND acts as a safety
    net for an after-hours name that yfinance was slow to mark as reported by the
    5:40pm evening gather (worst case: a redundant second deep dive the next
    morning, which we accept over a miss). 'evening' uses a same-day date test
    instead of a start window. Pure.
    """
    if session == "morning":
        return (now_et - timedelta(days=1)).replace(
            hour=16, minute=0, second=0, microsecond=0)
    return None


def reporters_for_session(calendar: dict, session: str, now_et: datetime,
                          *, future_tol_min: int = 90) -> list[str]:
    """Universe tickers whose most-recent earnings fall in the session window.

    morning -> overnight + pre-market (BMO) reporters since last evening.
    evening -> this afternoon's after-hours (AMC) reporters (reported today,
               not a morning print).
    Pure. `calendar` is the by_ticker map: {TICKER: {"last": iso, "last_when": ..}}.
    """
    if not isinstance(calendar, dict) or session not in ("morning", "evening"):
        return []
    tol = timedelta(minutes=future_tol_min)
    start = session_window_start(session, now_et)
    picked: list[tuple[str, datetime]] = []
    for tk, info in calendar.items():
        if not isinstance(info, dict):
            continue
        last = _parse_et(info.get("last"))
        if last is None or last > now_et + tol:
            continue
        when = info.get("last_when") or classify_when(last)
        if session == "morning":
            if start is not None and last >= start:
                picked.append((str(tk).upper(), last))
        else:  # evening: reported today, exclude clear morning prints
            if last.date() == now_et.date() and when != "bmo":
                picked.append((str(tk).upper(), last))
    picked.sort(key=lambda x: x[1], reverse=True)
    # De-dup while preserving order (newest first).
    seen: set[str] = set()
    out: list[str] = []
    for tk, _ in picked:
        if tk not in seen:
            seen.add(tk)
            out.append(tk)
    return out


# --------------------------------------------------------------------------- #
# Network + cache
# --------------------------------------------------------------------------- #
def _load_cache() -> dict:
    try:
        blob = json.loads(_CACHE.read_text())
        return blob if isinstance(blob, dict) else {}
    except Exception:
        return {}


def _cache_fresh(blob: dict, now_et: datetime) -> bool:
    built = _parse_et((blob or {}).get("built_at"))
    return built is not None and (now_et - built) <= timedelta(minutes=_CACHE_MAX_AGE_MIN)


def build_earnings_calendar(tickers: Iterable[str], *, now_et: datetime | None = None,
                            force: bool = False) -> dict:
    """Per-ticker {last, last_when, next, next_when} for the universe.

    yfinance-backed, wrapped. On any failure returns the cached calendar (or an
    empty stub). A cache younger than ~one slot is reused to avoid re-hitting the
    network on every tick. Never raises.
    """
    now_et = now_et or _et_now()
    cached = _load_cache()
    if not force and _cache_fresh(cached, now_et) and (cached.get("by_ticker")):
        return cached
    try:
        import yfinance as yf
        import pandas as pd
    except Exception:
        stub = cached or {}
        stub.setdefault("by_ticker", {})
        stub["status"] = "yfinance_unavailable"
        return stub

    by_ticker: dict[str, dict] = dict(cached.get("by_ticker") or {})
    now_ts = pd.Timestamp.now(tz="America/New_York")
    ok = 0
    for t in sorted({str(x).upper() for x in tickers if x}):
        try:
            ed = yf.Ticker(t).earnings_dates
            if ed is None or not len(ed.index):
                continue
            past = sorted([d for d in ed.index if d <= now_ts])
            future = sorted([d for d in ed.index if d > now_ts])
            rec: dict[str, Any] = {}
            if past:
                last = past[-1]
                rec["last"] = last.isoformat()
                rec["last_when"] = classify_when(last.to_pydatetime())
            if future:
                nxt = future[0]
                rec["next"] = nxt.isoformat()
                rec["next_when"] = classify_when(nxt.to_pydatetime())
            if rec:
                by_ticker[t] = rec
                ok += 1
        except Exception:
            continue
    out = {"built_at": now_et.isoformat(), "by_ticker": by_ticker,
           "status": "ok" if ok else "empty", "n": ok}
    try:
        _CACHE.write_text(json.dumps(out, indent=2))
    except Exception:
        pass
    return out


def earnings_reporters_for_slot(session: str, *, now_et: datetime | None = None,
                                cfg: dict | None = None,
                                universe: Iterable[str] | None = None) -> dict:
    """High-level entry: build/refresh the calendar and return the fresh reporters
    for `session` ('morning'|'evening'). Never raises.

    Returns {"session", "reporters", "as_of", "calendar_status",
             "note", "when": {ticker: bmo|amc|unknown}}.
    `reporters` (possibly empty) are universe tickers that just reported and
    warrant a forced full deep dive.
    """
    now_et = now_et or _et_now()
    try:
        if universe is None:
            import validator
            universe = validator.load_universe()
        uni = sorted({str(x).upper() for x in (universe or [])})
        uni_set = set(uni)
        cal = build_earnings_calendar(uni, now_et=now_et)
        by_ticker = cal.get("by_ticker") or {}
        reps = [t for t in reporters_for_session(by_ticker, session, now_et)
                if t in uni_set]
        when = {t: (by_ticker.get(t) or {}).get("last_when", "unknown") for t in reps}
        return {
            "session": session,
            "reporters": reps,
            "when": when,
            "as_of": now_et.isoformat(),
            "calendar_status": cal.get("status"),
            "note": (
                "Universe names that reported earnings and triggered a FORCED full "
                "deep dive this run (9am ET = overnight/pre-market, 5:30pm ET = "
                "afternoon after-hours). They are promoted into deep-research focus; "
                "BUY still requires the validator and fat-pitch bar."
            ),
        }
    except Exception as e:
        return {"session": session, "reporters": [], "when": {},
                "as_of": now_et.isoformat(),
                "calendar_status": f"error:{str(e)[:100]}"}
