"""SEPA event study — does the trend template actually separate forward returns?

This is MEASUREMENT, not fitting. It answers one question at Minervini's published
parameters: on a given day, did the names passing the gate outperform the names
failing it, over the following 5/10/21/63 sessions?

Three design choices carry the whole result:

1. CROSS-SECTIONAL, SAME-DAY. The metric is passing-group return MINUS the
   same-day mean of every name scanned. data/universe.json has no point-in-time
   history (git only goes back days), so the pool is today's SURVIVORS — names
   curated in partly because they already worked. Absolute returns off that pool
   are meaningless; NVDA is in the universe because it went up. Comparing two
   groups drawn from the SAME biased pool on the SAME day cancels most of it,
   along with market and sector beta. Never read the absolute column as edge.

2. NO LOOKAHEAD. The signal at bar t is computed from bars[:t+1]; the trade is
   entered at bar t+1's OPEN and held h sessions. A signal computed on a close you
   could not have traded is the classic way to manufacture a backtest.

3. OVERLAPPING SAMPLES ARE NOT INDEPENDENT. Sampling every 5 sessions with a
   21-session horizon means each observation shares ~4/5 of its window with its
   neighbours, and the names are one correlated AI/semis theme besides. The true
   independent sample is far smaller than `n`, so this script prints a block
   breakdown by year and REFUSES to print a p-value. Judge stability across years,
   not significance.

Usage:
  python scripts/sepa_study.py                 # default 4y, every 5th session
  python scripts/sepa_study.py --years 5 --step 5 --limit 60
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

from tools.sepa_trend import (  # noqa: E402
    MIN_RS_PERCENTILE, detect_vcp, percentile_rank, trend_template,
)

HORIZONS = (5, 10, 21, 63)
MIN_BARS_TO_START = 260   # need a real 200-DMA plus slope lookback before any signal


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def load_panel(tickers, years: int):
    """Batched daily OHLCV for the universe + SPY. Returns {ticker: DataFrame}."""
    df = yf.download(list(tickers) + ["SPY"], period=f"{years}y", interval="1d",
                     auto_adjust=True, group_by="ticker", progress=False,
                     threads=True)
    out = {}
    for t in list(tickers) + ["SPY"]:
        try:
            sub = df[t].dropna()
            if len(sub) > MIN_BARS_TO_START:
                out[t] = sub
        except Exception:
            continue
    return out


def _atr(bars, i: int, period: int = 14):
    """Wilder-ish ATR at bar i from completed bars. Pure enough for this harness."""
    if i < period + 1:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        hi, lo = float(bars["High"].iloc[j]), float(bars["Low"].iloc[j])
        pc = float(bars["Close"].iloc[j - 1])
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    return sum(trs) / len(trs) if trs else None


def excursions_atr(bars, i: int, h: int, atr_mult: float = 2.0,
                   trail_mult: float = 3.0):
    """The book's ACTUAL risk rule: an ATR-width initial stop plus a 3xATR chandelier
    trail that ratchets up on the high-water mark and never falls.

    This is the only test that matches validator.py. A fixed -8% stop (see excursions)
    sits INSIDE the noise band for high-ATR momentum names, which is exactly what
    CLAUDE.md's stop_engineering floor exists to prevent -- so measuring SEPA with one
    penalises the high-beta names the gate selects and tells you nothing about how this
    system would actually trade the signal.
    """
    if i + h >= len(bars) or i + 1 >= len(bars):
        return None
    atr = _atr(bars, i)
    entry = float(bars["Open"].iloc[i + 1])
    if not atr or not entry:
        return None
    stop = entry - atr_mult * atr
    hwm = entry
    for j in range(i + 1, i + h + 1):
        b_hi, b_lo = float(bars["High"].iloc[j]), float(bars["Low"].iloc[j])
        if b_lo <= stop:
            return stop / entry - 1
        hwm = max(hwm, b_hi)
        stop = max(stop, hwm - trail_mult * atr)   # ratchets up only
    return float(bars["Close"].iloc[i + h]) / entry - 1


def excursions(bars, i: int, h: int, stop_pct: float = 0.08):
    """MFE/MAE and a stop-aware exit over the h sessions after the signal.

    A fixed-horizon return systematically MISJUDGES SEPA: the methodology's entire
    premise is a hard -7-8% stop with scale-outs, so its edge lives in truncating
    the left tail, not in the raw h-day return. Testing a breakout entry without a
    stop measures a strategy nobody runs. Returns (mfe, mae, stopped_return) where
    stopped_return exits at the stop level on the first bar whose LOW breaches it.
    """
    if i + h >= len(bars) or i + 1 >= len(bars):
        return None, None, None
    entry = float(bars["Open"].iloc[i + 1])
    if not entry:
        return None, None, None
    stop = entry * (1 - stop_pct)
    hi = lo = entry
    for j in range(i + 1, i + h + 1):
        b_hi, b_lo = float(bars["High"].iloc[j]), float(bars["Low"].iloc[j])
        hi, lo = max(hi, b_hi), min(lo, b_lo)
        if b_lo <= stop:
            return hi / entry - 1, lo / entry - 1, -stop_pct
    return hi / entry - 1, lo / entry - 1, float(bars["Close"].iloc[i + h]) / entry - 1


def forward_return(bars, i: int, h: int):
    """Return from the OPEN of bar i+1 (the first tradeable price after the signal)
    to the CLOSE of bar i+h. None when the window runs past the data."""
    if i + h >= len(bars) or i + 1 >= len(bars):
        return None
    entry = float(bars["Open"].iloc[i + 1])
    exit_ = float(bars["Close"].iloc[i + h])
    if not entry:
        return None
    return exit_ / entry - 1


def run(tickers, years: int, step: int):
    panel = load_panel(tickers, years)
    spy = panel.get("SPY")
    names = [t for t in panel if t != "SPY"]
    if not names or spy is None:
        return {"error": "no data"}

    # Align every name to SPY's calendar so a "date" means the same bar everywhere.
    dates = list(spy.index)
    series = {}
    for t in names:
        b = panel[t].reindex(dates).dropna()
        if len(b) > MIN_BARS_TO_START:
            series[t] = b

    # obs[date] = list of per-name records evaluated on that date.
    obs = defaultdict(list)
    for t, bars in series.items():
        closes = [float(x) for x in bars["Close"]]
        highs = [float(x) for x in bars["High"]]
        lows = [float(x) for x in bars["Low"]]
        vols = [float(x) for x in bars["Volume"]]
        n = len(closes)
        for i in range(MIN_BARS_TO_START, n - max(HORIZONS) - 1, step):
            c, h, l, v = closes[:i + 1], highs[:i + 1], lows[:i + 1], vols[:i + 1]
            # 3-month relative strength vs SPY, for the RS percentile rank below.
            if i - 63 < 0:
                continue
            name_ret = closes[i] / closes[i - 63] - 1
            try:
                si = spy.index.get_loc(bars.index[i])
                spy_c = [float(x) for x in spy["Close"]]
                bench = spy_c[si] / spy_c[si - 63] - 1
            except Exception:
                continue
            rec = {
                "ticker": t, "i": i, "rs3m": (name_ret - bench) * 100,
                "closes": c, "highs": h, "lows": l, "vols": v,
                "fwd": {hz: forward_return(bars, i, hz) for hz in HORIZONS},
                "exc": {hz: excursions(bars, i, hz) for hz in HORIZONS},
                "atr": {hz: excursions_atr(bars, i, hz) for hz in HORIZONS},
            }
            obs[bars.index[i]].append(rec)

    # Per date: rank RS cross-sectionally (condition 8), then evaluate the gate.
    rows = []
    for date, recs in obs.items():
        if len(recs) < 20:            # a percentile over a handful of names is noise
            continue
        pop = [r["rs3m"] for r in recs]
        for r in recs:
            pct = percentile_rank(r["rs3m"], pop)
            tt = trend_template(r["closes"], r["highs"], r["lows"], rs_percentile=pct)
            vcp = detect_vcp(r["highs"], r["lows"], r["closes"], r["vols"])
            rows.append({
                "date": date, "ticker": r["ticker"],
                "pass": tt["template_pass"],
                "passed_n": tt["passed"],
                "rs_pct": pct,
                "vcp": bool(vcp.get("vcp_detected")),
                "buy_zone": bool(vcp.get("in_buy_zone")),
                "fwd": r["fwd"], "exc": r["exc"], "atr": r["atr"],
            })
    return rows


def summarize(rows, label: str, predicate):
    """Mean forward return of the selected group MINUS the same-day universe mean."""
    out = {}
    for hz in HORIZONS:
        excess, hits, absolute = [], 0, []
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["date"]].append(r)
        for date, recs in by_date.items():
            allr = [r["fwd"][hz] for r in recs if r["fwd"][hz] is not None]
            sel = [r["fwd"][hz] for r in recs
                   if predicate(r) and r["fwd"][hz] is not None]
            if not allr or not sel:
                continue
            base = _mean(allr)
            for s in sel:
                excess.append(s - base)
                absolute.append(s)
                hits += 1 if s > base else 0
        if not excess:
            out[hz] = None
            continue
        srt = sorted(excess)
        out[hz] = {
            "n": len(excess),
            "excess_mean_pct": round(_mean(excess) * 100, 2),
            "excess_median_pct": round(srt[len(srt) // 2] * 100, 2),
            "absolute_mean_pct": round(_mean(absolute) * 100, 2),
            "beat_universe_pct": round(hits / len(excess) * 100, 1),
            "p10_pct": round(srt[int(len(srt) * 0.10)] * 100, 2),
            "p90_pct": round(srt[int(len(srt) * 0.90)] * 100, 2),
        }
    return {"label": label, "horizons": out}


def summarize_stopped(rows, label: str, predicate):
    """Same groups, scored the way SEPA is actually traded: -8% hard stop. Also
    reports MFE/MAE so the asymmetry claim (small losses, large gains) becomes
    measurable rather than asserted."""
    out = {}
    for hz in HORIZONS:
        sel = [r for r in rows if predicate(r) and r["exc"][hz][2] is not None]
        if not sel:
            out[hz] = None
            continue
        rets = [r["exc"][hz][2] for r in sel]
        mfes = [r["exc"][hz][0] for r in sel]
        maes = [r["exc"][hz][1] for r in sel]
        stopped = sum(1 for x in rets if x <= -0.0799)
        wins = [x for x in rets if x > 0]
        losses = [x for x in rets if x <= 0]
        avg_w, avg_l = (_mean(wins) or 0), (_mean(losses) or 0)
        out[hz] = {
            "n": len(rets),
            "mean_pct": round(_mean(rets) * 100, 2),
            "win_rate_pct": round(len(wins) / len(rets) * 100, 1),
            "stopped_pct": round(stopped / len(rets) * 100, 1),
            "avg_win_pct": round(avg_w * 100, 2),
            "avg_loss_pct": round(avg_l * 100, 2),
            "payoff": round(abs(avg_w / avg_l), 2) if avg_l else None,
            "mfe_pct": round(_mean(mfes) * 100, 2),
            "mae_pct": round(_mean(maes) * 100, 2),
        }
    return {"label": label, "horizons": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=4)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="cap universe size")
    args = ap.parse_args()

    sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
    tickers = sorted({t for v in sectors.values() for t in v})
    if args.limit:
        tickers = tickers[:args.limit]

    print(f"universe={len(tickers)} years={args.years} step={args.step}")
    rows = run(tickers, args.years, args.step)
    if isinstance(rows, dict):
        print(rows)
        return
    print(f"observations={len(rows)} dates={len({r['date'] for r in rows})}")

    groups = [
        ("template PASS", lambda r: r["pass"] is True),
        ("template FAIL", lambda r: r["pass"] is False),
        ("PASS + VCP", lambda r: r["pass"] is True and r["vcp"]),
        ("PASS + VCP + buy zone", lambda r: r["pass"] is True and r["buy_zone"]),
        ("7 of 8 (near miss)", lambda r: r["pass"] is False and r["passed_n"] == 7),
        ("high RS only (no gate)", lambda r: r["rs_pct"] is not None
         and r["rs_pct"] > MIN_RS_PERCENTILE),
    ]
    results = [summarize(rows, lbl, pred) for lbl, pred in groups]

    print("\n=== excess return vs same-day universe mean (pct points) ===")
    hdr = f"{'group':24s}{'h':>4s}{'n':>7s}{'excess':>9s}{'median':>8s}{'beat%':>7s}{'p10':>8s}{'p90':>8s}"
    print(hdr)
    for res in results:
        for hz, d in res["horizons"].items():
            if not d:
                continue
            print(f"{res['label']:24s}{hz:>4d}{d['n']:>7d}"
                  f"{d['excess_mean_pct']:>9.2f}{d['excess_median_pct']:>8.2f}"
                  f"{d['beat_universe_pct']:>7.1f}{d['p10_pct']:>8.2f}{d['p90_pct']:>8.2f}")
        print()

    print("=== traded with an -8% hard stop (SEPA's actual risk rule) ===")
    print(f"{'group':24s}{'h':>4s}{'n':>7s}{'mean':>8s}{'win%':>7s}{'stop%':>7s}"
          f"{'avgW':>8s}{'avgL':>8s}{'payoff':>8s}{'MFE':>7s}{'MAE':>7s}")
    for lbl, pred in groups:
        st_res = summarize_stopped(rows, lbl, pred)
        for hz, d in st_res["horizons"].items():
            if not d or hz not in (21, 63):
                continue
            pay = f"{d['payoff']:>8.2f}" if d['payoff'] is not None else f"{'-':>8s}"
            print(f"{lbl:24s}{hz:>4d}{d['n']:>7d}{d['mean_pct']:>8.2f}"
                  f"{d['win_rate_pct']:>7.1f}{d['stopped_pct']:>7.1f}"
                  f"{d['avg_win_pct']:>8.2f}{d['avg_loss_pct']:>8.2f}"
                  f"{pay}{d['mfe_pct']:>7.2f}{d['mae_pct']:>7.2f}")
    print()

    print("=== traded the BOOK'S way: 2xATR stop + 3xATR chandelier trail ===")
    print(f"{'group':24s}{'h':>4s}{'n':>7s}{'mean':>8s}{'excess':>9s}{'win%':>7s}{'avgW':>8s}{'avgL':>8s}{'payoff':>8s}")
    for lbl, pred in groups:
        for hz in (21, 63):
            by_date = defaultdict(list)
            for r in rows:
                if r["atr"][hz] is not None:
                    by_date[r["date"]].append(r)
            rets, exc = [], []
            for date, recs in by_date.items():
                allr = [x["atr"][hz] for x in recs]
                sel = [x["atr"][hz] for x in recs if pred(x)]
                if not allr or not sel:
                    continue
                base = _mean(allr)
                rets += sel
                exc += [x - base for x in sel]
            if not rets:
                continue
            wins = [x for x in rets if x > 0]
            losses = [x for x in rets if x <= 0]
            aw, al = (_mean(wins) or 0), (_mean(losses) or 0)
            pay = f"{abs(aw/al):>8.2f}" if al else f"{'-':>8s}"
            print(f"{lbl:24s}{hz:>4d}{len(rets):>7d}{_mean(rets)*100:>8.2f}"
                  f"{_mean(exc)*100:>9.2f}{len(wins)/len(rets)*100:>7.1f}"
                  f"{aw*100:>8.2f}{al*100:>8.2f}{pay}")
    print()

    # Stability by year — the real test. An effect that only exists in one regime
    # is a regime, not an edge.
    print("=== 21-session excess by year (stability check) ===")
    years = sorted({r["date"].year for r in rows})
    print(f"{'group':24s}" + "".join(f"{y:>9d}" for y in years))
    for lbl, pred in groups[:4]:
        cells = []
        for y in years:
            sub = [r for r in rows if r["date"].year == y]
            s = summarize(sub, lbl, pred)["horizons"].get(21)
            cells.append(f"{s['excess_mean_pct']:>9.2f}" if s else f"{'-':>9s}")
        print(f"{lbl:24s}" + "".join(cells))

    print("\nNOTE: overlapping windows + one correlated theme -> effective sample is")
    print("far below n. Judge year-to-year stability, not the pooled mean. No p-value")
    print("is printed on purpose: the independence assumption behind one does not hold.")


if __name__ == "__main__":
    main()
