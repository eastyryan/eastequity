"""Shadow portfolio — counterfactual book of skips and untriggered watches.

Every rejected idea and watchlist trigger that is NOT bought becomes a shadow
trade. We mark it to market over 30/60/90 days and score:
  - max_upside_pct / max_drawdown_pct
  - would_hit_target (+10% default, swing floor)
  - would_hit_stop (−ATR-ish default −12% if no plan stop)
  - verdict: good_skip | regret_miss | mixed | open | expired

Persists to data/shadow_portfolio.json. Brain-facing summary binds after enough
closed shadows so the agent learns what it missed vs correctly skipped.

Fail-soft; never raises from public APIs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADOW_FILE = ROOT / "data" / "shadow_portfolio.json"

DEFAULT_TARGET_PCT = 0.10
DEFAULT_STOP_PCT = 0.12
HORIZON_DAYS = 90

# One shadow per ticker per window (was per ticker+source, which triple-counted).
DEDUPE_DAYS = 5

# OPEN-BOOK CAP. The old cap was 80 with `positions[-80:]` — a tail slice that keeps
# the NEWEST and silently discards the OLDEST, i.e. exactly the shadows closest to
# the 30-day resolution threshold. At the observed ~9.6 new shadows/day nothing
# survived past ~8 days, while closing requires 30. The book therefore could never
# produce a single closed shadow: 48 open / 0 closed, `binding` (n_closed >= 8)
# unreachable, regret_rate_pct permanently None. The cap must exceed the open rate
# times the full horizon, and eviction must drop the LEAST informative rows.
MAX_OPEN_SHADOWS = 1200
MARK_WINDOWS = (30, 60, 90)

# Benchmark used for beta attribution on close.
BENCHMARK_TICKER = "SPY"
# A market move only "explains" a shadow's excursion when it is BOTH absolutely
# large and proportionally large. Either test alone lies: a 0.4% tape against a
# 0.5% wobble is a ratio, not an explanation, and a -1.5% SPY against a -17%
# collapse is a rounding error dressed as a cause.
BENCH_MIN_ABS_MOVE_PCT = 2.0
BENCH_MIN_EXPLAINED_FRACTION = 0.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().date().isoformat()


def _load() -> dict:
    if not SHADOW_FILE.exists():
        return {"version": 1, "positions": [], "closed": [], "updated_at": None}
    try:
        d = json.loads(SHADOW_FILE.read_text())
        if not isinstance(d, dict):
            return {"version": 1, "positions": [], "closed": [], "updated_at": None}
        d.setdefault("positions", [])
        d.setdefault("closed", [])
        return d
    except Exception:
        return {"version": 1, "positions": [], "closed": [], "updated_at": None}


def _save(state: dict) -> None:
    SHADOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now().isoformat()
    SHADOW_FILE.write_text(json.dumps(state, indent=2, default=str))


def _evict(positions: list) -> list:
    """Trim the open book to MAX_OPEN_SHADOWS, dropping the LEAST informative first.

    The old `positions[-80:]` kept the newest and discarded the oldest — deleting
    precisely the observations about to resolve. Here the ordering is inverted:
    the oldest shadows are the most valuable (closest to their 30/90-day marks), so
    when the cap does bind we drop the YOUNGEST, which lose nothing but a few days
    of tracking. Pure.
    """
    if len(positions) <= MAX_OPEN_SHADOWS:
        return positions
    # Oldest first; keep that prefix.
    ordered = sorted(positions, key=lambda p: str(p.get("opened_at") or ""))
    return ordered[:MAX_OPEN_SHADOWS]


def _parse_day(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")[:10])
    except Exception:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d")
        except Exception:
            return None


def open_shadow(
    *,
    ticker: str,
    entry_price: float,
    source: str,
    reason: str,
    run_id: str | None = None,
    target_pct: float = DEFAULT_TARGET_PCT,
    stop_pct: float = DEFAULT_STOP_PCT,
    extra: dict | None = None,
) -> dict | None:
    """Open a shadow position if not already open for same ticker+source recently."""
    try:
        t = str(ticker or "").upper()
        if not t or not isinstance(entry_price, (int, float)) or entry_price <= 0:
            return None
        state = _load()
        # Dedupe on TICKER ALONE, not (ticker, source). Keying on both meant one
        # skipped idea opened up to three shadows — 48 open shadows covered only 28
        # tickers, with AMD/ANET/HPE/UNH each holding three. regret_rate_pct divides
        # by shadow count, so the names the brain watches most got up to 3x weight in
        # its own regret statistics.
        for p in state["positions"]:
            if p.get("ticker") != t:
                continue
            opened = _parse_day(p.get("opened_at"))
            if opened and (_now() - opened.replace(tzinfo=timezone.utc)).days < DEDUPE_DAYS:
                return None  # already tracking
        pos = {
            "id": f"SH-{uuid.uuid4().hex[:10]}",
            "ticker": t,
            "source": source,  # rejected_idea | watchlist_trigger | watchlist_hold
            "reason": str(reason or "")[:400],
            "entry_price": round(float(entry_price), 4),
            "opened_at": _today(),
            "run_id": run_id,
            "target_pct": float(target_pct),
            "stop_pct": float(stop_pct),
            "target_price": round(float(entry_price) * (1 + target_pct), 4),
            "stop_price": round(float(entry_price) * (1 - stop_pct), 4),
            "high_water": round(float(entry_price), 4),
            "low_water": round(float(entry_price), 4),
            "last_price": round(float(entry_price), 4),
            "last_mark_at": _today(),
            "marks": [],
            "status": "open",
            **(extra or {}),
        }
        state["positions"].append(pos)
        state["positions"] = _evict(state["positions"])
        _save(state)
        return pos
    except Exception:
        return None


def record_from_rejected_ideas(
    rejected: list,
    prices: dict,
    run_id: str | None = None,
) -> int:
    """Open shadows from process-gate rejected_ideas. Returns count opened."""
    n = 0
    try:
        for row in rejected or []:
            if not isinstance(row, dict):
                continue
            t = str(row.get("ticker") or "").upper()
            px = prices.get(t)
            if px is None:
                continue
            try:
                px = float(px)
            except (TypeError, ValueError):
                continue
            if open_shadow(
                ticker=t, entry_price=px, source="rejected_idea",
                reason=str(row.get("reason") or "rejected on full run"),
                run_id=run_id,
            ):
                n += 1
    except Exception:
        return n
    return n


def record_from_watchlist(
    watchlist: list,
    prices: dict,
    trigger_alerts: list | None = None,
    run_id: str | None = None,
) -> int:
    """Open shadows for trigger hits (priority) and hold-status names with prices."""
    n = 0
    try:
        alert_tickers = {
            str(a.get("ticker") or "").upper()
            for a in (trigger_alerts or []) if isinstance(a, dict)
        }
        for a in trigger_alerts or []:
            if not isinstance(a, dict):
                continue
            t = str(a.get("ticker") or "").upper()
            px = a.get("last_price") or prices.get(t)
            if not t or px is None:
                continue
            try:
                px = float(px)
            except (TypeError, ValueError):
                continue
            level = a.get("parsed_level")
            entry = float(level) if isinstance(level, (int, float)) and level > 0 else px
            if open_shadow(
                ticker=t, entry_price=entry, source="watchlist_trigger",
                reason=str(a.get("trigger_text") or a.get("note") or "trigger hit, not bought"),
                run_id=run_id,
                extra={"trigger_level": level, "spot_at_alert": px},
            ):
                n += 1
        for w in watchlist or []:
            if not isinstance(w, dict):
                continue
            t = str(w.get("ticker") or "").upper()
            if not t or t in alert_tickers:
                continue
            if str(w.get("status") or "").lower() == "buy":
                continue  # may be promoted this run
            px = prices.get(t)
            if px is None:
                continue
            try:
                px = float(px)
            except (TypeError, ValueError):
                continue
            # Only open hold-shadows occasionally: first time we see them this week
            # is enough — open_shadow dedupes 5d
            if open_shadow(
                ticker=t, entry_price=px, source="watchlist_hold",
                reason=str(w.get("one_line") or w.get("thoughts") or "on watchlist, not bought")[:400],
                run_id=run_id,
            ):
                n += 1
    except Exception:
        return n
    return n


def close_shadow_if_bought(ticker: str, fill_price: float | None = None) -> int:
    """If we actually bought the name, close open shadows as 'acted' (not regret)."""
    n = 0
    try:
        t = str(ticker or "").upper()
        state = _load()
        keep = []
        for p in state["positions"]:
            if p.get("ticker") != t:
                keep.append(p)
                continue
            p = dict(p)
            p["status"] = "acted"
            p["closed_at"] = _today()
            p["verdict"] = "acted_bought"
            p["exit_price"] = fill_price
            p["note"] = "Real BUY opened — shadow closed as acted (not a miss)."
            state.setdefault("closed", []).append(p)
            n += 1
        state["positions"] = keep
        if n:
            _save(state)
    except Exception:
        return n
    return n


def _needs_bar_check(p: dict, age_days: int) -> bool:
    """Should we spend a bar download on this shadow? Pure.

    Two rules, both about not paying for information we already have:

    1. A DAY-ZERO shadow has exactly one bar — the one its entry price came
       from. Fetching for it would download history for every idea skipped this
       morning, every morning, and learn nothing.

    2. A SAMPLED TOUCH IS PROOF; a sampled NON-touch is merely UNPROVEN. Sampling
       can only miss an excursion, never invent one, so once low_water is already
       through the stop (or high_water through the target) the verdict is settled
       and real bars cannot overturn it. Bars are only worth buying for the
       unproven side.
    """
    try:
        if age_days is None or int(age_days) < 1:
            return False
        entry = float(p.get("entry_price") or 0)
        if entry <= 0:
            return False
        lo = float(p.get("low_water") if p.get("low_water") is not None else entry)
        hi = float(p.get("high_water") if p.get("high_water") is not None else entry)
        stop = float(p.get("stop_price") or 0)
        target = float(p.get("target_price") or 0)
        if stop > 0 and lo <= stop:
            return False
        if target > 0 and hi >= target:
            return False
        return True
    except (TypeError, ValueError):
        return False


def _fetch_bars_for(tickers, start: str, end: str) -> dict:
    """{TICKER: {date: {high, low, close}}} for the unproven shadows, in ONE
    batched call. Network + cached + fail-soft: a blocked feed returns {} and
    every touch flag falls back to the sampled evidence, which is honest because
    sampling under-detects rather than over-detects."""
    try:
        from tools.benchmark import daily_ohlc_batch
        return daily_ohlc_batch(tickers, start, end) or {}
    except Exception:
        return {}


def _bar_window_extremes(bars: dict | None, start: str, end: str) -> dict:
    try:
        from tools.benchmark import window_extremes
        return window_extremes(bars, start, end)
    except Exception:
        return {"high": None, "low": None, "n_bars": 0}


def _benchmark_window_return_pct(bench_bars: dict | None, start: str, end: str):
    """Benchmark % return over exactly this shadow's window, from the same daily
    bars. None when unknowable — never 0, which would read as "the tape was
    flat" and quietly charge an entire market drawdown to the name."""
    try:
        rows = {str(d)[:10]: b for d, b in (bench_bars or {}).items()
                if isinstance(b, dict) and b.get("close") is not None}
        if not rows:
            return None
        s, e = str(start)[:10], str(end)[:10]
        before = sorted(d for d in rows if d <= s)
        upto = sorted(d for d in rows if d <= e)
        if not before or not upto:
            return None
        first, last = float(rows[before[-1]]["close"]), float(rows[upto[-1]]["close"])
        if first <= 0:
            return None
        return round((last / first - 1) * 100, 2)
    except Exception:
        return None


