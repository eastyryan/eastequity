"""Volume analysis - how price moves ON volume, and how reversals print in it.

Volume is the conviction dimension of price action: price says WHAT happened,
volume says HOW MANY agreed. This module distills the durable, daily-bar-testable
techniques into deterministic signals:

  EFFORT vs RESULT (Wyckoff): huge volume (effort) with a tiny price range
    (result) means the tape ABSORBED the flow - someone big was on the other
    side. Near lows after a decline = accumulation candidate (bullish); near
    highs after an advance = distribution candidate (bearish).
  CLIMACTIC VOLUME: an extreme-volume (>=3x average), wide-range bar after an
    extended slide is capitulation - it often MARKS the low but is not itself
    the entry; the classic reversal sequence is climax -> automatic rally ->
    secondary test on MUCH lighter volume (sellers exhausted) -> entry.
  NO-SUPPLY PULLBACK: a drift back to support on shrinking volume (<~0.75x
    average) says sellers are absent - constructive for continuation.
  OBV DIVERGENCE: On-Balance Volume is cumulative signed volume; when price
    makes a lower low but OBV holds a higher low, accumulation is happening
    under the surface (bullish divergence) - and vice versa at highs.
  CMF (Chaikin Money Flow, 20d): volume weighted by WHERE in the bar's range
    the close landed; persistently positive = closes near highs on volume =
    accumulation.
  BREAKOUT CONFIRMATION: a 20-day-high breakout on <1.5x average volume is
    suspect; on >=1.5x it has real sponsorship.
  UP/DOWN VOLUME RATIO (25d): total volume on up days vs down days - a slow
    accumulation/distribution gauge.
  POCKET PIVOT: an up day whose volume exceeds every down-day volume of the
    prior 10 sessions, printed within/near a base - institutional footprint.

All functions are pure Python over plain lists (oldest-first daily bars), so the
scanner reuses its in-memory data, the chart renderer annotates the same reads,
and everything is unit-testable offline.

CLI: python -m tools.volume_analysis DELL
"""

from __future__ import annotations

RVOL_AVG_SESSIONS = 50
CLIMAX_RVOL = 3.0          # x average volume = climactic
ABSORPTION_RVOL = 2.0      # x average volume with...
ABSORPTION_RANGE_ATR = 0.6  # ...a range this fraction of ATR or less = absorption
BREAKOUT_RVOL = 1.5        # minimum sponsorship for a breakout to be "confirmed"
NO_SUPPLY_RVOL = 0.75      # pullback volume below this fraction of average = no supply


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _atr(highs, lows, closes, n: int = 14):
    """Average true range over the last n bars (absolute price units)."""
    if len(closes) < 2:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return _avg(trs[-n:])


def obv_series(closes, volumes):
    """On-Balance Volume: cumulative volume signed by the day's direction."""
    obv, out = 0.0, []
    for i in range(len(closes)):
        if i > 0:
            if closes[i] > closes[i - 1]:
                obv += volumes[i]
            elif closes[i] < closes[i - 1]:
                obv -= volumes[i]
        out.append(obv)
    return out


def cmf(highs, lows, closes, volumes, n: int = 20):
    """Chaikin Money Flow: volume weighted by close position in the bar's range.
    +1 = every close at its high on volume; -1 = every close at its low."""
    if len(closes) < n:
        return None
    mfv, vol = 0.0, 0.0
    for i in range(len(closes) - n, len(closes)):
        rng = highs[i] - lows[i]
        if rng <= 0 or not volumes[i]:
            continue
        mult = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng
        mfv += mult * volumes[i]
        vol += volumes[i]
    return round(mfv / vol, 3) if vol else None


def rvol(volumes, n: int = RVOL_AVG_SESSIONS):
    """Today's volume relative to the trailing n-session average (excl. today)."""
    if len(volumes) < 2:
        return None
    base = _avg(volumes[-(n + 1):-1])
    return round(volumes[-1] / base, 2) if base else None


def updown_volume_ratio(closes, volumes, n: int = 25):
    """Up-day volume / down-day volume over the last n sessions. >1 = accumulation."""
    up = down = 0.0
    lo = max(1, len(closes) - n)
    for i in range(lo, len(closes)):
        if closes[i] > closes[i - 1]:
            up += volumes[i]
        elif closes[i] < closes[i - 1]:
            down += volumes[i]
    if down == 0:
        return None if up == 0 else 99.0
    return round(up / down, 2)


