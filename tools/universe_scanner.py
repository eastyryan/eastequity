"""Universe Scanner — swing-trade momentum/setup screen over the AI/data-center universe.

Pulls ~14 months of daily bars via yfinance for every ticker in data/universe.json
(plus SPY as the benchmark) and computes swing-relevant metrics over TRUE trading-
session windows: trend structure (50/100/200-day SMA), 1/3/6-month momentum, distance
from the trailing 52-week high, pullback depth from the 20-day high, volume surge,
ATR-based volatility, and relative strength vs SPY. Names with less than a full
252-session year of history are still admitted but flagged (is_full_52w_window) so the
"52-week" label is never claimed on a shorter window. Emits a ranked JSON list so
Claude can pick candidates worth deep research — the scanner ranks, it does not decide.

Window sizing is deliberate: ~14 months (~294 sessions) guarantees enough bars for a
real 200-day SMA and a true trailing-252-session 52-week high AFTER rolling (a 9- or
12-month pull would come up short once you roll a full year back).

CLI: python -m tools.universe_scanner [--top N]
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_universe() -> dict[str, list[str]]:
    return json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]


# Trading-session counts for each labelled window (~21 sessions per calendar month).
# Used everywhere so a field named "1m/3m/6m/52w" ALWAYS spans that many sessions and
# never the full fetch window. iloc offset for an N-session return is N+1 (the base bar
# sits N sessions before the last bar).
SESS_1M, SESS_3M, SESS_6M, SESS_52W = 21, 63, 126, 252

# Minimum bars to admit a name at all: enough for a valid 50-DMA and a true 3-month
# momentum reading (63 sessions -> needs 64 bars). Lowered from the old 130 so recent
# IPOs are screened too; longer windows (100/200-DMA, 6m momentum, 52w high) degrade
# gracefully to None / partial-window rather than being silently mislabeled.
MIN_BARS = SESS_3M + 1  # 64

# Sessions for the average-dollar-volume liquidity read, and the threshold below which
# a name is flagged illiquid. $20M/day: a $10k paper book never moves these, but the
# discipline surfaces names that would be hard to enter/exit at real size. Names are
# FLAGGED, never dropped (a held name must always survive the scan).
SESS_ADV, MIN_ADV_USD = 20, 20_000_000


def _median_dollar_volume(closes, volumes, sessions: int = SESS_ADV):
    """Median close x volume over the trailing `sessions` bars (median is robust to a
    single earnings-day volume spike). Pure Python so it is unit-testable without pandas."""
    if not closes or not volumes:
        return None
    n = min(len(closes), len(volumes))
    if n == 0:
        return None
    dv = [closes[i] * volumes[i] for i in range(n)
          if closes[i] is not None and volumes[i] is not None]
    if not dv:
        return None
    window = dv[-sessions:] if len(dv) >= sessions else dv
    s = sorted(window)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _return_over_sessions(closes, sessions: int):
    """Total return over `sessions` trading sessions from an oldest-first sequence of
    closing prices. None when history is too short or the base price is unusable. Pure
    Python (list/tuple in, float out) so it is unit-testable without pandas/numpy."""
    if closes is None or sessions <= 0:
        return None
    n = len(closes)
    if n < sessions + 1:
        return None
    base = closes[-(sessions + 1)]
    last = closes[-1]
    if base in (None, 0) or last is None:
        return None
    return last / base - 1


def _trailing_high(highs, sessions: int):
    """Highest value over the trailing `sessions` bars (all bars if fewer exist).
    Returns (high, is_full_window). is_full_window is True only when at least `sessions`
    bars were available, so callers can gate the "52-week" claim. Pure Python."""
    if not highs:
        return None, False
    window = highs[-sessions:] if len(highs) >= sessions else list(highs)
    vals = [h for h in window if h is not None]
    if not vals:
        return None, False
    return max(vals), len(highs) >= sessions


# 200-WEEK moving average: ~4 years of weekly closes. A widely-watched long-term
# support/reversion zone - fundamentally strong compounders trading AT or BELOW it
# have historically been "quality on sale" entries (the MSFT-below-its-200W pattern).
# Weekly bars move slowly, so one extra batched 5y/1wk download per scan is enough.
WEEKS_200 = 200
AT_OR_BELOW_TOLERANCE_PCT = 2.0  # "at" = within +2% above; anything below qualifies


def _ma_tail(values, n: int):
    """(mean of the last n values, is_full_window). Partial history returns the mean
    over what exists with is_full=False so callers can refuse to claim '200-week'.
    Pure Python (list in, float out) for offline unit tests."""
    if not values:
        return None, False
    vals = [v for v in values[-n:] if v is not None]
    if not vals:
        return None, False
    return sum(vals) / len(vals), len(values) >= n


def _load_ai_exposure() -> dict:
    """{TICKER: {exposure, reason}} from data/ai_exposure.json - the business-reality
    layer: is this company SELLING the AI buildout, benefiting from it, orthogonal to
    it, or is its core product replicable by frontier AI (the retail bear case)?
    Numbers lag narrative: a name can grow revenue for years while the market reprices
    its terminal value - this label is how the scanner carries that context. Fail-soft
    to empty (rows then carry no label; the brain still applies the CLAUDE.md rules)."""
    try:
        data = json.loads((ROOT / "data" / "ai_exposure.json").read_text())
        return {t.upper(): v for t, v in (data.get("labels") or {}).items()}
    except Exception:
        return {}


def _deep_value_sort_key(row: dict) -> tuple:
    """Deep-value lane ordering: AI-at-risk names rank LAST regardless of how far
    below the 200W MA they sit - a business whose core product frontier AI can
    replicate is the value-trap case, not the generational-entry case. Within each
    group, deepest below the MA first. Pure for offline tests."""
    return (row.get("ai_exposure") == "ai_at_risk",
            row.get("pct_vs_200w_ma") if row.get("pct_vs_200w_ma") is not None else 0.0)


def _screen_quality_ok(screen_row) -> bool:
    """Deterministic pre-filter for the deep-value lane: exclude names the estimate
    screen shows as SHRINKING with falling estimates (a broken business below its
    200W MA is a value trap, not a discount). Missing data passes - the brain and
    the deep fundamentals do the real vetting; this only removes obvious wrecks."""
    if not isinstance(screen_row, dict):
        return True
    growth = screen_row.get("fwd_revenue_growth_pct")
    direction = screen_row.get("revision_direction")
    if isinstance(growth, (int, float)) and growth < 0 and direction == "down":
        return False
    return True


def _trend_read(last, sma50, sma200) -> str:
    """Market-environment label from benchmark trend structure. 'supportive' =
    healthy uptrend (press with size); 'neutral' = mixed (normal selectivity);
    'hostile' = below the long-term average (raise the bar, prefer cash). Pure."""
    if last is None or sma200 is None:
        return "unknown"
    if last < sma200:
        return "hostile"
    if sma50 is not None and last > sma50 > sma200:
        return "supportive"
    return "neutral"


def _rel_strength(name_ret, bench_ret):
    """Relative strength: the name's return minus the benchmark's over the SAME window
    (both plain fractions). None if either side is missing. This is cross-sectional
    signal the pure-momentum funnel lacks, not another re-derivation of the name's price."""
    if name_ret is None or bench_ret is None:
        return None
    return name_ret - bench_ret