def _excursion_pct(p: dict):
    """The shadow's OWN move in the direction its verdict is about. Pure."""
    up = p.get("max_upside_pct")
    dd = p.get("max_drawdown_pct")
    verdict = str(p.get("verdict") or "")
    if verdict == "good_skip":
        return dd if isinstance(dd, (int, float)) else None
    if verdict == "regret_miss":
        return up if isinstance(up, (int, float)) else None
    cands = [x for x in (up, dd) if isinstance(x, (int, float))]
    return max(cands, key=abs) if cands else None


def _benchmark_explained_fraction(p: dict):
    """How much of this shadow's excursion the tape moved with it. Pure.

    None when there is no benchmark window; 0.0 when the benchmark moved the
    OTHER way, which explains nothing at all.
    """
    b = p.get("benchmark_return_pct")
    if not isinstance(b, (int, float)):
        return None
    exc = _excursion_pct(p)
    if not isinstance(exc, (int, float)) or abs(exc) < 1e-9:
        return None
    if (b > 0) != (exc > 0):
        return 0.0
    return round(abs(b) / abs(exc), 3)


def _verdict_attribution(p: dict) -> str:
    """market_wide | idiosyncratic | unknown. Pure.

    Roughly 38 open shadows once showed would_hit_stop true off ONE market-wide
    drawdown. Resolved naively they read as 38 independent pieces of evidence
    that the entry bar is set correctly — a beta detector wearing a skill
    detector's clothes. A verdict is only charged to the tape when the benchmark
    move clears BOTH gates: absolutely large (>= 2%) AND proportionally large
    (>= 50% of the shadow's own excursion). Either test alone produces a
    confident lie in one direction or the other.
    """
    if not isinstance(p.get("benchmark_return_pct"), (int, float)):
        return "unknown"
    frac = _benchmark_explained_fraction(p)
    if frac is None:
        return "unknown"
    if (abs(float(p["benchmark_return_pct"])) >= BENCH_MIN_ABS_MOVE_PCT
            and frac >= BENCH_MIN_EXPLAINED_FRACTION):
        return "market_wide"
    return "idiosyncratic"


