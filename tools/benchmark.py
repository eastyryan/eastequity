"""Benchmark attribution — is this book beating SPY, or is it long AI beta?

WHY THIS EXISTS

The entire performance surface was ABSOLUTE P&L: closed_trades, win_rate_pct,
realized_pnl_usd, avg_r_multiple, max_drawdown_pct. No Sharpe, no alpha, no
excess-vs-SPY, no beta. For a long-only book in an AI/semis universe during a bull
tape, absolute numbers are dominated by beta — and they were not merely cosmetic:
tools/calibration_gate.py flags a sector as "losing" on absolute win rate and then
demands an exception paragraph to trade it. In a rising market no bucket ever trips;
in a drawdown every bucket trips at once. It was a beta detector wearing a skill
detector's clothes.

WHAT THIS ADDS

  per trade  : the benchmark's return over the SAME holding window, and the
               excess. A +8% trade while SPY did +9% is a LOSS of alpha.
  book level : a daily return series -> Sharpe, Sortino, beta vs SPY, and
               beta-adjusted excess (Jensen's alpha).

HONESTY CONSTRAINTS, each deliberate

* Matched-window returns are computable RETROACTIVELY — closed trades already carry
  opened_at and closed_at — so this works on history, not just going forward.
* Dates are ET day-granular after to_et_date(), so a same-day round trip has no
  measurable benchmark window; those report None rather than 0.
* dashboard/data/equity_history.json contains WEEKEND rows and sometimes omits
  benchmark_close entirely (absent key, not null). Both are filtered, or the
  return series silently gains flat days that deflate volatility and inflate Sharpe.
* Sharpe/Sortino on a handful of daily observations are noise. Every ratio is
  returned with its sample size, and None below MIN_OBS, so the dashboard cannot
  quote a confident-looking number off two weeks of data.
* Free data only: SPY daily closes via yfinance, cached to data/cache/.

CLI: python -m tools.benchmark
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = ROOT / "data" / "cache" / "benchmark_closes.json"

BENCHMARK = "SPY"
TRADING_DAYS = 252
# Below this, a ratio is a number without information. Roughly a quarter of daily
# observations — still thin, but past the point of pure noise.
MIN_OBS = 60


# --------------------------------------------------------------------------- #
# Pure math (offline-testable, no network, no IO)
# --------------------------------------------------------------------------- #
def pct_return(start: float | None, end: float | None) -> float | None:
    """Simple return, None when either endpoint is unusable. Pure."""
    if start is None or end is None:
        return None
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        return None
    if start <= 0:
        return None
    return end / start - 1


def daily_returns(equity_hist: list[dict]) -> list[tuple[str, float]]:
    """[(date, return)] from an equity series, skipping non-trading and flat-stub rows.

    equity_history.json carries WEEKEND rows (equity unchanged) and rows whose
    benchmark_close key is absent. Including them adds artificial zero-return days,
    which deflates volatility and inflates every ratio computed from it. Pure.
    """
    # FILTER FIRST, then pair. Pairing first and discarding bad pairs afterwards
    # silently measures a Monday against a Saturday stub and DROPS the observation
    # entirely; consecutive TRADING rows are the actual observations.
    rows = [h for h in (equity_hist or [])
            if isinstance(h.get("equity"), (int, float)) and h.get("equity")
            and h.get("date") and h.get("benchmark_close") is not None]
    rows.sort(key=lambda h: h["date"])
    out = []
    for prev, cur in zip(rows, rows[1:]):
        r = pct_return(prev["equity"], cur["equity"])
        if r is not None:
            out.append((cur["date"], r))
    return out


def benchmark_daily_returns(equity_hist: list[dict]) -> list[tuple[str, float]]:
    """Same calendar as daily_returns(), but for the benchmark column. Pure."""
    rows = [h for h in (equity_hist or [])
            if h.get("date") and h.get("benchmark_close") is not None
            and isinstance(h.get("equity"), (int, float)) and h.get("equity")]
    rows.sort(key=lambda h: h["date"])
    out = []
    for prev, cur in zip(rows, rows[1:]):
        r = pct_return(prev["benchmark_close"], cur["benchmark_close"])
        if r is not None:
            out.append((cur["date"], r))
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(returns, rf_annual: float = 0.0):
    """Annualized Sharpe from daily returns. None below MIN_OBS — a ratio computed
    on a fortnight is a number without information. Pure."""
    rs = [r for _, r in returns] if returns and isinstance(returns[0], tuple) else list(returns or [])
    if len(rs) < MIN_OBS:
        return None
    sd = _stdev(rs)
    if not sd:
        return None
    excess = _mean(rs) - rf_annual / TRADING_DAYS
    return round(excess / sd * math.sqrt(TRADING_DAYS), 2)


def sortino(returns, rf_annual: float = 0.0):
    """Annualized Sortino: penalizes DOWNSIDE deviation only. Pure."""
    rs = [r for _, r in returns] if returns and isinstance(returns[0], tuple) else list(returns or [])
    if len(rs) < MIN_OBS:
        return None
    target = rf_annual / TRADING_DAYS
    downside = [min(0.0, r - target) for r in rs]
    dd = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
    if not dd:
        return None
    return round((_mean(rs) - target) / dd * math.sqrt(TRADING_DAYS), 2)


def beta(book_returns, bench_returns):
    """Book beta vs the benchmark over their COMMON dates. Pure.

    Aligning on dates matters: the two series can differ in length whenever a row
    lacks a benchmark_close, and zipping unaligned series silently pairs a Monday
    book return with a Tuesday benchmark return.
    """
    b = dict(book_returns or [])
    m = dict(bench_returns or [])
    common = sorted(set(b) & set(m))
    if len(common) < MIN_OBS:
        return None
    bx = [b[d] for d in common]
    mx = [m[d] for d in common]
    mm = _mean(mx)
    var = sum((x - mm) ** 2 for x in mx) / (len(mx) - 1)
    if not var:
        return None
    bmean = _mean(bx)
    cov = sum((bx[i] - bmean) * (mx[i] - mm) for i in range(len(mx))) / (len(mx) - 1)
    return round(cov / var, 2)


def jensens_alpha(book_returns, bench_returns, rf_annual: float = 0.0):
    """Annualized beta-adjusted excess return: the number that says whether the
    strategy earned anything the benchmark did not hand it. Pure."""
    bta = beta(book_returns, bench_returns)
    if bta is None:
        return None
    b = dict(book_returns or [])
    m = dict(bench_returns or [])
    common = sorted(set(b) & set(m))
    rf_daily = rf_annual / TRADING_DAYS
    rb, rm = _mean([b[d] for d in common]), _mean([m[d] for d in common])
    if rb is None or rm is None:
        return None
    return round((rb - rf_daily - bta * (rm - rf_daily)) * TRADING_DAYS * 100, 2)


# --------------------------------------------------------------------------- #
# Benchmark price history (network, cached, fail-soft)
# --------------------------------------------------------------------------- #
def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(blob: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(blob, indent=1, sort_keys=True))
    except Exception:
        pass


def benchmark_closes(start: str, end: str, ticker: str = BENCHMARK) -> dict:
    """{YYYY-MM-DD: close} covering [start, end]. Cached; fail-soft to {}.

    Cache is keyed per ticker and merged, so a widening window costs one fetch.
    """
    cache = _load_cache()
    have = cache.get(ticker) or {}
    need = [d for d in (start, end) if d and d not in have]
    if not need and have:
        return have
    try:
        import yfinance as yf
        s = (datetime.fromisoformat(start) - timedelta(days=10)).date().isoformat()
        e = (datetime.fromisoformat(end) + timedelta(days=5)).date().isoformat()
        hist = yf.Ticker(ticker).history(start=s, end=e, interval="1d",
                                         auto_adjust=True)
        fetched = {str(idx)[:10]: round(float(row["Close"]), 4)
                   for idx, row in hist.iterrows()}
        if fetched:
            have = {**have, **fetched}
            cache[ticker] = have
            _save_cache(cache)
    except Exception:
        pass
    return have


def _nearest_on_or_before(closes: dict, day: str):
    """Last close at or before `day` — a trade can open on a market holiday."""
    if not closes or not day:
        return None
    keys = sorted(k for k in closes if k <= day)
    return closes[keys[-1]] if keys else None


def _nearest_on_or_after(closes: dict, day: str):
    if not closes or not day:
        return None
    keys = sorted(k for k in closes if k >= day)
    return closes[keys[0]] if keys else None


def matched_window_return(opened_at: str, closed_at: str,
                          closes: dict | None = None,
                          ticker: str = BENCHMARK):
    """Benchmark return over exactly this trade's holding window. None when
    unknowable (missing dates, no data, or a same-day round trip which has no
    measurable daily window). Pass `closes` to avoid refetching per trade."""
    if not opened_at or not closed_at or opened_at == closed_at:
        return None
    if closes is None:
        closes = benchmark_closes(opened_at, closed_at, ticker)
    start = _nearest_on_or_before(closes, opened_at)
    end = _nearest_on_or_before(closes, closed_at)
    if start is None:
        start = _nearest_on_or_after(closes, opened_at)
    return pct_return(start, end)


def attach_benchmark_to_closed(closed: list[dict], ticker: str = BENCHMARK) -> list[dict]:
    """Stamp benchmark_return_pct and excess_return_pct on each closed trade.

    Trade return is measured on ENTRY PRICE, matching how the benchmark window is
    measured, so the two are comparable. Mutates and returns `closed`. Fail-soft:
    unknowable windows leave the fields None rather than 0, because 0 would read as
    "matched the benchmark".
    """
    rows = [t for t in (closed or []) if t.get("opened_at") and t.get("closed_at")]
    if not rows:
        return closed
    days = [t["opened_at"] for t in rows] + [t["closed_at"] for t in rows]
    closes = benchmark_closes(min(days), max(days), ticker)

    for t in (closed or []):
        bench = matched_window_return(t.get("opened_at"), t.get("closed_at"), closes)
        t["benchmark_return_pct"] = round(bench * 100, 2) if bench is not None else None
        own = pct_return(t.get("entry_price"), t.get("exit_price"))
        t["trade_return_pct"] = round(own * 100, 2) if own is not None else None
        t["excess_return_pct"] = (round((own - bench) * 100, 2)
                                  if own is not None and bench is not None else None)
    return closed


# --------------------------------------------------------------------------- #
# Daily OHLC bars — TRUE intraday extremes, for anything that needs to know
# whether a level was TOUCHED rather than merely sampled.
#
# WHY THIS LIVES HERE: this module already owns the cached, fail-soft price
# history path (benchmark_closes above). The shadow book's would_hit_stop /
# would_hit_target flags were accumulated from whatever last price happened to
# be passed at mark time, and sampling can only MISS an excursion, never invent
# one — so those flags were a systematic UNDER-count of touches. Real daily
# highs/lows fix the direction of that error.
#
# The asymmetry that keeps this cheap: a sampled touch is PROOF and needs no
# bars; a sampled non-touch is merely UNPROVEN. So callers should fetch bars
# only for the unproven side, in one batch.
# --------------------------------------------------------------------------- #
OHLC_CACHE_FILE = ROOT / "data" / "cache" / "daily_ohlc.json"


def _load_ohlc_cache() -> dict:
    try:
        blob = json.loads(OHLC_CACHE_FILE.read_text())
        return blob if isinstance(blob, dict) else {}
    except Exception:
        return {}


def _save_ohlc_cache(blob: dict) -> None:
    try:
        OHLC_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        OHLC_CACHE_FILE.write_text(json.dumps(blob, indent=0, sort_keys=True))
    except Exception:
        pass


def window_extremes(bars: dict | None, start: str, end: str) -> dict:
    """{"high", "low", "n_bars"} over the INCLUSIVE [start, end] date window.

    high/low are None — never 0 — when no bar covers the window. A 0 low reads
    as "the stop was touched" to every caller downstream, which would turn
    missing data into manufactured evidence. n_bars travels with the answer so
    a caller can tell "looked and found nothing" from "did not look". Pure.
    """
    lo_bound = str(start or "")[:10]
    hi_bound = str(end or "")[:10]
    highs: list[float] = []
    lows: list[float] = []
    n = 0
    for day, bar in (bars or {}).items():
        d = str(day)[:10]
        if lo_bound and d < lo_bound:
            continue
        if hi_bound and d > hi_bound:
            continue
        if not isinstance(bar, dict):
            continue
        got = False
        try:
            if bar.get("high") is not None:
                highs.append(float(bar["high"]))
                got = True
            if bar.get("low") is not None:
                lows.append(float(bar["low"]))
                got = True
        except (TypeError, ValueError):
            continue
        if got:
            n += 1
    return {"high": max(highs) if highs else None,
            "low": min(lows) if lows else None,
            "n_bars": n}


def touched_levels(ticker: str, start: str, end: str, *,
                   stop: float | None = None, target: float | None = None,
                   bars: dict | None = None) -> dict:
    """Was `stop` or `target` touched between start and end, on REAL daily bars?

    hit_stop / hit_target are None when no bar covers the window — "we could not
    look", which is a different claim from "we looked and it did not happen".
    Returning False there would let missing history masquerade as a clean path.
    `source` discloses which it was. Pass `bars` to avoid a fetch (an empty dict
    means "no bars available", NOT "go fetch some").
    """
    t = str(ticker or "").upper()
    if bars is None:
        bars = daily_ohlc(t, start, end)
    ext = window_extremes(bars, start, end)
    if not ext["n_bars"]:
        return {"ticker": t, "hit_stop": None, "hit_target": None,
                "high": None, "low": None, "n_bars": 0,
                "source": "unavailable",
                "note": "no daily bars cover this window — unknown, not False"}
    hit_stop = None
    hit_target = None
    try:
        if stop is not None and ext["low"] is not None:
            hit_stop = float(ext["low"]) <= float(stop)
        if target is not None and ext["high"] is not None:
            hit_target = float(ext["high"]) >= float(target)
    except (TypeError, ValueError):
        pass
    return {"ticker": t, "hit_stop": hit_stop, "hit_target": hit_target,
            "high": ext["high"], "low": ext["low"], "n_bars": ext["n_bars"],
            "source": "daily_bars"}


def daily_ohlc(ticker: str, start: str, end: str) -> dict:
    """{YYYY-MM-DD: {high, low, close}} for one ticker. Cached; fail-soft to {}."""
    return daily_ohlc_batch([ticker], start, end).get(str(ticker or "").upper(), {})


def daily_ohlc_batch(tickers, start: str, end: str) -> dict:
    """{TICKER: {YYYY-MM-DD: {high, low, close}}} for many tickers in ONE call.

    Batched because the caller (the shadow book) has dozens of unproven shadows
    at a time; per-shadow fetching is what made real-bar verification look
    expensive enough to skip. Cached per ticker with its covered window, so a
    repeat mark inside the same window costs nothing. Fail-soft: a blocked feed
    yields {} for that name and every downstream flag becomes None, not False.
    """
    want = sorted({str(t or "").upper() for t in (tickers or []) if str(t or "").strip()})
    if not want or not start or not end:
        return {}
    s10, e10 = str(start)[:10], str(end)[:10]
    cache = _load_ohlc_cache()
    out: dict = {}
    missing: list[str] = []
    for t in want:
        blob = cache.get(t)
        if (isinstance(blob, dict) and isinstance(blob.get("bars"), dict)
                and str(blob.get("from") or "9999") <= s10
                and str(blob.get("to") or "0000") >= e10):
            out[t] = blob["bars"]
        else:
            missing.append(t)
    if not missing:
        return out

    try:
        import yfinance as yf
    except Exception:
        return out
    # Pad the window: a shadow can open on a market holiday, and the caller's
    # `end` is usually today (whose bar may not have printed yet).
    try:
        fetch_from = (datetime.fromisoformat(s10) - timedelta(days=7)).date().isoformat()
        fetch_to = (datetime.fromisoformat(e10) + timedelta(days=2)).date().isoformat()
    except Exception:
        fetch_from, fetch_to = s10, e10
    for t in missing:
        try:
            hist = yf.Ticker(t).history(start=fetch_from, end=fetch_to,
                                        interval="1d", auto_adjust=True)
            bars = {}
            for idx, row in hist.iterrows():
                try:
                    bars[str(idx)[:10]] = {
                        "high": round(float(row["High"]), 4),
                        "low": round(float(row["Low"]), 4),
                        "close": round(float(row["Close"]), 4),
                    }
                except (TypeError, ValueError, KeyError):
                    continue
            if bars:
                prev = cache.get(t) if isinstance(cache.get(t), dict) else {}
                merged = {**(prev.get("bars") or {}), **bars}
                cache[t] = {
                    "bars": merged,
                    "from": min(str(prev.get("from") or fetch_from), fetch_from),
                    "to": max(str(prev.get("to") or fetch_to), e10),
                }
                out[t] = merged
        except Exception:
            continue
    _save_ohlc_cache(cache)
    return out


def book_risk_metrics(equity_hist: list[dict], rf_annual: float = 0.0) -> dict:
    """Sharpe / Sortino / beta / Jensen's alpha, each with its sample size.

    n_observations travels WITH the numbers: "Sharpe 2.1" off 14 days and off 3
    years are different claims and must not render identically.
    """
    book = daily_returns(equity_hist)
    bench = benchmark_daily_returns(equity_hist)
    common = len(set(dict(book)) & set(dict(bench)))
    return {
        "n_observations": len(book),
        "n_paired_observations": common,
        "min_observations_for_ratios": MIN_OBS,
        "sufficient_sample": len(book) >= MIN_OBS,
        "sharpe": sharpe(book, rf_annual),
        "sortino": sortino(book, rf_annual),
        "beta_vs_benchmark": beta(book, bench),
        "jensens_alpha_annual_pct": jensens_alpha(book, bench, rf_annual),
        "benchmark": BENCHMARK,
        "note": ("Ratios are None until n_observations >= "
                 f"{MIN_OBS}: computed on fewer days they are noise, not signal."),
    }


def summarize_excess(closed: list[dict]) -> dict:
    """Per-trade attribution rollup. The headline is beat_benchmark_rate_pct, NOT
    win rate — a 60% win rate in a tape that rose every week is not skill."""
    scored = [t for t in (closed or []) if t.get("excess_return_pct") is not None]
    if not scored:
        return {"n_scored": 0, "note": "no closed trade has a measurable benchmark window"}
    ex = [t["excess_return_pct"] for t in scored]
    beat = [x for x in ex if x > 0]
    return {
        "n_scored": len(scored),
        "n_unscored": len(closed or []) - len(scored),
        "mean_excess_return_pct": round(sum(ex) / len(ex), 2),
        "median_excess_return_pct": round(sorted(ex)[len(ex) // 2], 2),
        "beat_benchmark_rate_pct": round(len(beat) / len(scored) * 100, 1),
        "total_excess_return_pct": round(sum(ex), 2),
        "benchmark": BENCHMARK,
    }


if __name__ == "__main__":
    hist_file = ROOT / "dashboard" / "data" / "equity_history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.is_file() else []
    print(json.dumps(book_risk_metrics(hist), indent=1, default=str))