# --- entry-timing indicators (pure Python, list-in/scalar-out, offline-testable) ---
# The trend/volume stack answers "is this a setup?"; these answer "is NOW the entry?"
# None of them feed swing_setup_score - re-ranking the funnel is a separate decision.

def _short_interest_from_info(info: dict):
    """Short-interest snapshot from a yfinance info dict. Pure/testable; None
    when nothing useful is present. Read it two-sided: a rising short base into
    strength is squeeze fuel AND evidence someone is paying to bet against the
    name — cite which reading the thesis takes."""
    if not isinstance(info, dict):
        return None
    shares_short = info.get("sharesShort")
    prior = info.get("sharesShortPriorMonth")
    direction = None
    if isinstance(shares_short, (int, float)) and isinstance(prior, (int, float)) and prior > 0:
        chg = shares_short / prior - 1
        direction = "rising" if chg > 0.02 else "falling" if chg < -0.02 else "flat"
    pct_float = info.get("shortPercentOfFloat")
    out = {
        "shares_short": shares_short,
        "short_pct_float": (round(pct_float * 100, 2)
                            if isinstance(pct_float, (int, float)) else None),
        "days_to_cover": info.get("shortRatio"),
        "shares_short_prior_month": prior,
        "month_over_month": direction,
    }
    return out if any(v is not None for v in out.values()) else None