def _shadow_stats(closed: list) -> dict:
    """Raw rates AND beta-stripped rates side by side. Pure.

    The raw good_skip rate answers "did skipping avoid a drawdown"; the
    idiosyncratic rate answers "did skipping avoid a drawdown the MARKET did not
    hand everyone" — which is the only one that says anything about the bar.
    """
    rows = [c for c in (closed or []) if isinstance(c, dict)]

    def _rate(pool, verdict):
        if not pool:
            return None
        return round(100 * sum(1 for c in pool if c.get("verdict") == verdict)
                     / len(pool), 1)

    idio = [c for c in rows if c.get("verdict_attribution") == "idiosyncratic"]
    return {
        "n_closed": len(rows),
        "regret_rate_pct": _rate(rows, "regret_miss"),
        "good_skip_rate_pct": _rate(rows, "good_skip"),
        "n_market_wide": sum(1 for c in rows
                             if c.get("verdict_attribution") == "market_wide"),
        "n_idiosyncratic": len(idio),
        "n_unknown_attribution": sum(1 for c in rows
                                     if c.get("verdict_attribution")
                                     not in ("market_wide", "idiosyncratic")),
        "idiosyncratic_regret_rate_pct": _rate(idio, "regret_miss"),
        "idiosyncratic_good_skip_rate_pct": _rate(idio, "good_skip"),
        "n_bar_verified": sum(1 for c in rows
                              if c.get("touch_source") == "daily_bars"),
        "note": ("idiosyncratic_* strip out shadows whose excursion the benchmark "
                 "already explains; those say nothing about where the entry bar is set."),
    }


