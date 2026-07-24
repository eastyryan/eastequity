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
import time
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

# Keep module import light (pure helpers stay offline-testable): resolve ROOT
# locally and pull et_now lazily inside the network paths, so importing this
# module never requires dotenv/pandas.
ROOT = Path(__file__).resolve().parent.parent
_CACHE = ROOT / "data" / "earnings_calendar.json"
_CACHE_MAX_AGE_MIN = 90  # at most ~one refresh per slot

# THE INCIDENT (2026-07-18 data-integrity audit)
# ----------------------------------------------
# data/earnings_calendar.json read {"by_ticker": {}, "status": "empty", "n": 0}
# for its ENTIRE committed history. The sweep never once succeeded on the cloud
# gather node, and nothing anywhere said so. Four defects conspired:
#   1. every ticker was swallowed by a bare `except Exception: continue` - no
#      counter, no reason, no evidence a fetch had even been attempted;
#   2. `ok` counted only successes and the status was `"ok" if ok else "empty"`,
#      so a 100%-FAILED sweep reported "empty" and NEVER "error";
#   3. `built_at` was refreshed even when ok == 0, so a totally-failed sweep
#      wrote a FRESH timestamp over a dead calendar - and the 90-minute
#      `_cache_fresh` guard then suppressed the retry that would have healed it;
#   4. by_ticker was seeded from the CACHED map, so stale entries persisted
#      forever wearing that fresh timestamp with no way to age them out.
# Consequence: `days_to_earnings` was ABSENT on every scanned row. CLAUDE.md
# instructs the brain to "respect the binary-print problem" and to choose the
# earnings path "through binary vs around it" - and an ABSENT days_to_earnings
# reads as "no print imminent", the exact INVERSE of the truth. The system walked
# into earnings blind for weeks while every status field said it was fine.
#
# ROOT CAUSE of the fetch failure: NOT an API change. yf.Ticker(t).earnings_dates
# still returns the documented DataFrame (verified against yfinance 1.2.0: a
# full 182-name universe sweep returns 182/182 from a residential IP). The sweep
# fired ~182 sequential unauthenticated Yahoo requests with NO pause from a
# GitHub-hosted runner (.github/workflows/gather-data.yml: `runs-on:
# ubuntu-latest`, i.e. an Azure datacenter IP), which Yahoo rate-limits
# aggressively. Fixes below: throttle the sweep so we stop inflicting the limit
# on ourselves, count and report what actually happened, and fall back to a
# keyless EDGAR source that cloud IPs CAN still reach.

# Throttle discipline ported from tools/discovery_screen.py (CHUNK_SIZE /
# CHUNK_SLEEP_SEC), whose weekly sweep has never had this problem. The Yahoo
# earnings endpoint has no batch form, so the unit is a ticker, not a chunk:
# pause every _SWEEP_CHUNK names.
_SWEEP_CHUNK = 20
_SWEEP_SLEEP_SEC = 1.0
# Stop hammering a feed that is clearly refusing us. After this many CONSECUTIVE
# failures the sweep aborts and reports honestly rather than burning the gather
# node's 15-minute budget (gather-data.yml `timeout-minutes: 15`) on 182 calls
# that will all fail.
_SWEEP_ABORT_AFTER_CONSECUTIVE_FAILURES = 25

# Per-entry aging. Entries carry `fetched_at` so a name we have not successfully
# refreshed in this long is DROPPED instead of being served forever under a fresh
# built_at. Sized well past a quarterly cadence so a healthy name is never aged
# out between prints, but short enough that a delisted/renamed ticker does not
# haunt the calendar indefinitely.
_ENTRY_MAX_AGE_DAYS = 120

# Bound on the keyless EDGAR fallback sweep (one submissions call per name). Only
# runs for names the primary feed failed on, and only when something failed.
_EDGAR_FALLBACK_MAX_TICKERS = 40


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


