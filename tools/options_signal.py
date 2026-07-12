"""Options-derived signals (read-only) - the derivatives market's opinion.

Free Yahoo chains give IV, volume, and open interest. We derive what a swing
trader can honestly use: expected move (ATM straddle), IV level, put/call
volume ratio, IV skew, and unusual fresh positioning (volume >> OI). We can
NOT see aggressor side on free data, so direction is never claimed - the note
tells the brain exactly how far to trust each metric. We only READ options;
the long-only equity mandate is untouched.

CLI: python -m tools.options_signal DELL HPE
"""

from __future__ import annotations

import json
from datetime import datetime

AMBIGUITY_NOTE = (
    "Free chain data has NO aggressor side: high call volume is NOT proof of bullish "
    "flow (could be covered-call selling). Trust expected_move_pct for stop/target "
    "engineering and iv skew/put_call_ratio as sentiment tilt; treat unusual_strikes "
    "as 'someone cares about this date/level', not as a directional signal.")


def _signal_for(ticker: str) -> dict:
    import yfinance as yf
    tk = yf.Ticker(ticker)
    spot = float(tk.history(period="2d")["Close"].iloc[-1])
    expiries = tk.options or []
    today = datetime.now().date()
    # nearest expiry at least 7 days out (weekly noise avoided)
    expiry = next((e for e in expiries
                   if (datetime.strptime(e, "%Y-%m-%d").date() - today).days >= 7), None)
    if not expiry:
        return {"status": "no_chain"}
    days_out = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
    chain = tk.option_chain(expiry)
    calls, puts = chain.calls, chain.puts

    def mid(row) -> float:
        b, a = float(row.get("bid") or 0), float(row.get("ask") or 0)
        return (b + a) / 2 if b and a else float(row.get("lastPrice") or 0)

    atm_call = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]].iloc[0]
    atm_put = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]].iloc[0]
    straddle = mid(atm_call) + mid(atm_put)
    ivs = [float(atm_call.get("impliedVolatility") or 0),
           float(atm_put.get("impliedVolatility") or 0)]
    call_vol = int(calls["volume"].fillna(0).sum())
    put_vol = int(puts["volume"].fillna(0).sum())

    def otm_iv(df, otm_strike_filter):
        sub = df[otm_strike_filter(df["strike"])]
        sub = sub[sub["impliedVolatility"].notna()]
        return float(sub["impliedVolatility"].mean()) if len(sub) else None

    put_iv = otm_iv(puts, lambda s: (s >= spot * 0.85) & (s <= spot * 0.95))
    call_iv = otm_iv(calls, lambda s: (s >= spot * 1.05) & (s <= spot * 1.15))

    unusual = []
    for df, kind in ((calls, "call"), (puts, "put")):
        hot = df[(df["volume"].fillna(0) > 500)
                 & (df["volume"].fillna(0) > 3 * df["openInterest"].fillna(0).clip(lower=1))]
        for _, r in hot.nlargest(3, "volume").iterrows():
            unusual.append({"type": kind, "strike": float(r["strike"]),
                            "volume": int(r["volume"]), "open_interest": int(r["openInterest"] or 0),
                            "pct_from_spot": round((float(r["strike"]) / spot - 1) * 100, 1)})

    return {
        "status": "ok", "expiry": expiry, "days_to_expiry": days_out, "spot": round(spot, 2),
        "expected_move_pct": round(straddle / spot * 100, 2) if straddle else None,
        "atm_iv_pct": round(sum(ivs) / len(ivs) * 100, 1) if any(ivs) else None,
        "put_call_volume_ratio": round(put_vol / call_vol, 2) if call_vol else None,
        "iv_skew_pct": (round((put_iv - call_iv) * 100, 1)
                        if put_iv and call_iv else None),
        "skew_read": ("puts_bid_over_calls (downside protection in demand)"
                      if put_iv and call_iv and put_iv - call_iv > 0.03 else
                      "calls_bid_over_puts (speculative upside demand)"
                      if put_iv and call_iv and call_iv - put_iv > 0.03 else "balanced"),
        "unusual_strikes": unusual,
    }


def get_options_signals(tickers: list[str]) -> dict:
    out = {"status": "ok", "note": AMBIGUITY_NOTE, "tickers": {}}
    for t in tickers:
        try:
            out["tickers"][t.upper()] = _signal_for(t)
        except Exception as e:
            out["tickers"][t.upper()] = {"status": "error", "reason": str(e)[:150]}
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_options_signals(sys.argv[1:] or ["DELL"]), indent=1))