def mark_shadows(prices: dict, bars: dict | None = None) -> dict:
    """Mark open shadows, resolve on FIRST TOUCH, expire at the horizon.

    THE BUG this replaces: every close was gated behind `age >= 30` — even when
    the stop was already through. A shadow that gapped past its stop on day two
    sat "open" for a month, so with `binding` needing 8 closed shadows the whole
    loop produced nothing for 30 days after any restart. Live state was
    49 open / 0 closed. First touch RESOLVES the counterfactual; the horizon is
    only the fallback for a shadow that never touched anything.

    `bars` ({TICKER: {date: {high, low, close}}}) overrides the fetch, so the
    caller — or a test — can supply real tape.
    """
    try:
        state = _load()
        today = _now()
        today_s = _today()
        supplied = {str(k).upper(): v for k, v in (bars or {}).items()}

        # --- pass 1: sampled marks (cheap, no network) --------------------- #
        marked = []
        for raw in state["positions"]:
            p = dict(raw)
            t = p.get("ticker")
            px = prices.get(t)
            if isinstance(px, (int, float)) and px > 0:
                entry = float(p["entry_price"])
                p["last_price"] = round(float(px), 4)
                p["last_mark_at"] = today_s
                p["high_water"] = round(max(float(p.get("high_water") or entry), float(px)), 4)
                p["low_water"] = round(min(float(p.get("low_water") or entry), float(px)), 4)
                p["unrealized_pct"] = round((float(px) / entry - 1) * 100, 2)
            opened = _parse_day(p.get("opened_at"))
            p["_age"] = (today.date() - opened.date()).days if opened else 0
            p["_opened_s"] = str(p.get("opened_at") or today_s)[:10]
            marked.append(p)

        # --- fetch real bars ONLY for the unproven side, batched ----------- #
        need = sorted({str(p.get("ticker")).upper() for p in marked
                       if _needs_bar_check(p, p["_age"])
                       and str(p.get("ticker")).upper() not in supplied})
        fetched: dict = {}
        if need or (marked and BENCHMARK_TICKER not in supplied):
            oldest = min((p["_opened_s"] for p in marked), default=today_s)
            # SPY rides along in the same batch so a closing shadow can be
            # attributed to the tape without a second network path.
            fetched = _fetch_bars_for(need + [BENCHMARK_TICKER], oldest, today_s)
        all_bars = {**(fetched or {}), **supplied}
        bench_bars = all_bars.get(BENCHMARK_TICKER)

        # --- pass 2: resolve ---------------------------------------------- #
        still_open, closed_now = [], []
        for p in marked:
            entry = float(p["entry_price"])
            age = p.pop("_age")
            opened_s = p.pop("_opened_s")
            t = str(p.get("ticker") or "").upper()

            ext = _bar_window_extremes(all_bars.get(t), opened_s, today_s)
            bar_verified = bool(ext.get("n_bars"))
            if bar_verified:
                # TRUE intraday extremes supersede sampled marks: sampling can
                # only understate an excursion.
                if ext.get("high") is not None:
                    p["high_water"] = round(max(float(p.get("high_water") or entry),
                                                float(ext["high"])), 4)
                if ext.get("low") is not None:
                    p["low_water"] = round(min(float(p.get("low_water") or entry),
                                               float(ext["low"])), 4)
            p["touch_source"] = "daily_bars" if bar_verified else "sampled_only"
            p["bars_seen"] = int(ext.get("n_bars") or 0)

            p["max_upside_pct"] = round((float(p["high_water"]) / entry - 1) * 100, 2)
            p["max_drawdown_pct"] = round((float(p["low_water"]) / entry - 1) * 100, 2)
            p["would_hit_target"] = float(p["high_water"]) >= float(p["target_price"])
            p["would_hit_stop"] = float(p["low_water"]) <= float(p["stop_price"])

            for w in MARK_WINDOWS:
                if age >= w and not any(m.get("window") == w
                                        for m in (p.get("marks") or [])):
                    p.setdefault("marks", []).append({
                        "window": w, "price": p.get("last_price"),
                        "pct": p.get("unrealized_pct"), "at": today_s,
                    })

            touched = bool(p["would_hit_target"] or p["would_hit_stop"])
            if not touched and age < HORIZON_DAYS:
                still_open.append(p)
                continue

            p["status"] = "closed"
            p["closed_at"] = today_s
            p["close_trigger"] = "level_touched" if touched else "horizon_expired"
            p["age_days_at_close"] = age
            p["verdict"] = _verdict(p)
            # Beta attribution: the counterfactual is only evidence about OUR bar
            # to the extent the tape did not do the work.
            p["benchmark_return_pct"] = _benchmark_window_return_pct(
                bench_bars, opened_s, today_s)
            p["benchmark_explained_fraction"] = _benchmark_explained_fraction(p)
            p["verdict_attribution"] = _verdict_attribution(p)
            p["lesson"] = _lesson(p)
            closed_now.append(p)

        state["positions"] = still_open
        if closed_now:
            state.setdefault("closed", []).extend(closed_now)
            state["closed"] = state["closed"][-200:]
        _save(state)
        return {"marked": len(still_open), "closed_now": len(closed_now),
                "bar_checked": len(need), "bars_available": len(all_bars)}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150]}


