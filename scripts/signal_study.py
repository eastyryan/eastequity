"""Generalized signal event study — point it at ANY lane, not just SEPA.

This is `scripts/sepa_study.py` with the one hard-coded seam removed. That script
measured Minervini's trend template over 182 names x 5 years and rejected it; the
machinery that made the rejection trustworthy is signal-agnostic, and roughly ten other
lanes ship to the decision-maker having never been measured at all. This is the same
harness with a `signal_fn` argument.

The four design choices from sepa_study.py carry over unchanged, because they are what
separate a measurement from a manufactured backtest:

1. CROSS-SECTIONAL, SAME-DAY. The metric is the selected group's return MINUS the
   same-day mean of every name scanned. data/universe.json is SURVIVORS — names curated
   in partly because they already worked. Absolute returns off that pool are meaningless.
   Comparing two groups from the SAME biased pool on the SAME day cancels most of it,
   along with market and sector beta. Never read the absolute column as edge.

2. NO LOOKAHEAD. The signal at bar t is computed from bars[:t+1]; the trade enters at
   bar t+1's OPEN. A signal computed on a close you could not have traded is the classic
   way to manufacture a result.

3. SCORED UNDER THE BOOK'S ACTUAL RISK RULES. `excursions_atr` applies a 2xATR initial
   stop plus a 3xATR chandelier trail that ratchets and never falls — what validator.py
   and the safety layer really do. The SEPA study's whole finding was that a +1.15pp
   naive fixed-horizon separation EVAPORATED here, because the gate was selecting
   higher-ATR names rather than better ones. Any lane that looks good naive and dies
   under stops is the exact trap this harness exists to catch, so both are always run.

4. OVERLAPPING SAMPLES ARE NOT INDEPENDENT. Sampling every `step` sessions with a 21- or
   63-session horizon means neighbouring observations share most of their window, and
   this universe is one correlated AI/semis theme besides. The effective sample is far
   below `n`, so this prints a per-year breakdown and REFUSES to print a p-value. Judge
   stability across years, not significance.

Two structural changes vs sepa_study.py, both forced by market-wide use:

  * SLICING MOVED INSIDE THE SIGNAL CALL. sepa_study stored `closes[:i+1]` and three
    more full-history slices on EVERY observation record. At 183 names x ~530 sample
    bars that is tens of gigabytes. `signal_fn` now receives the full arrays plus an
    index and does its own slicing; records keep only the small dict it returns.
  * ATR AT SIGNAL TIME IS ALWAYS RECORDED, for every signal, whether or not the signal
    asked for it. The volatility-selection check is not optional here — it is the single
    failure mode that has already fooled this repo once.

Usage:
  python scripts/signal_study.py --lanes            # the three scanner lanes
  python scripts/signal_study.py --lanes --years 12 --step 5
  python scripts/signal_study.py --ear              # earnings-reaction price leg
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import pickle
import sys
import time
import warnings
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HORIZONS = (5, 10, 21, 63)
MIN_BARS_TO_START = 260   # a real 200-DMA plus slope lookback before any signal


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))]


# --- data --------------------------------------------------------------------
def load_panel(tickers, years: int, cache_path=None, chunk: int = 15, sleep: float = 2.0):
    """Batched daily OHLCV for the universe + SPY. Returns {ticker: DataFrame}.

    Chunked and slept, unlike sepa_study.load_panel's single call: 183 symbols x 12y in
    one request gets rate-limited. `cache_path` makes re-running the study free, which
    matters because a lane study is not a one-shot — you re-slice it several times.
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return pickle.load(f)
    import yfinance as yf
    want = list(tickers) + ["SPY"]
    out = {}
    for k in range(0, len(want), chunk):
        grp = want[k:k + chunk]
        for attempt in range(4):
            try:
                df = yf.download(grp, period=f"{years}y", interval="1d",
                                 auto_adjust=True, group_by="ticker",
                                 progress=False, threads=True)
                for t in grp:
                    try:
                        sub = df[t].dropna()
                        if len(sub) > MIN_BARS_TO_START:
                            out[t] = sub
                    except Exception:
                        continue
                break
            except Exception:
                time.sleep(8 * (attempt + 1))
        time.sleep(sleep)
    if cache_path:
        with open(cache_path, "wb") as f:
            pickle.dump(out, f)
    return out