def obv_divergence(closes, volumes, lookback: int = 20, recent: int = 5):
    """'bullish' when price prints a lower low vs the prior window but OBV holds a
    higher low (accumulation under the surface); 'bearish' for the mirror at highs;
    else 'none'. Deliberately simple - a divergence flag is a prompt to LOOK, not
    a trade signal."""
    if len(closes) < lookback + recent + 1:
        return "none"
    obv = obv_series(closes, volumes)
    p_recent, p_prior = closes[-recent:], closes[-(lookback + recent):-recent]
    o_recent, o_prior = obv[-recent:], obv[-(lookback + recent):-recent]
    if min(p_recent) < min(p_prior) and min(o_recent) > min(o_prior):
        return "bullish"
    if max(p_recent) > max(p_prior) and max(o_recent) < max(o_prior):
        return "bearish"
    return "none"


def analyze_volume(opens, highs, lows, closes, volumes) -> dict:
    """Composite volume read for one name from oldest-first daily bars.
    Returns a compact dict (the bundle is on a diet); `read` is the headline."""
    n = len(closes)
    if n < 30 or not volumes or not volumes[-1]:
        return {"read": "insufficient_data"}

    atr = _atr(highs, lows, closes)
    rv = rvol(volumes)
    last_range = highs[-1] - lows[-1]
    close_pos = ((closes[-1] - lows[-1]) / last_range) if last_range > 0 else 0.5
    hi20, lo20 = max(highs[-21:-1]), min(lows[-21:-1])
    near_lows = closes[-1] <= lo20 * 1.03
    near_highs = closes[-1] >= hi20 * 0.97
    down_5d = closes[-1] < closes[-6] if n >= 6 else False

    out = {
        "rvol": rv,
        "cmf_20": cmf(highs, lows, closes, volumes),
        "updown_vol_ratio_25d": updown_volume_ratio(closes, volumes),
        "obv_divergence": obv_divergence(closes, volumes),
    }

    # Pocket pivot: up day out-volumes every down day of the prior 10 sessions.
    down_vols = [volumes[i] for i in range(max(1, n - 11), n - 1)
                 if closes[i] < closes[i - 1]]
    out["pocket_pivot"] = bool(closes[-1] > closes[-2] and down_vols
                               and volumes[-1] > max(down_vols))

    # Breakout sponsorship: new 20d high today - did volume confirm?
    if closes[-1] > hi20:
        out["breakout_volume_confirmed"] = bool(rv and rv >= BREAKOUT_RVOL)

    # No-supply pullback: recent drift down on shrinking volume.
    base_vol = _avg(volumes[-(RVOL_AVG_SESSIONS + 1):-1])
    if base_vol and n >= 5:
        last3_down = sum(1 for i in (-3, -2, -1) if closes[i] < closes[i - 1]) >= 2
        light = _avg(volumes[-3:]) < NO_SUPPLY_RVOL * base_vol
        out["no_supply_pullback"] = bool(last3_down and light and not near_lows)

    # Headline read, most-informative first.
    read = "neutral"
    if rv and atr and last_range:
        if rv >= CLIMAX_RVOL and last_range >= 2 * atr:
            if down_5d and near_lows:
                # Capitulation. Close position tells whether buyers showed up.
                read = ("selling_climax_reversal_watch" if close_pos >= 0.5
                        else "selling_climax")
            elif near_highs:
                read = "buying_climax_watch" if close_pos <= 0.5 else "climactic_advance"
            else:
                read = "climactic_volume"
        elif rv >= ABSORPTION_RVOL and last_range <= ABSORPTION_RANGE_ATR * atr:
            # Effort without result = absorption; location decides the lean.
            read = ("absorption_at_lows_accumulation" if near_lows else
                    "absorption_at_highs_distribution" if near_highs else "churn_high_volume")
        elif out.get("breakout_volume_confirmed") is True:
            read = "confirmed_breakout"
        elif out.get("breakout_volume_confirmed") is False:
            read = "unconfirmed_breakout_suspect"
        elif out.get("pocket_pivot"):
            read = "pocket_pivot"
        elif out.get("no_supply_pullback"):
            read = "no_supply_pullback"
    out["read"] = read
    return out


if __name__ == "__main__":
    import json
    import sys
    import yfinance as yf
    t = sys.argv[1] if len(sys.argv) > 1 else "DELL"
    df = yf.Ticker(t).history(period="6mo", interval="1d", auto_adjust=True).dropna()
    print(json.dumps(analyze_volume(
        [float(x) for x in df["Open"]], [float(x) for x in df["High"]],
        [float(x) for x in df["Low"]], [float(x) for x in df["Close"]],
        [float(x) for x in df["Volume"]]), indent=1))
