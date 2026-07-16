"""Momentum-factor health gauge — detects factor UNWINDS, not market selloffs.

Why this exists: in July 2026 Goldman's momentum basket dropped ~18% in two
days while SPY barely moved. A long-momentum swing book gets hurt exactly when
"the market looks fine" but the FACTOR is being liquidated — crowded leaders
sold hard while the index holds up. The validator halves new-BUY risk when
`momentum_unwind` is true; this module's only job is producing that read
honestly and degrading gracefully when data is missing.

Two independent layers, combined worst-of:

1. INTERNAL (preferred, zero network) — from the scan rows this run already
   computed: rank names by 3-month relative strength vs SPY, take the top
   decile ("our momentum leaders"), and average their 1-MONTH relative
   strength. Names that led for 3 months but are now sharply LAGGING over
   1 month = the factor rolling over inside our own universe.
2. EXTERNAL (market-wide reference) — MTUM and SPMO (momentum-factor ETFs)
   daily closes vs SPY: a deep drawdown from the 20-day high that SPY did NOT
   share is the classic unwind footprint. Closes are cached in
   data/cache/momentum_etfs.json so blocked-network cloud runs reuse the last
   committed values (ignored once older than ~5 days — a stale factor read is
   worse than none).

"unwind" if EITHER layer says unwind; "softening" if either softens;
"healthy" if data was present and neither fired; "unknown" (fail-open,
momentum_unwind=False) only when NO layer had data at all.

All computation helpers are pure and offline-tested; all network is wrapped
and never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = ROOT / "data" / "cache" / "momentum_etfs.json"

# Internal layer (scan-row) thresholds: avg 1-month rel strength of the
# 3-month-leader decile, in percentage points vs SPY.
INTERNAL_UNWIND_AVG_1M_RS = -5.0
INTERNAL_SOFTENING_AVG_1M_RS = -2.0
MIN_INTERNAL_ROWS = 8            # fewer rows = no honest "decile"; skip layer
MIN_LEADERS = 3

# External layer (ETF) thresholds. dd = drawdown from the 20-day high (%);
# gap = ETF dd minus SPY dd (negative = ETF fell harder than the index).
ETF_UNWIND_DD = -8.0
ETF_UNWIND_GAP = -4.0            # ETF at least 4 pts deeper than SPY
ETF_SOFTENING_DD = -5.0
ETF_SOFTENING_GAP = -2.0
ETF_SYMBOLS = ("MTUM", "SPMO")
BENCH_SYMBOL = "SPY"
MIN_ETF_SESSIONS = 20            # need a full 20-day-high window
CACHE_MAX_AGE_DAYS = 5.0

_SEVERITY = {"healthy": 0, "softening": 1, "unwind": 2}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _num(v):
    """Return v as float if it is a usable finite number, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY.get(a, 0) >= _SEVERITY.get(b, 0) else b


