"""Second-layer technicals: weekly timeframe, anchored VWAP, sector-relative strength.

WHY THESE FOUR (2026-07-19 review of what the scanner actually produced)
-----------------------------------------------------------------------
The daily indicator set was broad and in places genuinely good. Four gaps stood out,
and all four were things the model was being asked to eyeball off a PNG instead of
being handed as numbers:

  1. SINGLE TIMEFRAME. Everything was daily. For a 3-90 day swing the WEEKLY chart
     is where base structure lives, and the weekly bars were already being
     downloaded for the 200-week MA and then thrown away after one line of use.
  2. NO NUMERIC SUPPORT. "Support at $345-352" in the weekly run came from the
     brain's eyes, not from data. Nothing auditable, nothing backtestable.
     Anchored VWAP from the breakout is the standard answer and costs one pass.
  3. RELATIVE STRENGTH ONLY VS SPY. A semiconductor holding flat while semis are
     -12% is a completely different name from one merely matching SPY, and that
     distinction was not computed anywhere.
  4. (handled in universe_scanner) indicators computed for 184 names and retained
     for 35.

Everything here is pure: lists in, scalars/dicts out, no network, no pandas. A
value that cannot be computed from the bars provided returns None — never a
fabricated default. A short history must not produce a confident-looking number.
"""

from __future__ import annotations

# Stage-analysis convention: the 30-week MA is the weekly equivalent of the
# 200-DMA and is the line most weekly base work is measured against.
WEEKLY_MA_SESSIONS = 30
WEEKLY_RSI_PERIOD = 14
# A breakout anchor: the bar that took out the prior N-session high.
BREAKOUT_LOOKBACK = 20
# How far back to hunt for an anchor before giving up.
ANCHOR_MAX_SESSIONS = 120


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _clean(xs) -> list[float]:
    out = []
    for x in xs or []:
        v = _f(x)
        if v is not None:
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# 1. Weekly timeframe
# ---------------------------------------------------------------------------

def wilder_rsi(closes, period: int = WEEKLY_RSI_PERIOD):
    """Wilder RSI on whatever timeframe the closes are. None if too short.

    Same smoothing as the daily rsi_14 in universe_scanner so a weekly and daily
    reading are directly comparable rather than subtly different measures.
    """
    c = _clean(closes)
    if len(c) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def weekly_structure(weekly_closes, weekly_highs=None, weekly_lows=None) -> dict:
    """Weekly trend read: 30-week MA, weekly RSI, and higher-highs/higher-lows.

    The daily set can call a name an uptrend while its weekly structure is
    rolling over — that divergence is exactly what a swing trader wants to see
    and it was not computed anywhere.
    """
    c = _clean(weekly_closes)
    out: dict = {"weeks": len(c)}
    if not c:
        return {"weeks": 0, "status": "no_weekly_bars"}

    last = c[-1]
    out["last_weekly_close"] = round(last, 2)
    if len(c) >= WEEKLY_MA_SESSIONS:
        ma = sum(c[-WEEKLY_MA_SESSIONS:]) / WEEKLY_MA_SESSIONS
        out["wma_30w"] = round(ma, 2)
        out["pct_vs_30w_ma"] = round((last / ma - 1) * 100, 1) if ma else None
        out["above_30w_ma"] = last > ma
        # Rising 30-week MA = the classic Stage 2 condition. Compare against the
        # value four weeks back so a single week's wiggle does not flip it.
        if len(c) >= WEEKLY_MA_SESSIONS + 4:
            prev = sum(c[-WEEKLY_MA_SESSIONS - 4:-4]) / WEEKLY_MA_SESSIONS
            out["wma_30w_rising"] = ma > prev
    else:
        out["wma_30w"] = None
        out["insufficient_for_30w_ma"] = True

    out["weekly_rsi_14"] = wilder_rsi(c)

    hi, lo = _clean(weekly_highs), _clean(weekly_lows)
    if len(hi) >= 9 and len(lo) >= 9:
        # Higher highs AND higher lows over the last two 4-week blocks — the
        # plainest definition of an intact weekly uptrend.
        out["weekly_higher_highs"] = max(hi[-4:]) > max(hi[-8:-4])
        out["weekly_higher_lows"] = min(lo[-4:]) > min(lo[-8:-4])
        hh, hl = out["weekly_higher_highs"], out["weekly_higher_lows"]
        out["weekly_structure"] = ("uptrend" if hh and hl else
                                   "downtrend" if not hh and not hl else "mixed")
    return out


