"""Universe Scanner — swing-trade momentum/setup screen over the AI/data-center universe.

Pulls ~9 months of daily bars via yfinance for every ticker in data/universe.json
and computes swing-relevant metrics: trend structure (50/200 SMA), 1/3/6-month
momentum, distance from 52-week high, pullback depth from 20-day high, volume
surge, and ATR-based volatility. Emits a ranked JSON list so Claude can pick
candidates worth deep research — the scanner ranks, it does not decide.

CLI: python -m tools.universe_scanner [--top N]
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_universe() -> dict[str, list[str]]:
    return json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]


def scan_universe(top_n: int = 15) -> dict:
    import pandas as pd
    import yfinance as yf

    sectors = load_universe()
    ticker_sector = {}
    for sector, ts in sectors.items():
        for t in ts:
            ticker_sector.setdefault(t, sector)
    tickers = sorted(ticker_sector)

    data = yf.download(tickers, period="9mo", interval="1d",
                       group_by="ticker", auto_adjust=True, progress=False, threads=True)

    rows = []
    for t in tickers:
        try:
            df = data[t].dropna()
            if len(df) < 130:
                continue
            close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
            last = float(close.iloc[-1])
            pct_1d = last / float(close.iloc[-2]) - 1
            sma20, sma50 = float(close.rolling(20).mean().iloc[-1]), float(close.rolling(50).mean().iloc[-1])
            sma100 = float(close.rolling(100).mean().iloc[-1])
            mom_1m = last / float(close.iloc[-22]) - 1
            mom_3m = last / float(close.iloc[-64]) - 1
            mom_6m = last / float(close.iloc[0]) - 1
            hi_52w = float(high.max())
            pullback_20d = last / float(high.rolling(20).max().iloc[-1]) - 1
            vol_surge = float(vol.iloc[-5:].mean()) / max(float(vol.iloc[-65:-5].mean()), 1)
            tr = pd.concat([high - low, (high - close.shift()).abs(),
                            (low - close.shift()).abs()], axis=1).max(axis=1)
            atr_pct = float(tr.rolling(14).mean().iloc[-1]) / last

            # Swing setup score: uptrend structure + medium-term momentum,
            # rewarding orderly pullbacks (buyable) over chases at the highs.
            score = 0.0
            score += 2.0 if last > sma50 > sma100 else 0.0
            score += 1.0 if last > sma20 else 0.0
            score += max(min(mom_3m * 5, 2.0), -1.0)
            score += max(min(mom_1m * 5, 1.0), -1.0)
            score += 1.0 if -0.10 <= pullback_20d <= -0.02 else 0.0
            score += 0.5 if vol_surge > 1.3 else 0.0

            rows.append({
                "ticker": t, "sector": ticker_sector[t],
                "last_close": round(last, 2),
                "pct_change_1d": round(pct_1d * 100, 2),
                "momentum_1m_pct": round(mom_1m * 100, 1),
                "momentum_3m_pct": round(mom_3m * 100, 1),
                "momentum_6m_pct": round(mom_6m * 100, 1),
                "above_50dma": last > sma50,
                "trend_up_50_over_100": sma50 > sma100,
                "pct_from_52w_high": round((last / hi_52w - 1) * 100, 1),
                "pullback_from_20d_high_pct": round(pullback_20d * 100, 1),
                "volume_surge_5d_vs_3m": round(vol_surge, 2),
                "atr_pct": round(atr_pct * 100, 2),
                "swing_setup_score": round(score, 2),
            })
        except Exception:
            continue

    rows.sort(key=lambda r: r["swing_setup_score"], reverse=True)

    # Valuation + analyst-estimate context for the top setups only (per-ticker
    # calls are slow, so we don't fetch them for the whole universe).
    for r in rows[:top_n]:
        tk = yf.Ticker(r["ticker"])
        try:
            info = tk.info
            r["valuation"] = {
                "trailing_pe": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "price_to_sales_ttm": info.get("priceToSalesTrailing12Months"),
                "ev_to_ebitda": info.get("enterpriseToEbitda"),
            }
        except Exception:
            r["valuation"] = None
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

    return {
        "status": "ok",
        "scanned": len(rows),
        "note": "Ranked by deterministic swing_setup_score; agent must apply judgment and research before proposing.",
        "top_setups": rows[:top_n],
        "prices": {r["ticker"]: r["last_close"] for r in rows},
    }


if __name__ == "__main__":
    import sys
    n = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 15
    print(json.dumps(scan_universe(n), indent=2))