# --- scoring (unchanged from sepa_study.py:78-151) ---------------------------
def _atr(highs, lows, closes, i: int, period: int = 14):
    """Wilder-ish ATR at bar i from completed bars."""
    if i < period + 1:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        trs.append(max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]),
                       abs(lows[j] - closes[j - 1])))
    return sum(trs) / len(trs) if trs else None


def excursions_atr(opens, highs, lows, closes, i: int, h: int,
                   atr_mult: float = 2.0, trail_mult: float = 3.0):
    """The book's ACTUAL risk rule: a 2xATR initial stop plus a 3xATR chandelier trail
    that ratchets up on the high-water mark and never falls.

    This is the only test that matches validator.py and the safety layer. A fixed -8%
    stop sits INSIDE the noise band for high-ATR momentum names — which is exactly what
    CLAUDE.md's stop_engineering floor exists to prevent — so measuring a lane with one
    penalises the high-beta names it selects and tells you nothing about how this system
    would really trade it. Returns (return, mfe, mae, stopped_flag).
    """
    n = len(closes)
    if i + h >= n or i + 1 >= n:
        return None
    atr = _atr(highs, lows, closes, i)
    entry = opens[i + 1]
    if not atr or not entry:
        return None
    stop = entry - atr_mult * atr
    hwm = entry
    hi = lo = entry
    for j in range(i + 1, i + h + 1):
        hi, lo = max(hi, highs[j]), min(lo, lows[j])
        if lows[j] <= stop:
            return (stop / entry - 1, hi / entry - 1, lo / entry - 1, True)
        hwm = max(hwm, highs[j])
        stop = max(stop, hwm - trail_mult * atr)   # ratchets up only
    return (closes[i + h] / entry - 1, hi / entry - 1, lo / entry - 1, False)


def excursions(opens, highs, lows, closes, i: int, h: int, stop_pct: float = 0.08):
    """MFE/MAE and a stop-aware exit under a fixed -8% stop. Kept for comparison only:
    sepa_study found this stop stops out over half of a momentum signal's picks by 63d
    and inverts the result. Returns (mfe, mae, stopped_return)."""
    n = len(closes)
    if i + h >= n or i + 1 >= n:
        return None, None, None
    entry = opens[i + 1]
    if not entry:
        return None, None, None
    stop = entry * (1 - stop_pct)
    hi = lo = entry
    for j in range(i + 1, i + h + 1):
        hi, lo = max(hi, highs[j]), min(lo, lows[j])
        if lows[j] <= stop:
            return hi / entry - 1, lo / entry - 1, -stop_pct
    return hi / entry - 1, lo / entry - 1, closes[i + h] / entry - 1


def forward_return(opens, closes, i: int, h: int):
    """Open of bar i+1 (first tradeable price after the signal) to close of bar i+h."""
    n = len(closes)
    if i + h >= n or i + 1 >= n:
        return None
    entry = opens[i + 1]
    return (closes[i + h] / entry - 1) if entry else None


# --- the seam ----------------------------------------------------------------
class SignalContext(object):
    """What a signal_fn gets. Full arrays plus an index — NOT slices.

    sepa_study.py stored four full-history slices per observation record, which is fine
    for one universe x 5 years and fatal market-wide. The signal does its own slicing
    here and returns only small scalars, so a record's memory is O(1) in history length.

    `extra` carries per-ticker static context a lane needs and bars cannot give:
    ai_exposure labels, precomputed weekly-close positions. Anything in there is by
    definition NOT point-in-time — see tools/signal_lanes.py's disclosure of that.
    """

    __slots__ = ("ticker", "opens", "highs", "lows", "closes", "vols", "i",
                 "date", "spy_closes", "spy_i", "extra")

    def __init__(self, ticker, opens, highs, lows, closes, vols, i, date,
                 spy_closes, spy_i, extra):
        self.ticker, self.i, self.date = ticker, i, date
        self.opens, self.highs, self.lows = opens, highs, lows
        self.closes, self.vols = closes, vols
        self.spy_closes, self.spy_i, self.extra = spy_closes, spy_i, extra