def age_out_entries(by_ticker: dict, now_et: datetime,
                    max_age_days: int = _ENTRY_MAX_AGE_DAYS) -> "tuple[dict, int]":
    """Drop carried-forward entries not successfully refreshed in max_age_days.

    Returns (kept, dropped_count). Pure (no clock, no IO) so it is unit-tested
    offline.

    THE INCIDENT THIS PREVENTS (defect 4 above): by_ticker was seeded from the
    CACHED map on every build, so a ticker fetched once and never again survived
    forever - and because built_at was refreshed unconditionally, it survived
    wearing a timestamp minutes old. `reporters_for_session` then compared a
    months-old "last" against a session window, and the freshness of the calendar
    as a whole was unauditable. Entries WITHOUT a `fetched_at` are legacy rows
    written before stamping existed; they are kept once (we cannot PROVE they are
    stale) but the next successful fetch stamps them, so they age out normally
    thereafter.
    """
    if not isinstance(by_ticker, dict):
        return {}, 0
    cutoff = now_et - timedelta(days=max_age_days)
    kept: dict = {}
    dropped = 0
    for tk, info in by_ticker.items():
        if not isinstance(info, dict):
            dropped += 1
            continue
        stamped = _parse_et(info.get("fetched_at"))
        if stamped is not None and stamped < cutoff:
            dropped += 1
            continue
        kept[tk] = info
    return kept, dropped


