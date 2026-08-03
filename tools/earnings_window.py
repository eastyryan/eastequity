"""Earnings-window map — the input to the coded pre-print blackout.

WHY THIS EXISTS (2026-08-03). There was no earnings rule in the validator:
`days_to_earnings` appeared zero times in validator.py. The entire "trade around
the binary" behaviour was the brain's own judgment, applied against a 45-day
default horizon — which in earnings season excludes nearly every name in the
universe. On 2026-08-03 the agent rejected MPC and AMGN (reporting 08-04) and
then NET and TEAM (08-06) and made no trade; over the preceding three weeks the
same reasoning appears in run after run. The fix is not to remove earnings
caution but to make it NARROW and CHECKABLE, so the brain can stop substituting
a self-invented exclusion for a real one.

Two outputs per ticker:
  days_to_earnings   - int, from the committed calendar (authoritative, covers
                       all ~184 universe names) or the scan row (fallback, only
                       the ~30 rate-limited enrichment names).
  days_since_earnings- int when known; drives the post-print drift lane.

Absent ticker means NOT MEASURED, never "no print" — the gate stands down rather
than guessing. Pure: no network, no clock beyond the bundle's own as_of date.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# "MPC(bmo), AMGN(amc), FOO(unknown)" -> [("MPC","bmo"), ("AMGN","amc"), ...]
_ENTRY = re.compile(r"\b([A-Z][A-Z0-9.\-]{0,6})\s*\(\s*(bmo|amc|unknown)\s*\)",
                    re.IGNORECASE)


def _as_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_by_date(by_date: Any) -> dict[str, tuple[date, str]]:
    """{'2026-08-04': 'MPC(bmo), AMD(amc)'} -> {'MPC': (date, 'bmo'), ...}.

    Keeps the EARLIEST print per ticker: a name listed twice in the window is
    gated on the nearer date, which is the conservative read.
    """
    out: dict[str, tuple[date, str]] = {}
    if not isinstance(by_date, dict):
        return out
    for raw_day, names in by_date.items():
        day = _as_date(raw_day)
        if day is None:
            continue
        if isinstance(names, (list, tuple)):
            names = ", ".join(str(n) for n in names)
        if not isinstance(names, str):
            continue
        for ticker, when in _ENTRY.findall(names):
            t = ticker.upper()
            prev = out.get(t)
            if prev is None or day < prev[0]:
                out[t] = (day, when.lower())
    return out


def build_earnings_map(context: dict | None,
                       calendar: dict | None = None,
                       today: date | None = None) -> dict[str, dict]:
    """Per-ticker earnings timing for the validator gate.

    Returns {TICKER: {days_to_earnings, days_since_earnings, when, source,
                      calendar_stale}}. Only keys we can actually date appear.

    Sources, richest first:
      calendar      - data/earnings_calendar.json `by_ticker`, which carries BOTH
                      `last` and `next` per name. This is the only source that can
                      populate days_since_earnings, and therefore the only one that
                      makes the POST-PRINT DRIFT lane detectable. earnings_week is
                      a 7-day forward window and structurally cannot see a print
                      that already happened.
      earnings_week - the bundle's forward window (all ~184 names, next 7 days).
      universe_scan - per-row enrichment, ~30 names, rate-limited.
    """
    context = context or {}
    ew = context.get("earnings_week") or {}
    # `today` may arrive as a date or an ET date string; _as_date takes both.
    as_of = _as_date(ew.get("as_of")) or _as_date(today)
    stale = bool(ew.get("stale"))
    out: dict[str, dict] = {}

    if isinstance(calendar, dict) and as_of is not None:
        for ticker, row in calendar.items():
            if not isinstance(row, dict):
                continue
            t = str(ticker).upper()
            nxt = _as_date(row.get("next"))
            last = _as_date(row.get("last"))
            if nxt is None and last is None:
                continue
            out[t] = {
                "days_to_earnings": (nxt - as_of).days if nxt else None,
                "days_since_earnings": (as_of - last).days if last else None,
                "when": row.get("next_when"),
                "earnings_date": nxt.isoformat() if nxt else None,
                "source": "earnings_calendar",
                "calendar_stale": stale,
            }

    if as_of is not None:
        for ticker, (day, when) in parse_by_date(ew.get("by_date")).items():
            # The bundle's forward window wins on the UPCOMING print (it is what
            # the run was actually handed), but must not erase a days_since read
            # the calendar file supplied — that is the drift lane's only input.
            prior_since = (out.get(ticker) or {}).get("days_since_earnings")
            out[ticker] = {
                "days_to_earnings": (day - as_of).days,
                "days_since_earnings": prior_since,
                "when": when,
                "earnings_date": day.isoformat(),
                "source": "earnings_week",
                "calendar_stale": stale,
            }

    # Fallback: the scanner's per-ticker enrichment. Only fills names the
    # calendar did not carry — the calendar is authoritative where both exist.
    scan = context.get("universe_scan") or {}
    lanes = ("top_setups", "contrarian_setups", "deep_value_200w",
             "supplier_pullbacks", "post_earnings_drift", "focus")
    for lane in lanes:
        for row in (scan.get(lane) or []):
            if not isinstance(row, dict):
                continue
            t = str(row.get("ticker") or "").upper()
            if not t:
                continue
            dte = row.get("days_to_earnings")
            dse = row.get("days_since_earnings")
            if t in out:
                # Calendar wins on days_to_earnings; still take days_since if new.
                if out[t].get("days_since_earnings") is None and isinstance(dse, int):
                    out[t]["days_since_earnings"] = dse
                continue
            if not isinstance(dte, int) and not isinstance(dse, int):
                continue
            out[t] = {
                "days_to_earnings": dte if isinstance(dte, int) else None,
                "days_since_earnings": dse if isinstance(dse, int) else None,
                "when": None,
                "earnings_date": None,
                "source": "universe_scan",
                "calendar_stale": stale,
            }
    return out


def earnings_state(row: dict | None, *, blackout_days: int,
                   drift_days: int) -> dict:
    """Classify one ticker's earnings timing.

    in_blackout  - a scheduled print lands within blackout_days (inclusive).
                   0 means it reports today; negative values are past prints and
                   never blackout.
    in_drift     - reported within the last drift_days: the post-print lane,
                   where the binary is RESOLVED and a swing entry is clean.
    """
    row = row or {}
    dte = row.get("days_to_earnings")
    dse = row.get("days_since_earnings")
    in_blackout = isinstance(dte, int) and 0 <= dte <= blackout_days
    in_drift = False
    if isinstance(dse, int) and 0 <= dse <= drift_days:
        in_drift = True
    elif isinstance(dte, int) and -drift_days <= dte < 0:
        # Calendar dates that have already passed read as negative days_to.
        in_drift = True
    return {
        "days_to_earnings": dte,
        "days_since_earnings": dse,
        "in_blackout": in_blackout,
        "in_drift": in_drift,
        "when": row.get("when"),
        "earnings_date": row.get("earnings_date"),
        "measured": isinstance(dte, int) or isinstance(dse, int),
    }