def run(tickers, years: int, step: int, signal_fn, cross_fn=None,
        horizons=HORIZONS, panel=None, cache_path=None,
        min_bars=MIN_BARS_TO_START, extras=None, progress=False):
    """Walk-forward event study over `tickers` for an arbitrary `signal_fn`.

    Args:
        signal_fn: SignalContext -> dict of small scalars, or None to skip the bar.
                   Must read only ctx arrays up to and including ctx.i.
        cross_fn:  (date, [records]) -> None. Runs AFTER every name is evaluated for a
                   date and MUTATES the records. This is where rank-based conditions
                   live — RS percentiles, "top 5 by momentum", lane cascades. It is not
                   optional plumbing: three of the four lanes measured here are top-N
                   selections, and a lane scored without its rank step is a different
                   signal from the one that ships.
        extras:    {ticker: dict} of per-ticker static context handed to signal_fn.

    Returns a list of flat records: ticker, date, whatever signal_fn returned, plus
    fwd / exc / atr per horizon and `sig_atr_pct` (always, for the selection check).
    """
    panel = panel if panel is not None else load_panel(tickers, years, cache_path)
    spy = panel.get("SPY")
    wanted = {str(t).upper() for t in tickers} if tickers else None
    names = [t for t in panel
             if t != "SPY" and (wanted is None or t.upper() in wanted)]
    if not names or spy is None:
        return {"error": "no data"}

    # Align every name to SPY's calendar so a "date" means the same bar everywhere.
    dates = list(spy.index)
    date_pos = {d: k for k, d in enumerate(dates)}
    spy_closes = [float(x) for x in spy["Close"]]
    extras = extras or {}
    maxh = max(horizons)

    rows = []
    for ct, t in enumerate(names):
        b = panel[t].reindex(dates).dropna()
        if len(b) <= min_bars:
            continue
        o = [float(x) for x in b["Open"]]
        h = [float(x) for x in b["High"]]
        lo = [float(x) for x in b["Low"]]
        c = [float(x) for x in b["Close"]]
        v = [float(x) for x in b["Volume"]]
        idx = list(b.index)
        n = len(c)
        extra = dict(extras.get(t) or {})
        if progress and ct % 25 == 0:
            print("  ...%d/%d names" % (ct, len(names)), flush=True)

        for i in range(min_bars, n - maxh - 1, step):
            spy_i = date_pos.get(idx[i])
            if spy_i is None:
                continue
            ctx = SignalContext(t, o, h, lo, c, v, i, idx[i], spy_closes, spy_i, extra)
            feat = signal_fn(ctx)
            if feat is None:
                continue
            rec = {"ticker": t, "date": idx[i],
                   "sig_atr_pct": _sig_atr_pct(h, lo, c, i)}
            rec.update(feat)
            rec["fwd"] = {hz: forward_return(o, c, i, hz) for hz in horizons}
            rec["exc"] = {hz: excursions(o, h, lo, c, i, hz) for hz in horizons}
            rec["atr"] = {hz: excursions_atr(o, h, lo, c, i, hz) for hz in horizons}
            rows.append(rec)

    if cross_fn is not None:
        by_date = defaultdict(list)
        for r in rows:
            by_date[r["date"]].append(r)
        for date, recs in by_date.items():
            cross_fn(date, recs)
    return rows


def _sig_atr_pct(highs, lows, closes, i, period: int = 14):
    a = _atr(highs, lows, closes, i, period)
    return (a / closes[i]) if (a and closes[i]) else None


# --- summaries (signal-agnostic, as in sepa_study.py:220-283) ----------------
def summarize(rows, label: str, predicate, horizons=HORIZONS):
    """Mean forward return of the selected group MINUS the same-day universe mean."""
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    out = {}
    for hz in horizons:
        excess, hits, absolute = [], 0, []
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
            "p10_pct": round(_pct(srt, 0.10) * 100, 2),
            "p90_pct": round(_pct(srt, 0.90) * 100, 2),
        }
    return {"label": label, "horizons": out}