def _last_earnings_8k_from_edgar(ticker: str, lookback_days: int = 120):
    """Most recent 8-K item 2.02 filing date for `ticker`, or None.

    KEYLESS EDGAR FALLBACK for the cloud. Item 2.02 is "Results of Operations and
    Financial Condition" - the item a company is REQUIRED to file when it
    releases earnings, so its filing date is an authoritative "this name just
    reported" marker. This exists because Yahoo is the single point of failure
    for the whole calendar and is exactly what a GitHub-hosted (Azure) runner
    cannot reach reliably, whereas EDGAR goes through tools/net.py's proxy
    fallback and stays reachable.

    LIMITATION, stated plainly: this recovers `last` only. There is no keyless
    source for a FUTURE earnings date, so `next` (and therefore
    days_to_earnings) cannot be reconstructed this way - see
    build_earnings_calendar's `next_source_note`.
    """
    from tools.sec_filings import _get, ticker_to_cik

    cik = ticker_to_cik(ticker)
    if not cik:
        return None
    sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = (sub.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or []
    cutoff = (datetime.now() - timedelta(days=lookback_days)).date().isoformat()
    for i, form in enumerate(forms):
        if not str(form).startswith("8-K"):
            continue
        fdate = dates[i] if i < len(dates) else None
        if not fdate or fdate < cutoff:
            continue
        # strip(): EDGAR space-pads item lists ("2.02, 9.01"); an unstripped
        # " 2.02" silently drops the filing (the same bug partnerships.py:66
        # documents fixing on its own item scan).
        row_items = {s.strip() for s in str(items[i] if i < len(items) else "").split(",")}
        if "2.02" in row_items:
            return fdate
    return None


def build_earnings_calendar(tickers: Iterable[str], *, now_et: datetime | None = None,
                            force: bool = False) -> dict:
    """Per-ticker {last, last_when, next, next_when} for the universe.

    yfinance-backed, wrapped, and THROTTLED. Never raises.

    STATUS CONTRACT (tools/freshness_audit.feed_status; see the module-level
    incident note):
      "ok"        every attempted name answered
      "degraded"  some answered, some failed - counts in `coverage`
      "empty"     every name was REACHABLE and none published an earnings date
      "error"     every attempted name FAILED - the feed is dead, and the
                  calendar you are holding is the previous one, not today's
    The "empty" vs "error" split is the whole point: an empty calendar means "no
    universe name has a scheduled print", while a dead feed means "we do not know
    when anyone reports". The old code returned "empty" for both.

    PERSISTENCE RULES (mirroring tools/freshness_audit.audit_universe's refusal
    to overwrite a good artifact with an all-error audit):
      * `built_at` is refreshed ONLY when at least one name succeeded. A failed
        sweep leaves the previous built_at intact and records `last_attempt_at`
        instead, so the freshness guard cannot be fooled into suppressing the
        retry that would have healed the calendar.
      * A fully-failed sweep is NOT written to disk at all when a non-empty cache
        exists - it would replace real data with nothing.
      * Every successfully fetched entry is stamped `fetched_at`; carried-forward
        entries older than _ENTRY_MAX_AGE_DAYS are aged out.
    """
    now_et = now_et or _et_now()
    cached = _load_cache()
    if not force and _cache_fresh(cached, now_et) and (cached.get("by_ticker")):
        return cached
    wanted = sorted({str(x).upper() for x in (tickers or []) if x})
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as e:
        # A missing dependency is an ERROR, not an empty market. Preserve the
        # cached calendar and the timestamp that describes when it was real.
        # (The old label "yfinance_unavailable" is in no dead-status tuple the
        # orchestrator knows, so it read as a perfectly healthy feed.)
        stub = dict(cached or {})
        stub.setdefault("by_ticker", {})
        stub["status"] = "error"
        stub["reason"] = f"yfinance_unavailable: {type(e).__name__}"
        stub["last_attempt_at"] = now_et.isoformat()
        stub["coverage"] = {"requested": len(wanted), "succeeded": 0,
                            "failed": len(wanted)}
        return stub

    carried, aged_out = age_out_entries(cached.get("by_ticker") or {}, now_et)
    by_ticker: dict[str, dict] = dict(carried)
    now_ts = pd.Timestamp.now(tz="America/New_York")
    stamp = now_et.isoformat()

    ok = 0                 # names that returned at least one usable date
    reachable = 0          # names whose fetch did not raise (incl. "no dates")
    failed = 0             # names whose fetch RAISED - the invisible case before
    no_dates = 0           # reachable but publishes no earnings dates
    consecutive_failures = 0
    aborted = False
    failures: dict = {}

    for i, t in enumerate(wanted):
        # Self-inflicted throttling was the root cause; pause like the weekly
        # discovery screen does rather than firing ~182 back-to-back requests.
        if i and i % _SWEEP_CHUNK == 0:
            time.sleep(_SWEEP_SLEEP_SEC)
        try:
            ed = yf.Ticker(t).earnings_dates
        except Exception as e:
            failed += 1
            consecutive_failures += 1
            if len(failures) < 10:
                failures[t] = f"{type(e).__name__}: {str(e)[:80]}"
            if consecutive_failures >= _SWEEP_ABORT_AFTER_CONSECUTIVE_FAILURES:
                aborted = True
                break
            continue
        consecutive_failures = 0
        reachable += 1
        try:
            if ed is None or not len(ed.index):
                no_dates += 1
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
                rec["fetched_at"] = stamp   # lets this entry age out later
                rec["source"] = "yfinance"
                by_ticker[t] = rec
                ok += 1
            else:
                no_dates += 1
        except Exception as e:
            # A parse failure is still a failure: it must never be counted as
            # "this name has no earnings".
            failed += 1
            if len(failures) < 10:
                failures[t] = f"parse {type(e).__name__}: {str(e)[:80]}"

    attempted = ok + no_dates + failed
    unattempted = len(wanted) - attempted

    # Keyless EDGAR fallback for `last` on names Yahoo could not deliver. Bounded
    # so a total Yahoo outage does not become an unbounded EDGAR sweep on a
    # 15-minute gather budget. Only runs when something actually failed.
    edgar_recovered = 0
    if failed:
        budget = _EDGAR_FALLBACK_MAX_TICKERS
        for t in wanted:
            if budget <= 0:
                break
            if t in by_ticker and by_ticker[t].get("fetched_at") == stamp:
                continue      # Yahoo already answered for this name this sweep
            budget -= 1
            try:
                filed = _last_earnings_8k_from_edgar(t)
            except Exception:
                continue
            if not filed:
                continue
            # A filing DATE carries no session time, so `when` is honestly
            # unknown - we do not invent a bmo/amc we cannot observe.
            prev = by_ticker.get(t) or {}
            rec = dict(prev)
            rec["last"] = f"{filed}T00:00:00"
            rec["last_when"] = "unknown"
            rec["fetched_at"] = stamp
            rec["source"] = "edgar_8k_item_202"
            rec["source_note"] = ("recovered from the 8-K item 2.02 filing date "
                                  "because the earnings feed failed for this name; "
                                  "no session time and no NEXT date is available "
                                  "from this source")
            by_ticker[t] = rec
            edgar_recovered += 1

    succeeded = ok + edgar_recovered
    if attempted <= 0:
        status = "skipped"
    elif succeeded <= 0 and failed > 0:
        status = "error"
    elif failed > 0:
        status = "degraded"
    elif succeeded <= 0:
        status = "empty"
    else:
        status = "ok"

    out: dict = {
        "by_ticker": by_ticker,
        "status": status,
        "n": len(by_ticker),
        "fetched_this_sweep": succeeded,
        "coverage": {
            "requested": len(wanted),
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": failed,
            "reachable_no_dates": no_dates,
            "unattempted": unattempted,
            "aged_out_entries": aged_out,
            "recovered_from_edgar_8k": edgar_recovered,
            "aborted_after_consecutive_failures": aborted,
            "failures": failures,
        },
        "next_source_note": (
            "`next` (and therefore days_to_earnings) comes ONLY from the "
            "yfinance earnings endpoint. The EDGAR item-2.02 fallback can "
            "reconstruct `last` when that endpoint is blocked, but there is no "
            "keyless source for a FUTURE earnings date - so on a degraded sweep "
            "a MISSING `next` means UNKNOWN, never 'no print scheduled'."),
    }
    # built_at describes when the calendar was last GENUINELY rebuilt. Refreshing
    # it on a failed sweep is what let a permanently-dead feed look fresh for
    # weeks AND suppressed the 90-minute retry that could have healed it.
    if succeeded > 0:
        out["built_at"] = stamp
    else:
        if cached.get("built_at"):
            out["built_at"] = cached["built_at"]
        out["last_attempt_at"] = stamp
        out["stale_notice"] = (
            "This calendar was NOT rebuilt on the latest attempt: "
            f"{failed} of {attempted} fetches failed. Entries are carried "
            "forward from built_at. Absent days_to_earnings here means UNKNOWN, "
            "not 'no earnings imminent'.")

    # Never overwrite a real calendar with the wreckage of a dead sweep (the same
    # rule freshness_audit applies to its own artifact). An all-failed sweep with
    # a non-empty cache keeps the artifact it would otherwise have destroyed.
    if status == "error" and (cached.get("by_ticker") or {}):
        out["artifact_error"] = ("not persisted: every fetch failed - keeping the "
                                 "last-good calendar on disk")
    else:
        try:
            _CACHE.write_text(json.dumps(out, indent=2))
        except Exception:
            pass
    return out


def earnings_week(by_ticker: dict, *, today: date_cls, days: int = 7,
                  universe: Iterable[str] | None = None) -> dict:
    """WHO REPORTS THIS WEEK, grouped by date. Pure (no clock, no IO, no network).

    The calendar already holds `next` + `next_when` for the WHOLE universe (184
    names as of 2026-07-23), but nothing ever surfaced it that way. Earnings data
    reached the brain only through the scanner's per-ticker enrichment, which is
    rate-limit-capped at ~30 names a run — so ~157 names carried NO earnings clock,
    and a proposal on one of them had nothing in the bundle to corroborate a claimed
    print against. That is the gap behind MU on 2026-07-22: the name was outside the
    focus set, so the brain reached for a web search the code cannot verify.

    This costs nothing extra. Earnings are QUARTERLY, so the calendar is refreshed
    incrementally (entries are re-fetched only once past _ENTRY_MAX_AGE_DAYS — the
    last real sweep requested 5 names and carried 179 forward). Reading it is a dict
    lookup; there is no reason every name should not have its date visible.

    Returns dates as ISO strings and `when` as bmo/amc/unknown. `days=7` gives the
    trading week ahead; today is included (a print happening today is the single
    most decision-relevant one there is).
    """
    horizon = today + timedelta(days=int(days))
    allow = {str(t).upper() for t in universe} if universe else None
    by_date: dict[str, list] = {}
    for tkr, rec in (by_ticker or {}).items():
        t = str(tkr).upper()
        if allow is not None and t not in allow:
            continue
        nxt = (rec or {}).get("next")
        if not nxt:
            continue
        day_s = str(nxt)[:10]
        try:
            day = date_cls.fromisoformat(day_s)
        except Exception:
            continue
        if not (today <= day <= horizon):
            continue
        by_date.setdefault(day_s, []).append(
            {"ticker": t, "when": (rec or {}).get("next_when") or "unknown"})
    for d in by_date:
        # bmo before amc before unknown, then alphabetical — a stable, readable order.
        by_date[d].sort(key=lambda r: ({"bmo": 0, "amc": 1}.get(r["when"], 2), r["ticker"]))
    total = sum(len(v) for v in by_date.values())
    return {
        "as_of": today.isoformat(),
        "window_days": int(days),
        "through": horizon.isoformat(),
        "count": total,
        "by_date": {d: by_date[d] for d in sorted(by_date)},
        "today": by_date.get(today.isoformat(), []),
        "note": (
            "Every universe name reporting in this window, from the committed "
            "earnings calendar — NOT limited to this run's ~30 deep-research focus "
            "names. `when` is bmo (before open) / amc (after close) / unknown. Use it "
            "for the earnings-path rule: trade THROUGH a binary or AROUND it. A name "
            "listed here reports inside your swing window even if it carries no other "
            "earnings field on its scan row. If a name is NOT listed, that means the "
            "calendar has no scheduled print for it in this window — it does NOT mean "
            "the name is safe to assume has no print; check calendar_age_days below."
        ),
    }


def earnings_week_from_cache(*, now_et: datetime | None = None, days: int = 7,
                             universe: Iterable[str] | None = None) -> dict:
    """earnings_week() over the committed calendar, with staleness disclosed.

    Read-only and fail-soft: never builds, never fetches, never raises. The whole
    point is that this is free to call on every gather at any depth.
    """
    try:
        cached = _load_cache() or {}
        now = now_et or _et_now()
        out = earnings_week(cached.get("by_ticker") or {}, today=now.date(),
                            days=days, universe=universe)
        built = _parse_et(cached.get("built_at"))
        age_days = (now - built).days if built else None
        out["calendar_built_at"] = cached.get("built_at")
        out["calendar_age_days"] = age_days
        out["calendar_status"] = cached.get("status")
        out["names_in_calendar"] = len(cached.get("by_ticker") or {})
        # A stale calendar is still useful (dates are quarterly) but the brain must
        # know: a name that reported since the last sweep may show a PAST-quarter
        # date, and a newly scheduled print may be missing entirely.
        out["stale"] = bool(age_days is not None and age_days > 7)
        if out["stale"]:
            out["stale_note"] = (
                f"Earnings calendar was last rebuilt {age_days} days ago. Dates are "
                f"quarterly so most remain correct, but treat a missing name as "
                f"UNKNOWN rather than as 'no print scheduled'.")
        return out
    except Exception as e:
        return {"count": 0, "by_date": {}, "today": [], "error": str(e)[:160],
                "note": "earnings calendar unavailable this run — treat every "
                        "name's earnings timing as UNKNOWN, not as absent."}


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
        cal_status = cal.get("status")
        out = {
            "session": session,
            "reporters": reps,
            "when": when,
            "as_of": now_et.isoformat(),
            "calendar_status": cal_status,
            "calendar_coverage": cal.get("coverage"),
            "calendar_built_at": cal.get("built_at"),
            "note": (
                "Universe names that reported earnings and triggered a FORCED full "
                "deep dive this run (9am ET = overnight/pre-market, 5:30pm ET = "
                "afternoon after-hours). They are promoted into deep-research focus; "
                "BUY still requires the validator and fat-pitch bar."
            ),
        }
        # An EMPTY reporter list off a DEAD calendar is not "nobody reported" -
        # it is "we do not know who reported". Say so, loudly, on the object the
        # orchestrator reads, so the earnings-escalation path cannot be silently
        # skipped by an outage (the failure mode that let this run blind for
        # weeks while calendar_status read a benign "empty").
        if cal_status in ("error", "degraded"):
            out["reporters_incomplete"] = True
            out["warning"] = (
                f"earnings calendar status={cal_status}: the reporter list is "
                "INCOMPLETE, not empty. Names that reported may be missing, so "
                "an absent forced deep dive is not evidence that nobody printed.")
        return out
    except Exception as e:
        return {"session": session, "reporters": [], "when": {},
                "as_of": now_et.isoformat(),
                "calendar_status": f"error:{str(e)[:100]}"}