def _verdict(p: dict) -> str:
    hit_t = bool(p.get("would_hit_target"))
    hit_s = bool(p.get("would_hit_stop"))
    up = float(p.get("max_upside_pct") or 0)
    dd = float(p.get("max_drawdown_pct") or 0)
    if hit_t and not hit_s:
        return "regret_miss"  # would have worked
    if hit_s and not hit_t:
        return "good_skip"  # would have stopped out
    if hit_t and hit_s:
        # Both touched — path dependent; use which came with larger magnitude
        if abs(up) >= abs(dd):
            return "mixed_path_up_first"
        return "mixed_path_down_first"
    if up >= 8:
        return "regret_miss"
    if dd <= -8:
        return "good_skip"
    return "flat_expired"


def _lesson(p: dict) -> str:
    v = p.get("verdict")
    t = p.get("ticker")
    src = p.get("source")
    # Disclosures travel WITH the lesson: an unverified verdict and a verdict the
    # market already explains are both weaker evidence than they read as.
    suffix = ""
    if p.get("verdict_attribution") == "market_wide":
        suffix += (f" NOTE: the tape did this — SPY {p.get('benchmark_return_pct')}% over "
                   f"the same window; charge it to beta, not to the entry bar.")
    if p.get("touch_source") == "sampled_only":
        suffix += " (resolved on sampled marks only — not bar-verified.)"
    if v == "regret_miss":
        return (f"{t}: skipped ({src}) but would have reached +{p.get('max_upside_pct')}% "
                f"before stop — review bar / trigger discipline.{suffix}")
    if v == "good_skip":
        return (f"{t}: skip ({src}) looked right — price hit stop zone "
                f"({p.get('max_drawdown_pct')}%); process held.{suffix}")
    return f"{t}: shadow closed as {v} (src={src}).{suffix}"