def _rsi14(closes, period: int = 14):
    """Wilder RSI. None with fewer than period+1 closes."""
    if not closes or len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + (-d if d < 0 else 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _ema_series(values, span: int):
    """Simple recursive EMA over the whole list (seeded on the first value)."""
    if not values:
        return []
    k = 2.0 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _macd_state(closes, fast: int = 12, slow: int = 26,
                signal: int = 9, cross_lookback: int = 5):
    """MACD(12/26/9) histogram state. None under slow+signal bars.

    state: bull_cross_recent / bear_cross_recent when the histogram changed sign
    within the last `cross_lookback` sessions (the actionable moment), else
    above_zero / below_zero (the standing regime)."""
    if not closes or len(closes) < slow + signal:
        return None
    macd = [f - s for f, s in zip(_ema_series(closes, fast), _ema_series(closes, slow))]
    hist = [m - s for m, s in zip(macd, _ema_series(macd, signal))]
    state = "above_zero" if hist[-1] > 0 else "below_zero"
    for i in range(1, min(cross_lookback, len(hist) - 1) + 1):
        if (hist[-i] > 0) != (hist[-i - 1] > 0):
            state = "bull_cross_recent" if hist[-i] > 0 else "bear_cross_recent"
            break
    return {"hist": round(hist[-1], 4),
            "hist_direction": ("rising" if len(hist) >= 2 and hist[-1] > hist[-2]
                               else "falling"),
            "state": state}


def _adx14(highs, lows, closes, period: int = 14):
    """Wilder ADX with DI+/DI-. Returns (adx, di_plus, di_minus), all None when
    fewer than 2*period+1 bars. ADX measures trend STRENGTH, not direction:
    <20 = chop, >25 = established trend; DI+ vs DI- gives the direction."""
    n = min(len(highs or []), len(lows or []), len(closes or []))
    if n < 2 * period + 1:
        return None, None, None
    trs, pdms, ndms = [], [], []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdms.append(up if up > dn and up > 0 else 0.0)
        ndms.append(dn if dn > up and dn > 0 else 0.0)
    atr, pdm, ndm = sum(trs[:period]), sum(pdms[:period]), sum(ndms[:period])
    di_p = di_n = 0.0
    dxs = []
    for i in range(period, len(trs)):
        atr = atr - atr / period + trs[i]
        pdm = pdm - pdm / period + pdms[i]
        ndm = ndm - ndm / period + ndms[i]
        di_p = 100.0 * pdm / atr if atr else 0.0
        di_n = 100.0 * ndm / atr if atr else 0.0
        denom = di_p + di_n
        dxs.append(100.0 * abs(di_p - di_n) / denom if denom else 0.0)
    if len(dxs) < period:
        return None, None, None
    adx = sum(dxs[:period]) / period
    for x in dxs[period:]:
        adx = (adx * (period - 1) + x) / period
    return adx, di_p, di_n


def _gap_events(opens, closes, sessions: int = 20, min_gap_pct: float = 2.0):
    """Open gaps >= min_gap_pct vs the prior close over the trailing `sessions`.

    Returns {"count_20d", "last": {sessions_ago, pct, direction, filled}|None},
    or None with under 2 bars. "filled" = a LATER CLOSE traded back through the
    pre-gap close (a conservative close-based proxy; intraday touches don't count).
    An unfilled up-gap is urgency; a filled one is a failed move."""
    n = min(len(opens or []), len(closes or []))
    if n < 2:
        return None
    events = []
    for i in range(max(1, n - sessions), n):
        prev_close = closes[i - 1]
        if not prev_close or opens[i] is None:
            continue
        gap = (opens[i] / prev_close - 1) * 100.0
        if abs(gap) >= min_gap_pct:
            later = closes[i:]
            filled = (any(c is not None and c <= prev_close for c in later) if gap > 0
                      else any(c is not None and c >= prev_close for c in later))
            events.append({"sessions_ago": n - 1 - i, "pct": round(gap, 1),
                           "direction": "up" if gap > 0 else "down",
                           "filled": bool(filled)})
    return {"count_20d": len(events), "last": events[-1] if events else None}


def scan_universe(top_n: int = 15) -> dict:
    import pandas as pd
    import yfinance as yf

    sectors = load_universe()
    ticker_sector = {}
    for sector, ts in sectors.items():
        for t in ts:
            ticker_sector.setdefault(t, sector)
    tickers = sorted(ticker_sector)

    # 14mo (~294 sessions) so a true 200-DMA and a trailing-252-session 52-week high
    # survive after rolling. SPY rides along in the same batched download to power
    # relative strength without an extra network round-trip.
    data = yf.download(tickers + ["SPY"], period="14mo", interval="1d",
                       group_by="ticker", auto_adjust=True, progress=False, threads=True)

    # Benchmark returns over the SAME session windows used per-name, computed once.
    # Also the market-ENVIRONMENT read: only press hard in a supportive tape.
    spy_1m = spy_3m = None
    benchmark_trend = {"read": "unknown"}
    try:
        spy_close = [float(x) for x in data["SPY"]["Close"].dropna().tolist()]
        spy_1m = _return_over_sessions(spy_close, SESS_1M)
        spy_3m = _return_over_sessions(spy_close, SESS_3M)
        if len(spy_close) >= 200:
            s_last = spy_close[-1]
            s50 = sum(spy_close[-50:]) / 50
            s200 = sum(spy_close[-200:]) / 200
            benchmark_trend = {
                "spy_last": round(s_last, 2),
                "spy_50dma": round(s50, 2),
                "spy_200dma": round(s200, 2),
                "spy_above_200dma": s_last > s200,
                "spy_1m_pct": round(spy_1m * 100, 1) if spy_1m is not None else None,
                "read": _trend_read(s_last, s50, s200),
            }
    except Exception:
        spy_1m = spy_3m = None

    # 200-WEEK MA: separate batched WEEKLY download (~260 rows/name - lighter than the
    # daily pull). Fail-soft: if the weekly fetch dies, the scan proceeds without the
    # deep-value lane rather than failing the whole cycle.
    w200: dict[str, tuple] = {}  # ticker -> (ma, is_full, pct_vs_ma)
    try:
        wdata = yf.download(tickers, period="5y", interval="1wk",
                            group_by="ticker", auto_adjust=True, progress=False, threads=True)
        for t in tickers:
            try:
                wcloses = [float(x) for x in wdata[t]["Close"].dropna().tolist()]
                if not wcloses:
                    continue
                ma, full = _ma_tail(wcloses, WEEKS_200)
                if ma:
                    w200[t] = (ma, full, (wcloses[-1] / ma - 1) * 100)
            except Exception:
                continue
    except Exception:
        w200 = {}

    # Estimate screen cache (whole universe, refreshed pre-market) powers the
    # deep-value quality gate without any extra network. Fail-soft to empty.
    try:
        screen_rows = (json.loads((ROOT / "data" / "fundamental_screen.json").read_text())
                       .get("current", {}).get("rows", {})) or {}
    except Exception:
        screen_rows = {}

    # Business-reality layer: per-name AI-exposure labels (see _load_ai_exposure).
    ai_exposure = _load_ai_exposure()

    rows = []
    for t in tickers:
        try:
            df = data[t].dropna()
            n = len(df)
            if n < MIN_BARS:
                continue
            close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
            close_list = [float(x) for x in close.tolist()]
            high_list = [float(x) for x in high.tolist()]
            last = close_list[-1]
            pct_1d = last / close_list[-2] - 1
            sma20 = float(close.rolling(20).mean().iloc[-1])
            sma50 = float(close.rolling(50).mean().iloc[-1])
            # 100/200-DMA only exist once enough sessions have rolled; None otherwise
            # so short-history names never get a fabricated long-term trend read.
            sma100 = float(close.rolling(100).mean().iloc[-1]) if n >= 100 else None
            sma200 = float(close.rolling(200).mean().iloc[-1]) if n >= 200 else None
            # True session-count momentum (21/63/126), NOT the full fetch window.
            mom_1m = _return_over_sessions(close_list, SESS_1M)  # numeric (n >= MIN_BARS)
            mom_3m = _return_over_sessions(close_list, SESS_3M)  # numeric (n >= MIN_BARS)
            mom_6m = _return_over_sessions(close_list, SESS_6M)  # None until 127 bars
            # True trailing-252-session high; is_full flags whether it is really 52 weeks.
            hi_52w, full_52w = _trailing_high(high_list, SESS_52W)
            pullback_20d = last / float(high.rolling(20).max().iloc[-1]) - 1
            vol_surge = float(vol.iloc[-5:].mean()) / max(float(vol.iloc[-65:-5].mean()), 1)
            tr = pd.concat([high - low, (high - close.shift()).abs(),
                            (low - close.shift()).abs()], axis=1).max(axis=1)
            atr_pct = float(tr.rolling(14).mean().iloc[-1]) / last

            # Relative strength vs SPY over the matching windows (cross-sectional signal).
            rel_1m = _rel_strength(mom_1m, spy_1m)
            rel_3m = _rel_strength(mom_3m, spy_3m)

            # Liquidity: 20-session median dollar volume, and a FLAG (never a filter).
            vol_list = [float(x) for x in vol.tolist()]
            adv_usd = _median_dollar_volume(close_list, vol_list)
            is_liquid = adv_usd is not None and adv_usd >= MIN_ADV_USD

            # Volume read: effort-vs-result, climax, OBV divergence, CMF, breakout
            # sponsorship - the conviction dimension of the price action.
            from tools.volume_analysis import analyze_volume
            low_list = [float(x) for x in low.tolist()]
            open_list = [float(x) for x in df["Open"].tolist()]
            volume_signal = analyze_volume(open_list, high_list, low_list,
                                           close_list, vol_list)

            # Entry-timing layer: oscillators + trend strength + gap map. These
            # answer "is NOW the entry?" on top of the setup/trend/volume stack;
            # deliberately NOT folded into swing_setup_score.
            rsi = _rsi14(close_list)
            macd = _macd_state(close_list)
            adx, di_plus, di_minus = _adx14(high_list, low_list, close_list)
            gap_analysis = _gap_events(open_list, close_list)

            # Derived trend booleans (None when the underlying SMA has too little history).
            above_50 = last > sma50
            above_200 = (last > sma200) if sma200 is not None else None
            trend_50_100 = (sma50 > sma100) if sma100 is not None else None
            trend_50_200 = (sma50 > sma200) if sma200 is not None else None
            pct_from_hi = (last / hi_52w - 1) * 100 if hi_52w else None

            # Swing setup score: uptrend structure + medium-term momentum, rewarding
            # orderly pullbacks (buyable) over chases at the highs. SCORING CHANGE: the
            # primary uptrend gate now uses the TRUE 200-DMA (last > sma50 > sma200),
            # falling back to the 100-DMA and then the 50-DMA only for names without a
            # full 200 sessions yet. A small, bounded relative-strength term nudges
            # market-beaters up as a tiebreaker without overriding the momentum ranking.
            if sma200 is not None:
                in_uptrend = last > sma50 > sma200
            elif sma100 is not None:
                in_uptrend = last > sma50 > sma100
            else:
                in_uptrend = last > sma50
            score = 0.0
            score += 2.0 if in_uptrend else 0.0
            score += 1.0 if last > sma20 else 0.0
            score += max(min(mom_3m * 5, 2.0), -1.0)
            score += max(min(mom_1m * 5, 1.0), -1.0)
            score += 1.0 if -0.10 <= pullback_20d <= -0.02 else 0.0
            score += 0.5 if vol_surge > 1.3 else 0.0
            if rel_1m is not None:  # bounded [-0.2, +0.4]: refine, don't reorder
                score += max(min(rel_1m * 4, 0.4), -0.2)

            rows.append({
                "ticker": t, "sector": ticker_sector[t],
                "last_close": round(last, 2),
                "pct_change_1d": round(pct_1d * 100, 2),
                "momentum_1m_pct": round(mom_1m * 100, 1),
                "momentum_3m_pct": round(mom_3m * 100, 1),
                "momentum_6m_pct": round(mom_6m * 100, 1) if mom_6m is not None else None,
                "above_50dma": above_50,
                "above_200dma": above_200,
                "trend_up_50_over_100": trend_50_100,
                "trend_up_50_over_200": trend_50_200,
                "pct_from_52w_high": round(pct_from_hi, 1) if pct_from_hi is not None else None,
                "dist_from_52w_high_pct": round(-pct_from_hi, 1) if pct_from_hi is not None else None,
                "is_full_52w_window": bool(full_52w),
                "high_window_sessions": min(n, SESS_52W),
                "pullback_from_20d_high_pct": round(pullback_20d * 100, 1),
                "volume_surge_5d_vs_3m": round(vol_surge, 2),
                "atr_pct": round(atr_pct * 100, 2),
                "rsi_14": round(rsi, 1) if rsi is not None else None,
                "macd": macd,
                "adx_14": round(adx, 1) if adx is not None else None,
                "di_plus": round(di_plus, 1) if di_plus is not None else None,
                "di_minus": round(di_minus, 1) if di_minus is not None else None,
                "gap_analysis": gap_analysis,
                "rel_strength_1m_pct": round(rel_1m * 100, 1) if rel_1m is not None else None,
                "rel_strength_3m_pct": round(rel_3m * 100, 1) if rel_3m is not None else None,
                "avg_dollar_volume_20d_usd": round(adv_usd) if adv_usd is not None else None,
                "liquid": is_liquid,
                # 200-WEEK MA context (None when the weekly fetch missed this name).
                "wma_200w": round(w200[t][0], 2) if t in w200 else None,
                "pct_vs_200w_ma": round(w200[t][2], 1) if t in w200 else None,
                "is_full_200w_window": bool(w200[t][1]) if t in w200 else False,
                # Business-reality: what this company sells relative to the AI wave.
                "ai_exposure": (ai_exposure.get(t) or {}).get("exposure"),
                "ai_exposure_reason": (ai_exposure.get(t) or {}).get("reason"),
                "volume_signal": volume_signal,
                "swing_setup_score": round(score, 2),
            })
        except Exception:
            continue

    rows.sort(key=lambda r: r["swing_setup_score"], reverse=True)

    # Contrarian/value-reversal lane: quality names knocked well off their highs
    # that are turning up. A momentum-only funnel never shows these to the brain.
    # Only names with a genuine trailing-52-week window qualify, so the "20% off the
    # 52-week high" claim is truthful (partial-history names are excluded, not counted
    # off a shorter high that understates their drawdown).
    top_tickers = {r["ticker"] for r in rows[:top_n]}
    contrarian = sorted(
        (r for r in rows
         if r["ticker"] not in top_tickers
         and r.get("is_full_52w_window")
         and r.get("pct_from_52w_high") is not None
         and r["pct_from_52w_high"] <= -20
         and r["momentum_1m_pct"] > 2),
        key=lambda r: r["momentum_1m_pct"], reverse=True)[:5]
    for r in contrarian:
        r["lane"] = "contrarian_reversal"

    # Deep-value lane: LIQUID names with a genuine 200-week history trading AT
    # (within +2%) or BELOW their 200-week MA - a widely-watched long-term
    # support/reversion zone where quality compounders have historically been
    # generational entries. The estimate screen removes obvious wrecks (shrinking
    # + falling estimates = value trap); the brain and deep fundamentals do the
    # real "is this business actually strong" vetting. Sorted deepest-below first.
    lane_taken = top_tickers | {r["ticker"] for r in contrarian}
    deep_value = sorted(
        (r for r in rows
         if r["ticker"] not in lane_taken
         and r.get("liquid")
         and r.get("is_full_200w_window")
         and r.get("pct_vs_200w_ma") is not None
         and r["pct_vs_200w_ma"] <= AT_OR_BELOW_TOLERANCE_PCT
         and _screen_quality_ok(screen_rows.get(r["ticker"]))),
        key=_deep_value_sort_key)[:5]
    for r in deep_value:
        r["lane"] = "deep_value_200w"

    # Supplier-pullback lane: AI SUPPLIERS (memory, photonics, interconnect, power...)
    # that ran hard, are now 8-30% off their 52-week high, but still hold their
    # 200-DMA - the "extended name coming back in" setup. Memory names here often
    # print the lowest forward multiples on the board; CLAUDE.md's cyclical-value
    # discipline governs whether that is value or a cycle-peak trap.
    lane_taken |= {r["ticker"] for r in deep_value}
    supplier_pullbacks = sorted(
        (r for r in rows
         if r["ticker"] not in lane_taken
         and r.get("ai_exposure") == "ai_supplier"
         and r.get("liquid")
         and r.get("is_full_52w_window")
         and r.get("above_200dma")
         and r.get("pct_from_52w_high") is not None
         and -30 <= r["pct_from_52w_high"] <= -8),
        key=lambda r: r.get("rel_strength_3m_pct") if r.get("rel_strength_3m_pct")
        is not None else -999, reverse=True)[:5]
    for r in supplier_pullbacks:
        r["lane"] = "supplier_pullback"

    # Valuation + analyst-estimate context for top setups, contrarian, deep-value
    # AND supplier-pullback picks (per-ticker calls are slow, so not the whole universe).
    for r in rows[:top_n] + contrarian + deep_value + supplier_pullbacks:
        tk = yf.Ticker(r["ticker"])
        try:
            info = tk.info
            r["valuation"] = {
                "trailing_pe": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "price_to_sales_ttm": info.get("priceToSalesTrailing12Months"),
                "ev_to_ebitda": info.get("enterpriseToEbitda"),
            }
            r["market_cap_usd"] = info.get("marketCap")  # feeds the $1B floor
            # Analyst consensus + short interest ride along at zero extra network
            # cost (info is already fetched). Sentiment context, not signals.
            from tools.news_catalysts import _ratings_from_info
            r["analyst_ratings"] = _ratings_from_info(info)
            r["short_interest"] = _short_interest_from_info(info)
        except Exception:
            r["valuation"] = None
        # Earnings clock: swing entries and binary prints interact constantly.
        try:
            from datetime import datetime, timezone
            now = pd.Timestamp.now(tz="America/New_York")
            ed = tk.earnings_dates
            if ed is not None and len(ed.index):
                past = [d for d in ed.index if d <= now]
                future = [d for d in ed.index if d > now]
                if future:
                    r["days_to_earnings"] = (min(future) - now).days
                if past:
                    r["days_since_earnings"] = (now - max(past)).days
        except Exception:
            pass
        # Is the multiple deserved? Forward growth + which way analysts are revising.
        try:
            est: dict = {}
            rev = tk.revenue_estimate
            if rev is not None and "growth" in rev and "0y" in rev.index:
                est["fwd_revenue_growth_pct"] = round(float(rev.loc["0y", "growth"]) * 100, 1)
                if "+1y" in rev.index:
                    est["next_yr_revenue_growth_pct"] = round(float(rev.loc["+1y", "growth"]) * 100, 1)
            trend = tk.eps_trend
            if trend is not None and "0y" in trend.index:
                cur, m30 = float(trend.loc["0y", "current"]), float(trend.loc["0y", "30daysAgo"])
                if m30:
                    est["eps_revision_30d_pct"] = round((cur - m30) / abs(m30) * 100, 2)
                    est["eps_revision_direction"] = ("up" if cur > m30 else
                                                     "down" if cur < m30 else "flat")
            r["analyst_estimates"] = est or None
        except Exception:
            r["analyst_estimates"] = None
        # Post-earnings drift: recently reported, estimates going up, price not yet
        # rewarded. Historically the cleanest swing setup for 10-15% compounding.
        dse = r.get("days_since_earnings")
        revision_up = (r.get("analyst_estimates") or {}).get("eps_revision_direction") == "up"
        r["post_earnings_drift_candidate"] = bool(
            dse is not None and 0 <= dse <= 15 and revision_up
            and r.get("momentum_1m_pct", 99) < 15)
        # Fwd-multiple vs growth: which extended AI suppliers are CHEAP relative to
        # their growth (memory names often print the lowest fwd multiples late-cycle -
        # the brain must judge cycle-peak vs value; this just makes it visible).
        fpe = (r.get("valuation") or {}).get("forward_pe")
        g = (r.get("analyst_estimates") or {}).get("fwd_revenue_growth_pct")
        r["fwd_pe_to_growth"] = (round(fpe / g, 2)
                                 if isinstance(fpe, (int, float)) and isinstance(g, (int, float))
                                 and fpe > 0 and g > 0 else None)

    # Sector relative strength: which parts of the AI stack money is rotating
    # into or out of. Computed over the whole universe, not just top setups.
    by_sector: dict[str, list[dict]] = {}
    for r in rows:
        by_sector.setdefault(r["sector"], []).append(r)
    sector_rs = sorted(
        ({"sector": s,
          "avg_momentum_1m_pct": round(sum(x["momentum_1m_pct"] for x in xs) / len(xs), 1),
          "avg_momentum_3m_pct": round(sum(x["momentum_3m_pct"] for x in xs) / len(xs), 1),
          "names": len(xs)}
         for s, xs in by_sector.items() if xs),
        key=lambda d: d["avg_momentum_1m_pct"], reverse=True)

    return {
        "contrarian_setups": contrarian,
        "deep_value_200w": deep_value,
        "supplier_pullbacks": supplier_pullbacks,
        "benchmark_trend": benchmark_trend,
        "sector_relative_strength": sector_rs,
        "status": "ok",
        "scanned": len(rows),
        "note": "Ranked by deterministic swing_setup_score; agent must apply judgment and research before proposing.",
        "top_setups": rows[:top_n],
        "prices": {r["ticker"]: r["last_close"] for r in rows},
        # 14-day ATR% for EVERY scanned name (not just top_setups), so the
        # deterministic volatility-stop floor has universe-wide coverage - including
        # cloud runs, which read this straight from the relayed data bundle.
        "atr_by_ticker": {r["ticker"]: r["atr_pct"] for r in rows},
    }


if __name__ == "__main__":
    import sys
    n = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 15
    print(json.dumps(scan_universe(n), indent=2))
