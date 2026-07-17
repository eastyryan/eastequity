"""High-frequency live-price feed — full universe via Alpaca, book via fallback.

The trading sandbox blocks Yahoo, so the cloud trader can only mark positions
and enforce stops from prices committed to the repo. The heavy full-bundle
gather (gather-data.yml) uses DAILY BARS and runs sparsely (GitHub throttles
scheduled crons hard), so between gathers a holding's price is frozen and an
intraday stop breach goes unseen — exactly how DELL could sit below its stop
without the safety layer reacting.

With Alpaca keys (ALPACA_API_KEY/SECRET), each tick refreshes the ENTIRE
universe from batched IEX snapshots (~2 requests for ~183 names, vs the free
tier's 200/min) — stops, watchlist triggers, and scan overlays all see a
≤5-min-old mark during market hours. Without keys it degrades to the original
serial-yfinance feed over just holdings + watchlist. A dedicated Action
(live-prices.yml) plus the local launchd feeder fetch every ~5 minutes during
market hours and commit state/live_prices.json to the live-data branch. The
orchestrator overlays these onto the bundle's daily-bar prices before the
safety layer enforces stops, and flags when the freshest mark is too old to
trust (fail-safe rather than silently trusting a stale price).

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


def fetch_live_prices(tickers: Iterable[str],
                      fallback_tickers: Iterable[str] | None = None) -> tuple[dict, str]:
    """Intraday last price per ticker. Returns ({TICKER: price}, source_label).

    Primary: Alpaca batched snapshots (IEX real-time; ~100 symbols/request, so
    the FULL universe costs 2 requests against a 200/min free-tier limit).
    Fallback: yfinance per-name — but yfinance is SERIAL and slow, so when
    Alpaca answered, only `fallback_tickers` (the stop-critical book) are
    retried through it; with no Alpaca at all, every name goes through
    yfinance exactly as before. Network wrapped; never raises."""
    want = list(dict.fromkeys(str(x).upper() for x in tickers if x))
    out: dict = {}
    source = "yfinance_intraday"
    try:
        from tools import alpaca_data
        if alpaca_data.has_keys():
            for t, rec in alpaca_data.get_prices(want).items():
                # latest trade / minute bar are intraday marks; a daily-bar
                # fallback is yesterday's close — keep it, it matches the old
                # behaviour of "freshest price yfinance would give us".
                out[t] = round(float(rec["price"]), 2)
            if out:
                source = "alpaca_iex"
    except Exception:
        pass

    missing = [t for t in want if t not in out]
    if out and fallback_tickers is not None:
        critical = {str(x).upper() for x in fallback_tickers}
        missing = [t for t in missing if t in critical]
    if missing:
        yf_prices = _fetch_yfinance(missing)
        if yf_prices:
            out.update(yf_prices)
            source = "alpaca_iex+yfinance" if source == "alpaca_iex" else source
    return out, source


def _fetch_yfinance(tickers: Iterable[str]) -> dict:
    """Original serial yfinance path (fast_info, then a 1m bar). Never raises."""
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


def write_live_prices(tickers: Iterable[str], *, now: datetime | None = None,
                      fallback_tickers: Iterable[str] | None = None) -> dict:
    """Fetch live quotes for `tickers` and persist state/live_prices.json."""
    now = now or _now_utc()
    tickers = list(dict.fromkeys(str(t).upper() for t in tickers if t))
    prices, source = fetch_live_prices(tickers, fallback_tickers=fallback_tickers)
    payload = {
        "as_of": now.isoformat(),
        "source": source,
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
    """Current holdings + published watchlist — the stop-critical names."""
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


def universe_tickers() -> list[str]:
    """Every name in data/universe.json (flattened sectors). Fail-soft []."""
    try:
        sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
        return sorted({str(t).upper() for names in sectors.values() for t in names})
    except Exception:
        return []


def refresh_from_book(*, now: datetime | None = None) -> dict:
    """Entry point for the feeders (Action + launchd tick).

    With Alpaca keys, one tick refreshes the ENTIRE universe (batched
    snapshots, ~2 requests) plus SPY — every stop, watchlist trigger, and
    scan overlay sees a ≤5-min-old mark all day. Without keys it degrades to
    the original behaviour: serial yfinance over just holdings + watchlist
    (the full universe would be far too slow serially)."""
    book = book_tickers()
    tickers = list(dict.fromkeys(book + ["SPY"] + universe_tickers()))
    try:
        from tools.alpaca_data import has_keys
        broad = has_keys()
    except Exception:
        broad = False
    if not broad:
        tickers = book
    if not tickers:
        return {"as_of": (now or _now_utc()).isoformat(), "source": "none",
                "prices": {}, "requested": [], "n": 0, "note": "no holdings/watchlist"}
    return write_live_prices(tickers, now=now, fallback_tickers=book)