# --------------------------------------------------------------------------- #
# Layer 1: internal (scan rows) — pure
# --------------------------------------------------------------------------- #
def _internal_layer(scan_rows) -> dict | None:
    """Health of OUR momentum leaders, from scan rows. Pure, no network.

    Rows need numeric rel_strength_3m_pct AND rel_strength_1m_pct. The top
    decile by 3-month RS (at least MIN_LEADERS names) are "the leaders"; their
    average 1-month RS decides the read. Returns None (layer has no say) when
    fewer than MIN_INTERNAL_ROWS usable rows exist.
    """
    if not scan_rows:
        return None
    usable = []
    seen = set()
    for r in scan_rows:
        if not isinstance(r, dict):
            continue
        rs3 = _num(r.get("rel_strength_3m_pct"))
        rs1 = _num(r.get("rel_strength_1m_pct"))
        if rs3 is None or rs1 is None:
            continue
        t = str(r.get("ticker") or "").upper()
        if t:
            if t in seen:
                continue
            seen.add(t)
        usable.append((t, rs3, rs1))
    n = len(usable)
    if n < MIN_INTERNAL_ROWS:
        return None
    usable.sort(key=lambda x: x[1], reverse=True)
    n_leaders = max(MIN_LEADERS, n // 10)
    leaders = usable[:n_leaders]
    # Round BEFORE comparing so the reported number and the status can never
    # disagree at a threshold boundary (float noise like -4.999999999).
    avg_1m = round(sum(x[2] for x in leaders) / len(leaders), 2)
    avg_3m = round(sum(x[1] for x in leaders) / len(leaders), 2)
    if avg_1m <= INTERNAL_UNWIND_AVG_1M_RS:
        status = "unwind"
    elif avg_1m <= INTERNAL_SOFTENING_AVG_1M_RS:
        status = "softening"
    else:
        status = "healthy"
    return {
        "status": status,
        "n_rows": n,
        "n_leaders": len(leaders),
        "leaders": [x[0] for x in leaders if x[0]],
        "avg_leader_1m_rs_pct": round(avg_1m, 2),
        "avg_leader_3m_rs_pct": round(avg_3m, 2),
        "thresholds": {"unwind": INTERNAL_UNWIND_AVG_1M_RS,
                       "softening": INTERNAL_SOFTENING_AVG_1M_RS},
    }


# --------------------------------------------------------------------------- #
# Layer 2: external (momentum ETFs vs SPY) — pure computation over closes
# --------------------------------------------------------------------------- #
def _clean_closes(v) -> list:
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        f = _num(x)
        if f is not None and f > 0:
            out.append(f)
    return out


def _dd_from_20d_high_pct(closes: list) -> float | None:
    """Last close vs the max of the trailing 20 sessions, in %. Negative = below."""
    if len(closes) < MIN_ETF_SESSIONS:
        return None
    window = closes[-MIN_ETF_SESSIONS:]
    hi = max(window)
    if hi <= 0:
        return None
    return (closes[-1] / hi - 1.0) * 100.0


def _ret_10d_pct(closes: list) -> float | None:
    if len(closes) < 11 or closes[-11] <= 0:
        return None
    return (closes[-1] / closes[-11] - 1.0) * 100.0


def _etf_layer(closes_by_symbol) -> dict | None:
    """Market-wide momentum-factor read from MTUM/SPMO vs SPY closes. Pure.

    closes_by_symbol: {"MTUM": [...], "SPMO": [...], "SPY": [...]} —
    chronological daily closes, oldest first. Uses the WORSE of MTUM/SPMO.
    Returns None when SPY or both ETFs lack a full 20-session window.
    """
    if not isinstance(closes_by_symbol, dict):
        return None
    spy = _clean_closes(closes_by_symbol.get(BENCH_SYMBOL))
    spy_dd = _dd_from_20d_high_pct(spy)
    spy_r10 = _ret_10d_pct(spy)
    if spy_dd is None or spy_r10 is None:
        return None  # no benchmark, no factor-vs-market comparison

    # Round BEFORE comparing so reported numbers and status can never disagree
    # at a threshold boundary (92/100 in floats is -7.999999999999996, which
    # would dodge an exact -8.0 unwind threshold).
    spy_dd, spy_r10 = round(spy_dd, 2), round(spy_r10, 2)
    etfs = {}
    worst = "healthy"
    for sym in ETF_SYMBOLS:
        c = _clean_closes(closes_by_symbol.get(sym))
        dd = _dd_from_20d_high_pct(c)
        r10 = _ret_10d_pct(c)
        if dd is None or r10 is None:
            continue
        dd, r10 = round(dd, 2), round(r10, 2)
        dd_gap = round(dd - spy_dd, 2)  # negative = ETF fell harder than SPY
        if dd <= ETF_UNWIND_DD and dd_gap <= ETF_UNWIND_GAP:
            status = "unwind"
        elif dd <= ETF_SOFTENING_DD and dd_gap <= ETF_SOFTENING_GAP:
            status = "softening"
        else:
            status = "healthy"
        worst = _worse(worst, status)
        etfs[sym] = {
            "status": status,
            "drawdown_20d_pct": dd,
            "dd_gap_vs_spy_pct": dd_gap,
            "ret_10d_pct": r10,
            "excess_ret_10d_vs_spy_pct": round(r10 - spy_r10, 2),
        }
    if not etfs:
        return None
    return {
        "status": worst,
        "etfs": etfs,
        "spy": {"drawdown_20d_pct": spy_dd, "ret_10d_pct": spy_r10},
        "thresholds": {"unwind": {"dd": ETF_UNWIND_DD, "gap_vs_spy": ETF_UNWIND_GAP},
                       "softening": {"dd": ETF_SOFTENING_DD, "gap_vs_spy": ETF_SOFTENING_GAP}},
    }


# --------------------------------------------------------------------------- #
# ETF closes: fetch + cache (network wrapped; cache pure-readable for tests)
# --------------------------------------------------------------------------- #
def _fetch_etf_closes() -> dict:
    """~40 recent daily closes for MTUM/SPMO/SPY via yfinance. Never raises;
    returns whatever it could get (possibly {})."""
    out: dict = {}
    try:
        import yfinance as yf
    except Exception:
        return out
    for sym in ETF_SYMBOLS + (BENCH_SYMBOL,):
        try:
            hist = yf.Ticker(sym).history(period="3mo", interval="1d")
            if hist is not None and len(hist):
                closes = [round(float(x), 4) for x in hist["Close"].dropna().tolist()]
                closes = [c for c in closes if c > 0]
                if closes:
                    out[sym] = closes[-45:]
        except Exception:
            continue
    return out


def _read_cache() -> dict:
    try:
        blob = json.loads(CACHE_FILE.read_text())
        return blob if isinstance(blob, dict) else {}
    except Exception:
        return {}


def _write_cache(closes_by_symbol: dict, now: datetime | None = None) -> None:
    try:
        payload = {"as_of": (now or _now_utc()).isoformat(),
                   "closes": closes_by_symbol}
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


def _cache_closes(blob: dict, max_age_days: float = CACHE_MAX_AGE_DAYS,
                  now: datetime | None = None) -> dict | None:
    """Closes from a cache blob if fresh enough, else None. Pure.

    A factor read off week-old ETF bars would claim to describe TODAY's tape;
    older than max_age_days the whole external layer is ignored instead.
    """
    if not isinstance(blob, dict):
        return None
    closes = blob.get("closes")
    if not isinstance(closes, dict) or not closes:
        return None
    try:
        as_of = datetime.fromisoformat(str(blob.get("as_of")))
    except (TypeError, ValueError):
        return None
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    age_days = ((now or _now_utc()) - as_of).total_seconds() / 86400.0
    if age_days > max_age_days:
        return None
    return closes


# --------------------------------------------------------------------------- #
# Combine
# --------------------------------------------------------------------------- #
def _combined_note(status: str, internal, external) -> str:
    frags = []
    if internal is not None:
        frags.append(
            "our 3m-RS leader decile averages {:+.1f}% 1m rel strength".format(
                internal["avg_leader_1m_rs_pct"]))
    if external is not None:
        worst_sym = None
        worst_row = None
        for sym, row in external["etfs"].items():
            if worst_row is None or row["drawdown_20d_pct"] < worst_row["drawdown_20d_pct"]:
                worst_sym, worst_row = sym, row
        if worst_row is not None:
            frags.append("{} is {:+.1f}% off its 20d high vs SPY {:+.1f}%".format(
                worst_sym, worst_row["drawdown_20d_pct"],
                external["spy"]["drawdown_20d_pct"]))
    detail = "; ".join(frags)
    if status == "unwind":
        return ("Momentum-factor UNWIND: recent leaders are being sold hard while the "
                "index holds up ({}) — new-BUY risk is halved; do not chase momentum "
                "entries this run.".format(detail))
    if status == "softening":
        return ("Momentum factor softening ({}) — leaders losing relative strength; "
                "prefer setups that do not depend on factor momentum continuing."
                .format(detail))
    if status == "healthy":
        return ("Momentum factor healthy ({}) — recent leaders still leading; no "
                "factor-unwind adjustment.".format(detail))
    return ("No momentum-factor data this run (no usable scan rows and no fresh "
            "momentum-ETF closes) — status unknown, fail-open, no adjustment.")


def get_momentum_health(scan_rows=None) -> dict:
    """Momentum-factor health for this run. Never raises; fail-open to unknown.

    scan_rows: optional list of scanner row dicts (rel_strength_1m_pct /
    rel_strength_3m_pct) — the zero-network internal layer. The external
    MTUM/SPMO-vs-SPY layer fetches via yfinance when the network allows and
    otherwise reuses data/cache/momentum_etfs.json (ignored past ~5 days).
    """
    now = _now_utc()
    try:
        internal = _internal_layer(scan_rows)
    except Exception:
        internal = None

    external = None
    etf_source = None
    try:
        fetched = _fetch_etf_closes()
        external = _etf_layer(fetched)
        if external is not None:
            etf_source = "live"
            _write_cache(fetched, now)
        else:
            cached = _cache_closes(_read_cache(), now=now)
            if cached:
                external = _etf_layer(cached)
                if external is not None:
                    etf_source = "cache"
    except Exception:
        external = None
    if external is not None:
        external["source"] = etf_source

    statuses = [layer["status"] for layer in (internal, external) if layer is not None]
    if not statuses:
        status = "unknown"
    elif "unwind" in statuses:
        status = "unwind"
    elif "softening" in statuses:
        status = "softening"
    else:
        status = "healthy"

    return {
        "status": status,
        "momentum_unwind": status == "unwind",
        "signals": {"internal_leaders": internal, "external_etfs": external},
        "as_of": now.isoformat(),
        "note": _combined_note(status, internal, external),
    }


if __name__ == "__main__":
    print(json.dumps(get_momentum_health(), indent=2))