def summarize_stopped(rows, label: str, predicate, horizons=HORIZONS):
    """Scored under a fixed -8% hard stop, with MFE/MAE so the asymmetry claim becomes
    measurable rather than asserted."""
    out = {}
    for hz in horizons:
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
            "n": len(rets), "mean_pct": round(_mean(rets) * 100, 2),
            "win_rate_pct": round(len(wins) / len(rets) * 100, 1),
            "stopped_pct": round(stopped / len(rets) * 100, 1),
            "avg_win_pct": round(avg_w * 100, 2),
            "avg_loss_pct": round(avg_l * 100, 2),
            "payoff": round(abs(avg_w / avg_l), 2) if avg_l else None,
            "mfe_pct": round(_mean(mfes) * 100, 2),
            "mae_pct": round(_mean(maes) * 100, 2),
        }
    return {"label": label, "horizons": out}


def summarize_atr_rules(rows, label: str, predicate, horizons=(21, 63)):
    """THE table that decides a verdict: the book's own 2xATR stop + 3xATR chandelier.

    Reports excess vs the same-day universe mean under the SAME rules — every name in
    the pool is scored as if it had been traded that way — plus stop-out rate and
    MFE/MAE. sepa_study.py's rejection of SEPA lives entirely in this table.
    """
    out = {}
    for hz in horizons:
        by_date = defaultdict(list)
        for r in rows:
            if r["atr"][hz] is not None:
                by_date[r["date"]].append(r)
        rets, exc, mfes, maes, stops = [], [], [], [], 0
        for date, recs in by_date.items():
            allr = [x["atr"][hz][0] for x in recs]
            sel = [x for x in recs if predicate(x)]
            if not allr or not sel:
                continue
            base = _mean(allr)
            for x in sel:
                rets.append(x["atr"][hz][0])
                exc.append(x["atr"][hz][0] - base)
                mfes.append(x["atr"][hz][1])
                maes.append(x["atr"][hz][2])
                stops += 1 if x["atr"][hz][3] else 0
        if not rets:
            out[hz] = None
            continue
        wins = [x for x in rets if x > 0]
        losses = [x for x in rets if x <= 0]
        aw, al = (_mean(wins) or 0), (_mean(losses) or 0)
        out[hz] = {
            "n": len(rets), "mean_pct": round(_mean(rets) * 100, 2),
            "excess_mean_pct": round(_mean(exc) * 100, 2),
            "win_rate_pct": round(len(wins) / len(rets) * 100, 1),
            "stopped_pct": round(stops / len(rets) * 100, 1),
            "avg_win_pct": round(aw * 100, 2),
            "avg_loss_pct": round(al * 100, 2),
            "payoff": round(abs(aw / al), 2) if al else None,
            "mfe_pct": round(_mean(mfes) * 100, 2),
            "mae_pct": round(_mean(maes) * 100, 2),
        }
    return {"label": label, "horizons": out}


def summarize_atr_matched(rows, label: str, predicate, horizons=(21, 63), buckets=5):
    """Excess under the book's rules vs same-day, SAME-VOLATILITY-BUCKET peers.

    This is the SEPA failure mode turned into a control rather than an observation.
    `atr_selection_check` only tells you a lane picks higher-ATR names; it cannot tell
    you whether that is the whole story. Here each day's scanned names are split into
    ATR quintiles and a selected name is benchmarked ONLY against names of comparable
    volatility on that same day. If a lane's excess survives this, it is picking names
    that move BETTER. If it collapses toward zero, it was picking names that move MORE —
    which is precisely what the trend template turned out to be doing.
    """
    by_date = defaultdict(list)
    for r in rows:
        if r["atr"][horizons[0]] is not None and r["sig_atr_pct"]:
            by_date[r["date"]].append(r)
    out = {}
    for hz in horizons:
        exc = []
        for date, recs in by_date.items():
            usable = [r for r in recs if r["atr"][hz] is not None]
            if len(usable) < buckets * 4:
                continue
            usable.sort(key=lambda r: r["sig_atr_pct"])
            size = len(usable) / float(buckets)
            for b in range(buckets):
                grp = usable[int(b * size):int((b + 1) * size)]
                if len(grp) < 3:
                    continue
                base = _mean([r["atr"][hz][0] for r in grp])
                for r in grp:
                    if predicate(r):
                        exc.append(r["atr"][hz][0] - base)
        out[hz] = ({"n": len(exc),
                    "excess_mean_pct": round(_mean(exc) * 100, 2),
                    "excess_median_pct": round(_pct(exc, 0.5) * 100, 2)}
                   if exc else None)
    return {"label": label, "horizons": out}


