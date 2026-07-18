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


def mark_shadows(prices: dict) -> dict:
    """Mark all open shadows to current prices; expire past horizon; score."""
    try:
        state = _load()
        still_open = []
        closed_now = []
        today = _now()
        for p in state["positions"]:
            p = dict(p)
            t = p.get("ticker")
            px = prices.get(t)
            if isinstance(px, (int, float)) and px > 0:
                entry = float(p["entry_price"])
                p["last_price"] = round(float(px), 4)
                p["last_mark_at"] = _today()
                p["high_water"] = round(max(float(p.get("high_water") or entry), float(px)), 4)
                p["low_water"] = round(min(float(p.get("low_water") or entry), float(px)), 4)
                p["unrealized_pct"] = round((float(px) / entry - 1) * 100, 2)
                p["max_upside_pct"] = round((float(p["high_water"]) / entry - 1) * 100, 2)
                p["max_drawdown_pct"] = round((float(p["low_water"]) / entry - 1) * 100, 2)
                p["would_hit_target"] = float(p["high_water"]) >= float(p["target_price"])
                p["would_hit_stop"] = float(p["low_water"]) <= float(p["stop_price"])
                # window marks
                opened = _parse_day(p.get("opened_at"))
                if opened:
                    age = (today.date() - opened.date()).days
                    for w in MARK_WINDOWS:
                        if age >= w and not any(
                            m.get("window") == w for m in (p.get("marks") or [])
                        ):
                            p.setdefault("marks", []).append({
                                "window": w,
                                "price": p["last_price"],
                                "pct": p["unrealized_pct"],
                                "at": _today(),
                            })

            opened = _parse_day(p.get("opened_at"))
            age = (today.date() - opened.date()).days if opened else 0
            # Resolve verdict on horizon or stop/target first-touch score
            if age >= HORIZON_DAYS or p.get("would_hit_target") or p.get("would_hit_stop"):
                if age >= HORIZON_DAYS or (
                    p.get("would_hit_target") is not None
                    and (p.get("would_hit_target") or p.get("would_hit_stop") or age >= 30)
                ):
                    # Only close at 30d+ for target/stop resolution, or hard 90d
                    if age >= HORIZON_DAYS or age >= 30:
                        p["status"] = "closed"
                        p["closed_at"] = _today()
                        p["verdict"] = _verdict(p)
                        p["lesson"] = _lesson(p)
                        closed_now.append(p)
                        continue
            still_open.append(p)

        state["positions"] = still_open
        if closed_now:
            state.setdefault("closed", []).extend(closed_now)
            state["closed"] = state["closed"][-200:]
        _save(state)
        return {"marked": len(still_open), "closed_now": len(closed_now)}
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
    if v == "regret_miss":
        return (f"{t}: skipped ({src}) but would have reached +{p.get('max_upside_pct')}% "
                f"before stop — review bar / trigger discipline.")
    if v == "good_skip":
        return (f"{t}: skip ({src}) looked right — price hit stop zone "
                f"({p.get('max_drawdown_pct')}%); process held.")
    return f"{t}: shadow closed as {v} (src={src})."


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
                 "lesson": r.get("lesson"), "opened_at": r.get("opened_at")}
                for r in good
            ],
            "open_running_best": [
                {"ticker": p.get("ticker"), "unrealized_pct": p.get("unrealized_pct"),
                 "source": p.get("source"), "opened_at": p.get("opened_at"),
                 "reason": (p.get("reason") or "")[:120]}
                for p in open_running if float(p.get("unrealized_pct") or 0) > 5
            ],
            "stats": {
                "regret_rate_pct": round(
                    100 * len([c for c in (state.get("closed") or [])
                               if c.get("verdict") == "regret_miss"])
                    / max(1, n_closed), 1) if n_closed else None,
                "good_skip_rate_pct": round(
                    100 * len([c for c in (state.get("closed") or [])
                               if c.get("verdict") == "good_skip"])
                    / max(1, n_closed), 1) if n_closed else None,
            },
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150],
                "regret_misses": [], "good_skips": [], "binding": False}


if __name__ == "__main__":
    print(json.dumps(brain_facing_shadow_learning(), indent=2))