# ---------------------------------------------------------------------------
# 2. Anchored VWAP
# ---------------------------------------------------------------------------

def find_anchor_index(closes, highs, volumes, lookback: int = BREAKOUT_LOOKBACK,
                      max_back: int = ANCHOR_MAX_SESSIONS):
    """Index of the most recent BREAKOUT bar — one that took out the prior high.

    Falls back to the highest-volume session in the window, which is where
    institutional repositioning most likely happened. Returns (index, reason) or
    (None, why-not) so the caller can say WHY there is no anchor rather than
    silently omitting the level.
    """
    c, h, v = _clean(closes), _clean(highs), _clean(volumes)
    n = min(len(c), len(h), len(v))
    if n < lookback + 5:
        return None, "insufficient_history"
    start = max(lookback, n - max_back)
    for i in range(n - 1, start - 1, -1):
        prior_high = max(h[i - lookback:i]) if i >= lookback else None
        if prior_high and c[i] > prior_high:
            return i, f"breakout_above_{lookback}d_high"
    window = v[start:n]
    if window:
        j = start + max(range(len(window)), key=lambda k: window[k])
        return j, "highest_volume_session"
    return None, "no_anchor_found"


def anchored_vwap(closes, highs, lows, volumes, anchor_index: int):
    """Volume-weighted average price from `anchor_index` to the last bar.

    Typical price (H+L+C)/3 per bar, weighted by volume. This is the level the
    average buyer since that event paid — the most defensible numeric support in
    a post-breakout name, and the thing the brain was previously eyeballing.
    """
    c, h, l, v = _clean(closes), _clean(highs), _clean(lows), _clean(volumes)
    n = min(len(c), len(h), len(l), len(v))
    if anchor_index is None or not (0 <= anchor_index < n):
        return None
    num = den = 0.0
    for i in range(anchor_index, n):
        tp = (h[i] + l[i] + c[i]) / 3.0
        num += tp * v[i]
        den += v[i]
    if den <= 0:
        return None
    return round(num / den, 2)


def anchored_vwap_block(closes, highs, lows, volumes) -> dict:
    """Anchored VWAP + where price sits against it. Honest when unavailable."""
    idx, reason = find_anchor_index(closes, highs, volumes)
    if idx is None:
        return {"status": "unavailable", "reason": reason}
    vwap = anchored_vwap(closes, highs, lows, volumes, idx)
    c = _clean(closes)
    if vwap is None or not c:
        return {"status": "unavailable", "reason": "no_volume_in_window"}
    last = c[-1]
    n = min(len(c), len(_clean(volumes)))
    out = {
        "status": "ok",
        "anchor_reason": reason,
        "sessions_since_anchor": n - 1 - idx,
        "anchored_vwap": vwap,
        "pct_vs_anchored_vwap": round((last / vwap - 1) * 100, 1),
        "above_anchored_vwap": last > vwap,
        "note": ("Volume-weighted average price paid since the anchor event. "
                 "Above it, buyers since the anchor are collectively in profit "
                 "and it tends to act as support; below it, as resistance. This "
                 "is a measured level, not a chart read."),
    }
    return out


# ---------------------------------------------------------------------------
# 3. Relative strength vs SECTOR
# ---------------------------------------------------------------------------