def atr_selection_check(rows, label: str, predicate):
    """Does the lane merely pick higher-ATR names?

    This is the check that caught SEPA: a +1.15pp naive separation that was a
    VOLATILITY-SELECTION artifact — the gate picked names that move MORE, not names
    that move BETTER, and the difference vanished once a real ATR-width stop was
    applied. Any lane whose selected ATR distribution sits materially above the
    unselected one has to clear that suspicion before its naive numbers mean anything.
    """
    sel = [r["sig_atr_pct"] for r in rows if predicate(r) and r["sig_atr_pct"]]
    uns = [r["sig_atr_pct"] for r in rows if not predicate(r) and r["sig_atr_pct"]]
    if not sel or not uns:
        return None
    ms, mu = _mean(sel), _mean(uns)
    return {"label": label, "n_sel": len(sel), "n_unsel": len(uns),
            "atr_sel_pct": round(ms * 100, 2), "atr_unsel_pct": round(mu * 100, 2),
            "ratio": round(ms / mu, 3),
            "median_sel_pct": round(_pct(sel, 0.5) * 100, 2),
            "median_unsel_pct": round(_pct(uns, 0.5) * 100, 2)}


# --- the lane signal_fn ------------------------------------------------------
def make_lane_signal(top_n=15):
    """(signal_fn, cross_fn) for the three unmeasured scanner lanes.

    signal_fn builds the scan-row subset the lanes read; cross_fn replays the scanner's
    same-day lane CASCADE, so each record ends up carrying both `pred_<lane>` (the raw
    filter) and `sel_<lane>` (made the top 5 the brain actually sees).
    """
    from tools import signal_lanes as SL

    def signal_fn(ctx):
        wpos = ctx.extra.get("weekly_pos")
        wcl = ctx.extra.get("weekly_closes")
        wupto = None
        if wpos:
            k = bisect.bisect_right(wpos, ctx.i)
            wupto = wcl[:k] if k else None
        row = SL.lane_features(ctx.closes, ctx.highs, ctx.lows, ctx.vols, ctx.i,
                               spy_closes=ctx.spy_closes, spy_i=ctx.spy_i,
                               ai_exposure=ctx.extra.get("ai_exposure"),
                               weekly_closes_upto=wupto, ticker=ctx.ticker)
        if row is None:
            return None
        reaction, _read = SL.gap_hold_reaction(ctx.opens, ctx.closes, ctx.i)
        return {
            "row": row,
            "score": row["swing_setup_score"],
            "pred_contrarian_reversal": SL.contrarian_reversal_pred(row),
            "pred_deep_value_200w": SL.deep_value_200w_pred(row),
            "pred_supplier_pullback": SL.supplier_pullback_pred(row),
            "pred_pullback_no_label": SL.supplier_pullback_pred_no_label(row),
            "ear_reaction": reaction,
            "sel_top_setups": False, "sel_contrarian_reversal": False,
            "sel_deep_value_200w": False, "sel_supplier_pullback": False,
        }

    def cross_fn(date, recs):
        lanes = SL.select_lanes([r["row"] for r in recs], top_n=top_n)
        by_row = {id(r["row"]): r for r in recs}
        for lane, picked in lanes.items():
            for row in picked:
                rec = by_row.get(id(row))
                if rec is not None:
                    rec["sel_" + lane] = True
        for r in recs:
            r.pop("row", None)          # drop the row once ranking is done: memory

    return signal_fn, cross_fn


def build_extras(panel, tickers):
    """Per-ticker static context: ai_exposure labels + precomputed weekly closes."""
    from tools import signal_lanes as SL
    try:
        labels = json.loads((ROOT / "data" / "ai_exposure.json").read_text())["labels"]
    except Exception:
        labels = {}
    extras = {}
    for t in tickers:
        df = panel.get(t)
        e = {"ai_exposure": (labels.get(t.upper()) or {}).get("exposure")}
        if df is not None:
            wc, wp = SL.weekly_closes(list(df.index), [float(x) for x in df["Close"]])
            e["weekly_closes"], e["weekly_pos"] = wc, wp
        extras[t] = e
    return extras


