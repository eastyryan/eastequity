"""Real NYSE sessions from Alpaca's calendar, cached to disk.

WHAT THIS REPLACES. Every session boundary in the system was hardcoded:

  * scripts/push_live_prices.sh gated the feeder on `DOW <= 5 && 0900 <= HHMM <= 1630`
  * .github/workflows/live-prices.yml on `*/5 13-21 * * 1-5` -- UTC hours, so the ET
    window silently shifts by an hour across both DST transitions
  * runlib/preflight.py on `strftime("%a") in run_days`
  * autonomy_config slot depths on fixed 0600/0845/1030/1200/1400/1600/1730

None of them knew about holidays or half-days. On Thanksgiving the feeder ran all day
against the prior session's prints; on 2026-11-27 and 2026-12-24 the market closes at
13:00 and everything downstream kept treating 15:00 as a live session.

Note what this does NOT have to carry any more. The stale-price danger on a holiday is
already handled upstream: stop enforcement now gates on TRADE time, so a prior-session
print is rejected regardless of what any calendar says. This module prevents wasted
work, false "missed run" alarms, and trading decisions taken on a day the market never
opened -- not stop misfires.

FAIL-SAFE, NOT FAIL-OPEN. With no cache and no network the answer is the old Mon-Fri
09:30-16:00 assumption, returned with `source="assumed"` so a caller can see it is a
guess. Refusing to trade because a calendar fetch failed would be a worse failure than
the one being fixed -- but pretending a guess is knowledge is how this class of bug
started, so the provenance always travels with the answer.

Pure apart from one cached HTTP GET. Never raises.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache" / "market_calendar.json"
CACHE_MAX_AGE_DAYS = 21          # refresh well before the window runs out
FETCH_AHEAD_DAYS = 180

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                        # pragma: no cover
    _ET = timezone(timedelta(hours=-4))

# Used only when neither cache nor network can answer.
_ASSUMED_OPEN, _ASSUMED_CLOSE = time(9, 30), time(16, 0)


def et_now() -> datetime:
    return datetime.now(_ET)


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _cache_fresh(blob: dict) -> bool:
    try:
        built = datetime.fromisoformat(str(blob.get("built_at")))
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - built).days < CACHE_MAX_AGE_DAYS
    except Exception:
        return False


def refresh(force: bool = False) -> dict:
    """Fetch and cache the session calendar. Returns {DATE: {open, close}}."""
    blob = _load_cache()
    if blob.get("days") and _cache_fresh(blob) and not force:
        return blob["days"]
    try:
        from execution import alpaca_broker
        start = (date.today() - timedelta(days=7)).isoformat()
        end = (date.today() + timedelta(days=FETCH_AHEAD_DAYS)).isoformat()
        code, rows = alpaca_broker._req("GET", "/v2/calendar",
                                        params={"start": start, "end": end})
        if code != 200 or not isinstance(rows, list) or not rows:
            return blob.get("days") or {}
        days = {str(r["date"]): {"open": r.get("open") or "09:30",
                                 "close": r.get("close") or "16:00"}
                for r in rows if isinstance(r, dict) and r.get("date")}
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(
            {"built_at": datetime.now(timezone.utc).isoformat(),
             "n": len(days), "days": days}, indent=2))
        return days
    except Exception:
        # Keep whatever we had; a failed refresh must never erase a good calendar.
        return blob.get("days") or {}


def _days() -> dict:
    blob = _load_cache()
    if blob.get("days") and _cache_fresh(blob):
        return blob["days"]
    return refresh()


def session(day: date | None = None) -> dict:
    """{"is_trading_day", "open", "close", "is_half_day", "source"} for a date.

    `source` is "calendar" when it came from Alpaca and "assumed" when it is the
    Mon-Fri fallback. Callers that care about correctness should look at it.
    """
    day = day or et_now().date()
    days = _days()
    row = days.get(day.isoformat()) if isinstance(days, dict) else None
    if row:
        try:
            oh, om = [int(x) for x in str(row["open"]).split(":")]
            ch, cm = [int(x) for x in str(row["close"]).split(":")]
            close_t = time(ch, cm)
            return {"is_trading_day": True, "open": time(oh, om), "close": close_t,
                    "is_half_day": close_t < _ASSUMED_CLOSE, "source": "calendar"}
        except Exception:
            pass
    if days:
        # We HAVE a calendar and this date is not in it -> genuinely not a session.
        return {"is_trading_day": False, "open": None, "close": None,
                "is_half_day": False, "source": "calendar"}
    return {"is_trading_day": day.weekday() < 5, "open": _ASSUMED_OPEN,
            "close": _ASSUMED_CLOSE, "is_half_day": False, "source": "assumed"}


def is_trading_day(day: date | None = None) -> bool:
    return bool(session(day)["is_trading_day"])


def is_market_open(now: datetime | None = None) -> bool:
    """Is the regular session open right now (ET)?"""
    now = now or et_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    now = now.astimezone(_ET)
    s = session(now.date())
    if not s["is_trading_day"]:
        return False
    return s["open"] <= now.time() <= s["close"]


def minutes_to_close(now: datetime | None = None) -> float | None:
    """Minutes until the session close, or None when not a trading day."""
    now = now or et_now()
    now = now.astimezone(_ET) if now.tzinfo else now.replace(tzinfo=_ET)
    s = session(now.date())
    if not s["is_trading_day"] or s["close"] is None:
        return None
    close_dt = datetime.combine(now.date(), s["close"], tzinfo=_ET)
    return (close_dt - now).total_seconds() / 60.0


if __name__ == "__main__":
    s = session()
    print(json.dumps({**s, "open": str(s["open"]), "close": str(s["close"]),
                      "market_open_now": is_market_open(),
                      "minutes_to_close": minutes_to_close()}, indent=2))