def sector_medians(rows: list[dict]) -> dict:
    """{sector: {median 1m, median 3m, n}} over every scanned row.

    Median rather than mean on purpose: one +300% name should not redefine what
    "keeping up with the sector" means for its peers.
    """
    by: dict[str, dict[str, list]] = {}
    for r in rows or []:
        s = r.get("sector")
        if not s:
            continue
        b = by.setdefault(s, {"m1": [], "m3": []})
        for key, bucket in (("momentum_1m_pct", "m1"), ("momentum_3m_pct", "m3")):
            v = _f(r.get(key))
            if v is not None:
                b[bucket].append(v)

    def _med(xs):
        if not xs:
            return None
        xs = sorted(xs)
        m = len(xs) // 2
        return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0, 1)

    return {s: {"median_momentum_1m_pct": _med(b["m1"]),
                "median_momentum_3m_pct": _med(b["m3"]),
                "n": max(len(b["m1"]), len(b["m3"]))}
            for s, b in by.items()}


def attach_sector_relative_strength(rows: list[dict], medians: dict | None = None,
                                    min_peers: int = 3) -> dict:
    """Add rel_strength_vs_sector_1m/3m_pct to each row, in place.

    A name beating SPY while LAGGING its own sector is not a leader — it is a
    laggard in a strong group, and that is a materially different trade. Sectors
    with fewer than `min_peers` scored names are skipped rather than compared
    against a one-name "median".
    """
    medians = medians or sector_medians(rows)
    attached = skipped_thin = 0
    for r in rows or []:
        med = medians.get(r.get("sector"))
        if not med or (med.get("n") or 0) < min_peers:
            skipped_thin += 1
            continue
        did = False
        for own, med_key, out_key in (
                ("momentum_1m_pct", "median_momentum_1m_pct",
                 "rel_strength_vs_sector_1m_pct"),
                ("momentum_3m_pct", "median_momentum_3m_pct",
                 "rel_strength_vs_sector_3m_pct")):
            a, b = _f(r.get(own)), _f(med.get(med_key))
            if a is not None and b is not None:
                r[out_key] = round(a - b, 1)
                did = True
        if did:
            r["sector_peers_scored"] = med.get("n")
            attached += 1
    return {"attached": attached, "skipped_thin_sector": skipped_thin,
            "min_peers": min_peers}


# ---------------------------------------------------------------------------
# 4. Compact line retained for EVERY scanned name
# ---------------------------------------------------------------------------

# Chosen to answer "is this a setup and is now the entry" in one line per name.
# Deliberately short: 184 names x this many fields has to stay affordable inside
# the brain's read window.
COMPACT_FIELDS = (
    "last_close", "pct_change_1d", "atr_pct",
    "momentum_1m_pct", "momentum_3m_pct",
    "rel_strength_1m_pct", "rel_strength_3m_pct",
    "rel_strength_vs_sector_1m_pct", "rel_strength_vs_sector_3m_pct",
    "rsi_14", "adx_14", "above_50dma", "above_200dma", "trend_up_50_over_200",
    "dist_from_52w_high_pct", "sector",
)


def compact_indicator_line(row: dict) -> dict:
    """The retained subset for a name that did not surface into a lane."""
    out = {k: row.get(k) for k in COMPACT_FIELDS if row.get(k) is not None}
    macd = row.get("macd")
    if isinstance(macd, dict) and macd.get("state"):
        out["macd_state"] = macd["state"]
    vs = row.get("volume_signal")
    if isinstance(vs, dict) and vs.get("read"):
        out["volume_read"] = vs["read"]
    ws = row.get("weekly")
    if isinstance(ws, dict):
        for k in ("above_30w_ma", "weekly_rsi_14", "weekly_structure"):
            if ws.get(k) is not None:
                out[k] = ws[k]
    av = row.get("anchored_vwap")
    if isinstance(av, dict) and av.get("status") == "ok":
        out["pct_vs_anchored_vwap"] = av.get("pct_vs_anchored_vwap")
    return out


def build_indicator_map(rows: list[dict]) -> dict:
    """{TICKER: compact line} for every scanned row.

    THE GAP THIS CLOSES: the scanner computed ~38 indicators for all ~184 names
    and retained them for the ~35 that surfaced into a lane. The other ~149 were
    reduced to a price and an ATR — including watchlist names the brain is
    required to rule drop/hold/buy on every run. It was being asked to judge
    names whose indicators had been computed and thrown away.
    """
    return {r["ticker"]: compact_indicator_line(r)
            for r in rows or [] if r.get("ticker")}
