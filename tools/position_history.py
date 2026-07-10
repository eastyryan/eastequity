"""Position History — recent daily price path for held tickers.

The brain reviews each holding seeing only the entry plan and today's price;
this tool gives it the recent path so "down slightly" vs "breaking down" is a
data-backed call, not a guess. Pulls ~3 weeks of daily bars via yfinance and
returns the last N sessions per ticker plus derived swing-relevant fields:
5-day change and distance from the 10-day high/low.

Fail-soft per ticker: a bad symbol gets an error string, everything else
still comes back. status is "unavailable" only if every ticker failed.

CLI: python -m tools.position_history DELL HPE
"""

from __future__ import annotations


def get_position_histories(tickers: list[str], days: int = 10) -> dict:
    """Return the last `days` daily OHLCV bars + derived fields per ticker."""
    if not tickers:
        return {"status": "ok", "tickers": {}}

    out: dict = {"status": "ok", "tickers": {}}
    try:
        import pandas as pd  # noqa: F401  (yfinance returns DataFrames)
        import yfinance as yf
        symbols = sorted({t.upper() for t in tickers if t})
        data = yf.download(symbols, period="1mo", interval="1d",
                           group_by="ticker", auto_adjust=True,
                           progress=False, threads=True)
    except Exception as e:
        return {"status": "unavailable", "error": str(e),
                "tickers": {t.upper(): {"error": str(e)} for t in tickers if t}}

    any_ok = False
    for t in symbols:
        try:
            # With a single symbol yfinance may return flat columns.
            df = data[t] if t in getattr(data.columns, "levels", [[]])[0] else data
            df = df.dropna(subset=["Close"])
            if df.empty:
                raise ValueError("no price data returned")
            df = df.tail(days)
            bars = []
            for idx, row in df.iterrows():
                bars.append({
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": round(float(row["Open"]), 2),
                    "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                })
            closes = [b["close"] for b in bars]
            highs = [b["high"] for b in bars]
            lows = [b["low"] for b in bars]
            last = closes[-1]
            entry: dict = {"bars": bars}
            # 5 sessions back needs 6 closes; degrade gracefully on short history.
            if len(closes) >= 6:
                entry["pct_change_5d"] = round((last / closes[-6] - 1) * 100, 2)
            else:
                entry["pct_change_5d"] = None
            entry["distance_from_10d_high_pct"] = round((last / max(highs) - 1) * 100, 2)
            entry["distance_from_10d_low_pct"] = round((last / min(lows) - 1) * 100, 2)
            out["tickers"][t] = entry
            any_ok = True
        except Exception as e:
            out["tickers"][t] = {"error": f"history unavailable: {e}"}

    if not any_ok:
        out["status"] = "unavailable"
    return out


if __name__ == "__main__":
    import json
    import sys
    ts = [a for a in sys.argv[1:] if not a.startswith("-")] or ["DELL", "HPE"]
    print(json.dumps(get_position_histories(ts), indent=2))
