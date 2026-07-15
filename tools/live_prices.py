"""High-frequency live-price feed for holdings + watchlist.

The trading sandbox blocks Yahoo, so the cloud trader can only mark positions
and enforce stops from prices committed to the repo. The heavy full-bundle
gather (gather-data.yml) uses DAILY BARS and runs sparsely (GitHub throttles
scheduled crons hard), so between gathers a holding's price is frozen and an
intraday stop breach goes unseen — exactly how DELL could sit below its stop
without the safety layer reacting.

This is a lightweight feed for JUST the names that need a fresh mark — current
holdings + the published watchlist. A dedicated Action (live-prices.yml) fetches
INTRADAY quotes every few minutes during market hours and commits
state/live_prices.json. The orchestrator overlays these onto the bundle's
daily-bar prices before the safety layer enforces stops, and flags when the
freshest mark is too old to trust (fail-safe rather than silently trusting a
stale price).

Network is wrapped and degrades to the last file; the read / overlay / staleness
helpers are pure and offline-tested. GitHub's scheduler throttles */5 crons, so
real freshness is typically ~10-20 min, not 5 — a large improvement over the
multi-hour full-gather, but not a guarantee of per-minute prices.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
LIVE_FILE = ROOT / "state" / "live_prices.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Pure helpers (no network, offline-tested)
# --------------------------------------------------------------------------- #
def _parse_ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def live_age_minutes(blob: dict | None, now: datetime | None = None) -> float | None:
    """Minutes since the live snapshot was taken, or None if unknown. Pure."""
    now = now or _now_utc()
    as_of = _parse_ts((blob or {}).get("as_of"))
    if as_of is None:
        return None
    return (now - as_of).total_seconds() / 60.0


def overlay_live_prices(scan_prices: dict | None, live_blob: dict | None, *,
                        tickers: Iterable[str] | None = None,
                        max_age_min: float | None = 30.0,
                        now: datetime | None = None) -> tuple[dict, list[str]]:
    """Overlay the live feed onto bundle (daily-bar) prices. Pure.

    Returns (merged_prices, applied_tickers). The live snapshot is only applied
    when it is within max_age_min (None = always apply): a very stale feed (a
    broken Action) is worse than the daily bar, so we keep the bar and let the
    caller flag staleness. `tickers` restricts the overlay (default: every name
    in the feed).
    """
    merged = dict(scan_prices or {})
    live = (live_blob or {}).get("prices") or {}
    if not live:
        return merged, []
    if max_age_min is not None:
        age = live_age_minutes(live_blob, now)
        if age is None or age > max_age_min:
            return merged, []
    want = {str(t).upper() for t in tickers} if tickers is not None else None
    applied: list[str] = []
    for t, px in live.items():
        tu = str(t).upper()
        if want is not None and tu not in want:
            continue
        if isinstance(px, (int, float)) and px > 0:
            merged[tu] = round(float(px), 2)
            applied.append(tu)
    return merged, applied


def freshness_report(blob: dict | None, applied: list[str], *,
                     max_age_min: float, now: datetime | None = None) -> dict:
    """Structured freshness/staleness summary for the bundle + dashboard. Pure."""
    age = live_age_minutes(blob, now)
    return {
        "status": "ok" if applied else "no_live_overlay",
        "as_of": (blob or {}).get("as_of"),
        "source": (blob or {}).get("source"),
        "age_min": round(age, 1) if age is not None else None,
        "max_age_min": max_age_min,
        "stale": bool(age is None or age > max_age_min),
        "applied": sorted(applied),
        "note": (
            "Live intraday overlay for holdings + watchlist (state/live_prices.json). "
            "When stale=true the freshest available mark is older than max_age_min, so "
            "stop enforcement may lag the real price — treat marks as approximate and "
            "avoid time-sensitive entries."
        ),
    }


# --------------------------------------------------------------------------- #
# I/O + network
# --------------------------------------------------------------------------- #
LIVE_BRANCH = "live-data"


def pick_fresher(a: dict | None, b: dict | None) -> dict:
    """Return whichever snapshot has the newer as_of. Pure.

    A snapshot with an unparseable/absent as_of loses to one that has a real
    timestamp; two empties return {}.
    """
    a, b = a or {}, b or {}
    if not a.get("prices"):
        return b if b.get("prices") else a or b
    if not b.get("prices"):
        return a
    aa, ba = live_age_minutes(a), live_age_minutes(b)
    if aa is None:
        return b if ba is not None else a
    if ba is None:
        return a
    return a if aa <= ba else b


def _read_local() -> dict:
    try:
        blob = json.loads(LIVE_FILE.read_text())
        return blob if isinstance(blob, dict) else {}
    except Exception:
        return {}


def _read_from_branch(branch: str = LIVE_BRANCH) -> dict:
    """Read state/live_prices.json from origin/<branch> via git. Fail-soft.

    The feed publishes to the dedicated live-data branch (not main), so the
    cloud trader's main checkout has no local file — it reads the freshest
    snapshot from the branch here. Any git/parse failure returns {} so the
    overlay simply no-ops and stops fall back to the daily bar."""
    import subprocess
    try:
        subprocess.run(["git", "fetch", "--depth=1", "origin", branch],
                       cwd=str(ROOT), capture_output=True, timeout=30, check=False)
        r = subprocess.run(["git", "show", f"origin/{branch}:state/live_prices.json"],
                           cwd=str(ROOT), capture_output=True, timeout=15,
                           text=True, check=False)
        if r.returncode == 0 and r.stdout.strip():
            blob = json.loads(r.stdout)
            return blob if isinstance(blob, dict) else {}
    except Exception:
        pass
    return {}


def load_live_prices(*, use_branch: bool = True) -> dict:
    """Freshest live snapshot: the local file (self-gather / dev) merged with the
    live-data branch (the cloud trader's only source). Returns whichever is newer.
    Pass use_branch=False to skip the git read (tests / offline)."""
    local = _read_local()
    if not use_branch:
        return local
    return pick_fresher(local, _read_from_branch())


def fetch_live_prices(tickers: Iterable[str]) -> dict:
    """Intraday last price per ticker via yfinance (fast_info, then a 1m bar as
    fallback). Network wrapped; returns whatever it could get. Never raises."""
    out: dict = {}
    try:
        import yfinance as yf
    except Exception:
        return out
    for t in sorted({str(x).upper() for x in tickers if x}):
        px = None
        try:
            tk = yf.Ticker(t)
            try:
                fi = tk.fast_info
                px = fi.get("last_price") if hasattr(fi, "get") else getattr(fi, "last_price", None)
            except Exception:
                px = None
            if not px:
                hist = tk.history(period="1d", interval="1m")
                if hist is not None and len(hist):
                    px = float(hist["Close"].dropna().iloc[-1])
            if px and float(px) > 0:
                out[t] = round(float(px), 2)
        except Exception:
            continue
    return out


def write_live_prices(tickers: Iterable[str], *, now: datetime | None = None) -> dict:
    """Fetch live quotes for `tickers` and persist state/live_prices.json."""
    now = now or _now_utc()
    tickers = list(dict.fromkeys(str(t).upper() for t in tickers if t))
    prices = fetch_live_prices(tickers)
    payload = {
        "as_of": now.isoformat(),
        "source": "yfinance_intraday",
        "prices": prices,
        "requested": tickers,
        "n": len(prices),
    }
    try:
        LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LIVE_FILE.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass
    return payload


def book_tickers() -> list[str]:
    """Current holdings + published watchlist — the names the feed refreshes."""
    tickers: list[str] = []
    try:
        pf = json.loads((ROOT / "state" / "portfolio.json").read_text())
        tickers += [p.get("ticker") for p in pf.get("positions", []) if p.get("ticker")]
    except Exception:
        pass
    try:
        latest = json.loads((ROOT / "dashboard" / "data" / "latest.json").read_text())
        tickers += [w.get("ticker") for w in (latest.get("watchlist") or [])
                    if isinstance(w, dict) and w.get("ticker")]
    except Exception:
        pass
    return list(dict.fromkeys(str(t).upper() for t in tickers if t))


def refresh_from_book(*, now: datetime | None = None) -> dict:
    """Entry point for the live-prices Action: refresh holdings + watchlist."""
    tickers = book_tickers()
    if not tickers:
        return {"as_of": (now or _now_utc()).isoformat(), "source": "yfinance_intraday",
                "prices": {}, "requested": [], "n": 0, "note": "no holdings/watchlist"}
    return write_live_prices(tickers, now=now)