# --- reporting ---------------------------------------------------------------
def report(rows, groups, horizons=HORIZONS, stability_horizon=21):
    print("observations=%d dates=%d names=%d"
          % (len(rows), len({r["date"] for r in rows}),
             len({r["ticker"] for r in rows})))

    print("\n=== 1. NAIVE fixed-horizon: excess vs same-day universe mean (pp) ===")
    print("%-28s%4s%8s%9s%8s%7s%8s%8s"
          % ("group", "h", "n", "excess", "median", "beat%", "p10", "p90"))
    for lbl, pred in groups:
        res = summarize(rows, lbl, pred, horizons)
        for hz in horizons:
            d = res["horizons"].get(hz)
            if not d:
                continue
            print("%-28s%4d%8d%9.2f%8.2f%7.1f%8.2f%8.2f"
                  % (lbl, hz, d["n"], d["excess_mean_pct"], d["excess_median_pct"],
                     d["beat_universe_pct"], d["p10_pct"], d["p90_pct"]))
        print()

    print("=== 2. THE BOOK'S REAL RULES: 2xATR stop + 3xATR chandelier trail ===")
    print("%-28s%4s%8s%8s%9s%7s%7s%8s%8s%8s%8s%8s"
          % ("group", "h", "n", "mean", "excess", "win%", "stop%", "avgW", "avgL",
             "payoff", "MFE", "MAE"))
    for lbl, pred in groups:
        res = summarize_atr_rules(rows, lbl, pred)
        for hz in (21, 63):
            d = res["horizons"].get(hz)
            if not d:
                continue
            pay = ("%8.2f" % d["payoff"]) if d["payoff"] is not None else "%8s" % "-"
            print("%-28s%4d%8d%8.2f%9.2f%7.1f%7.1f%8.2f%8.2f%s%8.2f%8.2f"
                  % (lbl, hz, d["n"], d["mean_pct"], d["excess_mean_pct"],
                     d["win_rate_pct"], d["stopped_pct"], d["avg_win_pct"],
                     d["avg_loss_pct"], pay, d["mfe_pct"], d["mae_pct"]))
        print()

    print("=== 3. fixed -8% stop (for comparison only; inside the noise band) ===")
    print("%-28s%4s%8s%8s%7s%7s%8s%8s%8s"
          % ("group", "h", "n", "mean", "win%", "stop%", "avgW", "avgL", "payoff"))
    for lbl, pred in groups:
        res = summarize_stopped(rows, lbl, pred, (21, 63))
        for hz in (21, 63):
            d = res["horizons"].get(hz)
            if not d:
                continue
            pay = ("%8.2f" % d["payoff"]) if d["payoff"] is not None else "%8s" % "-"
            print("%-28s%4d%8d%8.2f%7.1f%7.1f%8.2f%8.2f%s"
                  % (lbl, hz, d["n"], d["mean_pct"], d["win_rate_pct"],
                     d["stopped_pct"], d["avg_win_pct"], d["avg_loss_pct"], pay))
    print()

    print("=== 4. VOLATILITY-SELECTION CHECK (the failure mode that killed SEPA) ===")
    print("%-28s%9s%9s%10s%12s%8s"
          % ("group", "n_sel", "n_unsel", "ATR%_sel", "ATR%_unsel", "ratio"))
    for lbl, pred in groups:
        d = atr_selection_check(rows, lbl, pred)
        if d:
            print("%-28s%9d%9d%10.2f%12.2f%8.3f"
                  % (lbl, d["n_sel"], d["n_unsel"], d["atr_sel_pct"],
                     d["atr_unsel_pct"], d["ratio"]))
    print()

    print("=== 4b. ATR-MATCHED excess, BOOK'S RULES (vs same-day same-volatility "
          "peers) ===")
    print("%-28s%4s%9s%10s%10s" % ("group", "h", "n", "excess", "median"))
    for lbl, pred in groups:
        res = summarize_atr_matched(rows, lbl, pred)
        for hz in (21, 63):
            d = res["horizons"].get(hz)
            if d:
                print("%-28s%4d%9d%10.2f%10.2f"
                      % (lbl, hz, d["n"], d["excess_mean_pct"], d["excess_median_pct"]))
    print()

    print("=== 5. %dd excess by year, BOOK'S RULES (stability: a one-regime effect "
          "is a regime, not an edge) ===" % stability_horizon)
    years = sorted({r["date"].year for r in rows})
    print("%-28s" % "group" + "".join("%9d" % y for y in years))
    for lbl, pred in groups:
        cells = []
        for y in years:
            sub = [r for r in rows if r["date"].year == y]
            s = summarize_atr_rules(sub, lbl, pred, (stability_horizon,))
            d = s["horizons"].get(stability_horizon)
            cells.append("%9.2f" % d["excess_mean_pct"] if d else "%9s" % "-")
        print("%-28s" % lbl + "".join(cells))

    print("\nNOTE: overlapping windows + one correlated theme -> the effective sample is")
    print("far below n. Judge year-to-year stability, not the pooled mean. No p-value is")
    print("printed on purpose: the independence assumption behind one does not hold.")
    print("data/universe.json is SURVIVORS-ONLY; read excess columns, never absolute.")