def brain_facing_shadow_learning(limit: int = 12) -> dict:
    """Summary for context: open regrets forming + closed lessons."""
    try:
        state = _load()
        open_pos = list(state.get("positions") or [])
        closed = list(reversed(state.get("closed") or []))[:80]
        regrets = [c for c in closed if c.get("verdict") == "regret_miss"][:limit]
        good = [c for c in closed if c.get("verdict") == "good_skip"][:limit]
        # Open with strong unrealized upside = live regret risk
        open_running = sorted(
            [p for p in open_pos if isinstance(p.get("unrealized_pct"), (int, float))],
            key=lambda p: float(p.get("unrealized_pct") or 0),
            reverse=True,
        )[:8]
        n_closed = len(state.get("closed") or [])
        binding = n_closed >= 8  # enough shadows to treat as directional caution
        return {
            "note": (
                "SHADOW BOOK: ideas you skipped or watched but did not buy, marked "
                "forward. regret_miss = would have hit +10% target before stop — "
                "learn from false negatives. good_skip = would have hit stop — "
                "process worked. "
                + ("Binding caution: cite regrets when skipping similar setups."
                   if binding else
                   f"Warming up ({n_closed}/8 closed shadows) — anecdotal only.")
            ),
            "binding": binding,
            "n_open": len(open_pos),
            "n_closed": n_closed,
            "regret_misses": [
                {"ticker": r.get("ticker"), "source": r.get("source"),
                 "max_upside_pct": r.get("max_upside_pct"),
                 "reason": r.get("reason"), "lesson": r.get("lesson"),
                 "opened_at": r.get("opened_at"), "closed_at": r.get("closed_at")}
                for r in regrets
            ],
            "good_skips": [
                {"ticker": r.get("ticker"), "source": r.get("source"),
                 "max_drawdown_pct": r.get("max_drawdown_pct"),
                 "lesson": r.get("lesson"), "opened_at": r.get("opened_at"),
                 "verdict_attribution": r.get("verdict_attribution"),
                 "touch_source": r.get("touch_source")}
                for r in good
            ],
            "open_running_best": [
                {"ticker": p.get("ticker"), "unrealized_pct": p.get("unrealized_pct"),
                 "source": p.get("source"), "opened_at": p.get("opened_at"),
                 "reason": (p.get("reason") or "")[:120]}
                for p in open_running if float(p.get("unrealized_pct") or 0) > 5
            ],
            "stats": _shadow_stats(state.get("closed") or []),
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150],
                "regret_misses": [], "good_skips": [], "binding": False}


if __name__ == "__main__":
    print(json.dumps(brain_facing_shadow_learning(), indent=2))
