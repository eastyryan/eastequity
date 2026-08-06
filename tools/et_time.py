"""US/Eastern (market) time — the ONE copy of the ET calendar-day helpers.

WHY THIS FILE EXISTS (2026-08-05 audit). Three divergent private copies of the
same three helpers were live at once:

  * runlib/core.py       et_now / et_date / to_et_date
  * runlib/analytics.py  imported all three from runlib.core at :12 and then
                         SILENTLY SHADOWED them with local redefinitions at
                         :929-958, so which body ran depended on which module
                         you were reading;
  * validator.py         _et_now / _et_today / _to_et_date, with a subtly
                         different fallback chain from either of the above.

The copies disagreed on two edge behaviors: (1) whether a NAIVE timestamp is
treated as UTC or as machine-local time, and (2) how the no-tzdata fallback
shifts an aware timestamp. A date bucketing helper that answers differently per
module is how the same fill lands on two different calendar days in two
reports. All three modules now delegate here.

WHY ET AT ALL: user-facing dates use the MARKET timezone, never UTC. An evening
run (after 8pm ET) is still ~02:00 UTC the NEXT day — stamping UTC would show
viewers "tomorrow's" date on the review blurb, the equity curve, and X posts.

FALLBACK CHOICES (each is the safest of the three former behaviors):
  * Naive timestamps are treated as UTC. Two of the three copies already did
    this, every timestamp this repo writes is `datetime.now(timezone.utc)`, and
    the third copy's behavior (naive = machine-local) silently changed meaning
    between the UTC cloud runner and the operator's ET Mac.
  * zoneinfo/tzdata unavailable (some minimal Linux images): convert the aware
    timestamp to UTC FIRST, then subtract 4 hours (EDT). validator's fallback
    was the only one that did the UTC conversion — the others subtracted 4h in
    whatever offset the timestamp arrived with, double-shifting non-UTC inputs.
    Fixed UTC-4 is what all three copies used; it is only reachable without
    tzdata and at worst mis-dates a stamp by one hour near midnight in winter.
  * Unparseable input: to_et_date returns None (validator's strict contract —
    callers skip the record); to_et_date_str preserves the legacy
    runlib "first 10 chars" salvage that slot reports already depend on.

DEPENDENCY-FREE BY DESIGN: stdlib only. validator.py's header forbids importing
anything that talks to a network or a broker, and runlib/core imports this at
module load — nothing here may ever grow a config read or an env dependency.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def _eastern():
    """ZoneInfo("America/New_York"), or None when tzdata is unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return None


def et_now() -> datetime:
    """Current time in America/New_York. zoneinfo preferred; UTC-4 fallback."""
    tz = _eastern()
    if tz is not None:
        try:
            return datetime.now(tz)
        except Exception:
            pass  # a broken tzdata install behaves like a missing one
    return datetime.now(timezone.utc) - timedelta(hours=4)


def et_today() -> date:
    return et_now().date()


def et_date() -> str:
    return et_now().date().isoformat()


def to_et_date(iso_ts: str | None) -> date | None:
    """Parse an ISO timestamp to its US/Eastern calendar date.

    Strict contract (validator's): unparseable/empty input returns None so the
    caller can skip the record. Naive timestamps are treated as UTC.
    """
    if not isinstance(iso_ts, str) or not iso_ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = _eastern()
        if tz is not None:
            try:
                return dt.astimezone(tz).date()
            except Exception:
                pass
        # No tzdata: normalize to UTC BEFORE the fixed shift, or a non-UTC
        # offset gets double-counted.
        return (dt.astimezone(timezone.utc) - timedelta(hours=4)).date()
    except (ValueError, TypeError, OSError):
        return None


def to_et_date_str(iso_ts) -> str | None:
    """String variant with runlib.core's legacy salvage contract:
    parseable -> "YYYY-MM-DD" in ET; unparseable but truthy -> first 10 chars
    (slot reports group on already-date-shaped strings); falsy -> None."""
    if not iso_ts:
        return None
    d = to_et_date(str(iso_ts))
    return d.isoformat() if d is not None else str(iso_ts)[:10]