LANE_GROUPS = [
    ("contrarian (raw pred)", lambda r: r.get("pred_contrarian_reversal") is True),
    ("contrarian (top5 shipped)", lambda r: r.get("sel_contrarian_reversal") is True),
    ("deep_value (raw pred)", lambda r: r.get("pred_deep_value_200w") is True),
    ("deep_value (top5 shipped)", lambda r: r.get("sel_deep_value_200w") is True),
    ("supplier_pb (raw pred)", lambda r: r.get("pred_supplier_pullback") is True),
    ("supplier_pb (top5 shipped)", lambda r: r.get("sel_supplier_pullback") is True),
    ("pullback ABLATION (no label)", lambda r: r.get("pred_pullback_no_label") is True),
    ("top_setups (score top15)", lambda r: r.get("sel_top_setups") is True),
    ("EAR gap_held (price leg)", lambda r: r.get("ear_reaction") == "gap_held"),
    ("EAR gap_faded", lambda r: r.get("ear_reaction") == "gap_faded"),
    ("EAR gap_filled", lambda r: r.get("ear_reaction") == "gap_filled"),
    ("EAR down_gap", lambda r: r.get("ear_reaction") == "down_gap"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=12)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="cap universe size")
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--cache", default="", help="pickle path for the price panel")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--lanes", action="store_true", help="(default) the scanner lanes")
    args = ap.parse_args()

    sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
    tickers = sorted({t for v in sectors.values() for t in v})
    if args.limit:
        tickers = tickers[:args.limit]

    print("universe=%d years=%d step=%d top_n=%d"
          % (len(tickers), args.years, args.step, args.top_n))
    panel = load_panel(tickers, args.years, args.cache or None)
    print("panel names=%d" % len(panel))
    extras = build_extras(panel, tickers)
    signal_fn, cross_fn = make_lane_signal(top_n=args.top_n)
    rows = run(tickers, args.years, args.step, signal_fn, cross_fn=cross_fn,
               panel=panel, extras=extras, progress=True)
    if isinstance(rows, dict):
        print(rows)
        return
    report(rows, LANE_GROUPS)

    if args.json_out:
        blob = {}
        for lbl, pred in LANE_GROUPS:
            blob[lbl] = {
                "naive": summarize(rows, lbl, pred)["horizons"],
                "book_rules": summarize_atr_rules(rows, lbl, pred)["horizons"],
                "hard_stop_8pct": summarize_stopped(rows, lbl, pred, (21, 63))["horizons"],
                "atr_selection": atr_selection_check(rows, lbl, pred),
                "atr_matched": summarize_atr_matched(rows, lbl, pred)["horizons"],
                "by_year_21d": {
                    y: (summarize_atr_rules([r for r in rows if r["date"].year == y],
                                            lbl, pred, (21,))["horizons"].get(21) or {})
                    for y in sorted({r["date"].year for r in rows})},
            }
        Path(args.json_out).write_text(json.dumps(blob, indent=2, default=str))
        print("wrote", args.json_out)


if __name__ == "__main__":
    main()
