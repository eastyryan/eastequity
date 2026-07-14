"""Portfolio-level correlation / concentration awareness.

Quantifies the "shared left tail" problem: a book of names that all move
together is one bet wearing several tickers. Pure list-math helpers (no
numpy/pandas — offline-testable) feed a pure assembler
analyze_portfolio_risk(); get_portfolio_risk() is the only network touch,
one batched yfinance download, fail-soft.

CLI:  python -m tools.correlation
    prints the risk read for the current state/portfolio.json (no candidates).
"""

from __future__ import annotations

MIN_OVERLAP = 10          # fewer overlapping return points than this -> None
SHARED_TAIL_CORR = 0.7    # every pairwise holding corr above this -> shared left tail
DIVERSIFIER_CORR = 0.3    # candidate corr_to_book below this -> diversifier


# ---------------------------------------------------------------- pure math

def _f(x):
    """Coerce to float; None on failure/NaN."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN guard


def _r2(x):
    return round(x, 2) if x is not None else None


def _daily_returns(closes):
    """Simple day-over-day returns from a close series (bad points skipped)."""
    rets = []
    prev = None
    for c in closes or []:
        v = _f(c)
        if v is None or v <= 0:
            continue
        if prev is not None:
            rets.append(v / prev - 1.0)
        prev = v
    return rets


def _aligned_pairs(xs, ys):
    """Align two series on min length from the END, drop non-numeric pairs."""
    xs, ys = list(xs or []), list(ys or [])
    n = min(len(xs), len(ys))
    if n <= 0:
        return []
    pairs = [(_f(a), _f(b)) for a, b in zip(xs[-n:], ys[-n:])]
    return [(a, b) for a, b in pairs if a is not None and b is not None]


def _corr(xs, ys):
    """Pearson correlation. None if <MIN_OVERLAP overlapping points or zero
    variance in either series."""
    pairs = _aligned_pairs(xs, ys)
    n = len(pairs)
    if n < MIN_OVERLAP:
        return None
    # Shift by the first point before computing moments: correlation is
    # shift-invariant, and a constant series then yields an EXACT 0.0
    # variance (the unshifted mean accumulates float error, so a constant
    # series would otherwise slip past the zero-variance guard).
    x0, y0 = pairs[0]
    sx = [a - x0 for a, _ in pairs]
    sy = [b - y0 for _, b in pairs]
    mx = sum(sx) / n
    my = sum(sy) / n
    sxx = sum((a - mx) ** 2 for a in sx)
    syy = sum((b - my) ** 2 for b in sy)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(sx, sy))
    return sxy / (sxx * syy) ** 0.5


def _beta(asset_returns, bench_returns):
    """cov(asset, bench) / var(bench). None on insufficient overlap or a
    zero-variance benchmark."""
    pairs = _aligned_pairs(asset_returns, bench_returns)
    n = len(pairs)
    if n < MIN_OVERLAP:
        return None
    # Same first-point shift as _corr: beta is shift-invariant and a constant
    # benchmark must yield an exact 0.0 variance, not float noise.
    a0, b0 = pairs[0]
    sa = [a - a0 for a, _ in pairs]
    sb = [b - b0 for _, b in pairs]
    ma = sum(sa) / n
    mb = sum(sb) / n
    var_b = sum((b - mb) ** 2 for b in sb)
    if var_b <= 0.0:
        return None
    cov = sum((a - ma) * (b - mb) for a, b in zip(sa, sb))
    return cov / var_b


def _book_returns(returns_by_ticker, weights):
    """Weighted portfolio return series. weights = market-value dollars per
    ticker (normalized here over the tickers that actually have return data).
    Series are aligned on the min length from the END of each series."""
    usable = {}
    for t, rets in (returns_by_ticker or {}).items():
        w = _f((weights or {}).get(t))
        if rets and w is not None and w > 0:
            usable[t] = (list(rets), w)
    if not usable:
        return []
    n = min(len(r) for r, _ in usable.values())
    if n <= 0:
        return []
    total_w = sum(w for _, w in usable.values())
    out = [0.0] * n
    for rets, w in usable.values():
        share = w / total_w
        tail = rets[-n:]
        for i in range(n):
            v = _f(tail[i])
            out[i] += share * (v if v is not None else 0.0)
    return out


# ------------------------------------------------------------ pure assembler

def analyze_portfolio_risk(returns_by_ticker, weights_by_ticker,
                           benchmark_returns, candidates=None,
                           benchmark="SPY") -> dict:
    """Assemble the full risk dict from precomputed return series (pure —
    tests exercise this offline; get_portfolio_risk just fetches and calls)."""
    returns_by_ticker = returns_by_ticker or {}
    weights = {t: _f(w) for t, w in (weights_by_ticker or {}).items()}
    weights = {t: w for t, w in weights.items() if w is not None and w > 0}
    bench_rets = list(benchmark_returns or [])
    candidates = [str(c).upper() for c in (candidates or []) if c]
    holding_tickers = sorted(weights)

    # actual observation window
    if bench_rets:
        window_days = len(bench_rets)
    else:
        lens = [len(returns_by_ticker.get(t) or [])
                for t in holding_tickers + candidates]
        window_days = max(lens) if lens else 0

    # pairwise holding correlations (sorted "A|B" keys)
    pairwise, raw_pair_vals = {}, []
    for i, a in enumerate(holding_tickers):
        for b in holding_tickers[i + 1:]:
            c = _corr(returns_by_ticker.get(a) or [], returns_by_ticker.get(b) or [])
            pairwise[f"{a}|{b}"] = _r2(c)
            raw_pair_vals.append(c)

    known = [v for v in raw_pair_vals if v is not None]
    avg_pair = sum(known) / len(known) if known else None
    shared = (len(holding_tickers) >= 2 and bool(raw_pair_vals)
              and all(v is not None and v > SHARED_TAIL_CORR for v in raw_pair_vals))

    if shared:
        note = (f"Every pair of holdings is correlated above {SHARED_TAIL_CORR}, "
                "so one equity-market selloff would hit the entire book at once "
                "(a shared left tail).")
    elif len(holding_tickers) >= 2:
        note = (f"Not every holding pair is correlated above {SHARED_TAIL_CORR}, "
                "so the book is not a single shared-left-tail bet.")
    elif len(holding_tickers) == 1:
        note = ("Single-position book: no pairwise correlation to share a left "
                "tail, but the position still carries its full market beta.")
    else:
        note = "No holdings with usable price history to assess correlation."

    holdings_out = {}
    for t in holding_tickers:
        rets = returns_by_ticker.get(t) or []
        holdings_out[t] = {
            "beta": _r2(_beta(rets, bench_rets)),
            "corr_to_benchmark": _r2(_corr(rets, bench_rets)),
        }

    book = _book_returns({t: returns_by_ticker.get(t) or []
                          for t in holding_tickers}, weights)
    portfolio_beta = _beta(book, bench_rets)

    candidates_out = {}
    for c in candidates:
        c_rets = returns_by_ticker.get(c) or []
        # book EXCLUDING the candidate itself (matters when adding to a holding)
        ex = {t: returns_by_ticker.get(t) or [] for t in holding_tickers if t != c}
        ex_w = {t: weights[t] for t in ex}
        corr_book = _corr(c_rets, _book_returns(ex, ex_w))
        candidates_out[c] = {
            "corr_to_book": _r2(corr_book),
            "beta": _r2(_beta(c_rets, bench_rets)),
            "diversifier": corr_book is not None and corr_book < DIVERSIFIER_CORR,
        }

    return {
        "status": "ok",
        "window_days": window_days,
        "benchmark": str(benchmark or "SPY").upper(),
        "portfolio_beta": _r2(portfolio_beta),
        "avg_pairwise_holding_corr": _r2(avg_pair),
        "shared_left_tail": shared,
        "note": note,
        "pairwise": pairwise,
        "holdings": holdings_out,
        "candidates": candidates_out,
    }


# ------------------------------------------------------------- fetch wrapper

def _closes_from_download(data, symbol):
    """Extract a clean close list for one symbol from a batched yf frame
    (handles both MultiIndex and flat single-symbol columns). Fail-soft []."""
    try:
        levels = getattr(getattr(data, "columns", None), "levels", None)
        df = data[symbol] if (levels is not None and symbol in levels[0]) else data
        return [float(x) for x in df["Close"].dropna().tolist()]
    except Exception:
        return []


def get_portfolio_risk(holdings, candidates=None, benchmark="SPY", days=90) -> dict:
    """Correlation/beta read for the current book plus optional candidates.

    holdings:  list of {"ticker": ..., "market_value_usd": ...}
    candidates: list of tickers (may be empty/None)
    One batched yfinance download (~6mo daily), last `days` returns used.
    """
    try:
        bench = str(benchmark or "SPY").upper()
        cands = [str(c).upper() for c in (candidates or []) if c]
        weights: dict = {}
        for h in holdings or []:
            t = str((h or {}).get("ticker") or "").upper()
            mv = _f((h or {}).get("market_value_usd"))
            if t and mv is not None and mv > 0:
                weights[t] = weights.get(t, 0.0) + mv

        symbols = sorted(set(list(weights) + cands + [bench]))
        import yfinance as yf
        data = yf.download(symbols, period="6mo", interval="1d",
                           group_by="ticker", auto_adjust=True,
                           progress=False, threads=True)

        n = max(int(days), 1) if days else 90
        returns_by_ticker = {}
        for s in symbols:
            rets = _daily_returns(_closes_from_download(data, s))
            if rets:
                returns_by_ticker[s] = rets[-n:]

        return analyze_portfolio_risk(
            returns_by_ticker, weights, returns_by_ticker.get(bench, []),
            cands, benchmark=bench)
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def _main():
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "state" / "portfolio.json"
    try:
        portfolio = json.loads(path.read_text())
    except Exception as e:
        print(json.dumps({"status": "error",
                          "reason": f"portfolio read failed: {e}"}))
        return
    holdings = [{"ticker": p.get("ticker"),
                 "market_value_usd": p.get("market_value_usd")}
                for p in portfolio.get("positions", []) or []]
    print(json.dumps(get_portfolio_risk(holdings), indent=2))


if __name__ == "__main__":
    _main()
