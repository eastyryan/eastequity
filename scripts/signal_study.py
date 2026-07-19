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
    from tools import technicals as TA

    def signal_fn(ctx):
        wpos = ctx.extra.get("weekly_pos")
        wcl = ctx.extra.get("weekly_closes")
        wupto, k = None, 0
        if wpos:
            k = bisect.bisect_right(wpos, ctx.i)
            wupto = wcl[:k] if k else None
        row = SL.lane_features(ctx.closes, ctx.highs, ctx.lows, ctx.vols, ctx.i,
                               spy_closes=ctx.spy_closes, spy_i=ctx.spy_i,
                               ai_exposure=ctx.extra.get("ai_exposure"),
                               weekly_closes_upto=wupto, ticker=ctx.ticker)
        if row is None:
            return None
        # lane_features does not carry a sector (the scanner sets it from its own
        # ticker_sector map). cross_fn's sector-RS passes key off these.
        row["real_sector"] = ctx.extra.get("sector")
        row["pseudo_sector"] = ctx.extra.get("pseudo_sector")
        reaction, _read = SL.gap_hold_reaction(ctx.opens, ctx.closes, ctx.i)

        # --- anchored VWAP, evaluated AT bar i ------------------------------
        # technicals.anchored_vwap_block treats the LAST element of what it is
        # given as "now", so it must be handed a slice ending at i. The slice is
        # also bounded on the left: find_anchor_index looks back at most
        # ANCHOR_MAX_SESSIONS (120) bars, so nothing older can be reached and a
        # full-history slice would only pay to copy bars the function cannot see.
        lo_i = max(0, ctx.i - (TA.ANCHOR_MAX_SESSIONS + 20))
        hi_i = ctx.i + 1
        av = TA.anchored_vwap_block(ctx.closes[lo_i:hi_i], ctx.highs[lo_i:hi_i],
                                    ctx.lows[lo_i:hi_i], ctx.vols[lo_i:hi_i])
        av_ok = av.get("status") == "ok"

        # --- weekly structure, using only CLOSED weeks as of bar i ----------
        wk = {}
        if wpos and k:
            wk = TA.weekly_structure(
                wcl[:k],
                (ctx.extra.get("weekly_highs") or [])[:k],
                (ctx.extra.get("weekly_lows") or [])[:k])

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
            # --- the four 32fbac9 technical layers, under measurement -------
            "avwap_status": av.get("status"),
            "avwap_above": av.get("above_anchored_vwap") if av_ok else None,
            "avwap_pct": av.get("pct_vs_anchored_vwap") if av_ok else None,
            "avwap_anchor": av.get("anchor_reason") if av_ok else None,
            "wk_structure": wk.get("weekly_structure"),
            "wk_above_30w": wk.get("above_30w_ma"),
            "wk_30w_rising": wk.get("wma_30w_rising"),
            "wk_rsi": wk.get("weekly_rsi_14"),
            # The daily trend flag these two are supposed to ADD to. Every
            # marginal-value group below conditions on it, so a "weekly uptrend"
            # edge cannot be just "daily uptrend" measured a second time.
            "daily_above_200dma": row.get("above_200dma"),
            "rs_spy_1m": row.get("rel_strength_1m_pct"),
            # filled by cross_fn — they need the whole day's pool
            "rs_sector_1m": None, "rs_pseudo_1m": None,
            "sel_rs_spy_top": False, "sel_rs_sector_top": False,
            "sel_rs_pseudo_top": False, "sel_rs_sector_only": False,
            "sel_rs_spy_only": False,
        }

    def cross_fn(date, recs):
        rows = [r["row"] for r in recs]
        lanes = SL.select_lanes(rows, top_n=top_n)
        by_row = {id(r["row"]): r for r in recs}
        for lane, picked in lanes.items():
            for row in picked:
                rec = by_row.get(id(row))
                if rec is not None:
                    rec["sel_" + lane] = True

        # --- RS vs SECTOR vs RS vs SPY: the comparison, done fairly ---------
        # attach_sector_relative_strength is cross-sectional by construction — it
        # needs the whole day's pool, which is exactly what cross_fn holds. It
        # keys off row["sector"] and mutates in place, so each grouping is run as
        # its own pass and harvested before the next one overwrites the key.
        # Both passes score the SAME observations, so the ablation is a like-for-
        # like comparison rather than two separate runs.
        def _rs_pass(group_key):
            for r in rows:
                r["sector"] = r.get(group_key)
                r.pop("rel_strength_vs_sector_1m_pct", None)
                r.pop("rel_strength_vs_sector_3m_pct", None)
            TA.attach_sector_relative_strength(rows)
            return [r.get("rel_strength_vs_sector_1m_pct") for r in rows]

        real_rs = _rs_pass("real_sector")
        pseudo_rs = _rs_pass("pseudo_sector")
        for rec, real_v, pseudo_v in zip(recs, real_rs, pseudo_rs):
            rec["rs_sector_1m"] = real_v
            rec["rs_pseudo_1m"] = pseudo_v

        # THE POOL MUST BE IDENTICAL or the comparison is confounded: a name with
        # no sector RS (thin sector, <3 scored peers) must be excluded from the
        # SPY arm too, otherwise the two arms rank different universes and any
        # difference could be composition rather than signal.
        pool = [r for r in recs
                if r.get("rs_spy_1m") is not None
                and r.get("rs_sector_1m") is not None
                and r.get("rs_pseudo_1m") is not None]
        if len(pool) >= 20:
            cut = max(1, len(pool) // 5)          # top quintile, equal n per arm
            for key, sel in (("rs_spy_1m", "sel_rs_spy_top"),
                             ("rs_sector_1m", "sel_rs_sector_top"),
                             ("rs_pseudo_1m", "sel_rs_pseudo_top")):
                for r in sorted(pool, key=lambda x: x[key], reverse=True)[:cut]:
                    r[sel] = True
            # The names each metric uniquely surfaces. This is the incremental
            # question — not "is sector RS good" but "does it find anything the
            # SPY version does not already find".
            for r in pool:
                r["sel_rs_sector_only"] = (r["sel_rs_sector_top"]
                                           and not r["sel_rs_spy_top"])
                r["sel_rs_spy_only"] = (r["sel_rs_spy_top"]
                                        and not r["sel_rs_sector_top"])

        for r in recs:
            r.pop("row", None)          # drop the row once ranking is done: memory

    return signal_fn, cross_fn


def pseudo_sectors(sectors: dict) -> dict:
    """A deterministic ABLATION grouping: same shape, no sector information.

    data/universe.json's sector labels are a 2026 classification applied to
    historical bars — the same lookahead hole that made supplier_pullbacks look
    like a +0.47 edge until the ai_supplier label was removed and it collapsed to
    +0.07. Sector membership is far more stable than an AI-exposure label, so the
    contamination is milder, but "milder" is not "measured".

    So rel_strength_vs_sector gets an ablation the way supplier_pullbacks got one.
    This assigns every ticker to an arbitrary group, preserving the number of
    groups and their sizes exactly, by dealing the alphabetical ticker list round
    robin into buckets of the real sizes. If relative strength measured against
    THIS grouping scores like relative strength against the real one, then what
    the metric captures is "beat an arbitrary cohort of your peers" — i.e. a
    restatement of cross-sectional momentum — and the sector labels carry nothing.

    Deterministic on purpose: no shuffling, so the control is reproducible across
    runs and a verdict can be re-derived rather than re-rolled.
    """
    names, order = sorted({t for v in sectors.values() for t in v}), sorted(sectors)
    sizes = [(s, len(set(sectors[s]))) for s in order]
    out, k = {}, 0
    for s, n in sizes:
        for t in names[k:k + n]:
            out[t] = "pseudo_" + s
        k += n
    for t in names[k:]:
        out[t] = "pseudo_" + order[-1]
    return out


def build_extras(panel, tickers, sectors=None):
    """Per-ticker static context: ai_exposure, sector, and weekly OHLC bars.

    Everything in here is NOT point-in-time — today's labels applied to old bars.
    ai_exposure already carried that disclosure; `sector` inherits it, which is
    why pseudo_sectors() exists as its control.
    """
    from tools import signal_lanes as SL
    try:
        labels = json.loads((ROOT / "data" / "ai_exposure.json").read_text())["labels"]
    except Exception:
        labels = {}
    sectors = sectors or {}
    by_ticker = {t: s for s, names in sectors.items() for t in names}
    pseudo = pseudo_sectors(sectors) if sectors else {}
    extras = {}
    for t in tickers:
        df = panel.get(t)
        e = {"ai_exposure": (labels.get(t.upper()) or {}).get("exposure"),
             "sector": by_ticker.get(t.upper()),
             "pseudo_sector": pseudo.get(t.upper())}
        if df is not None:
            idx = list(df.index)
            wc, wh, wl, wp = SL.weekly_ohlc(
                idx,
                [float(x) for x in df["Open"]], [float(x) for x in df["High"]],
                [float(x) for x in df["Low"]], [float(x) for x in df["Close"]])
            e["weekly_closes"], e["weekly_pos"] = wc, wp
            e["weekly_highs"], e["weekly_lows"] = wh, wl
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

# ---------------------------------------------------------------------------
# The four technical layers added in 32fbac9, none of which had been measured.
#
# The bar is what supplier_pullbacks failed: an edge must survive an ATR-matched
# control AND an ablation that removes only the thing being claimed. That lane
# looked like +0.47pp until the ai_supplier label came out of the predicate and
# it fell to +0.07 — "well-established" is exactly what it looked like first.
#
# So every group here is paired. A raw arm answers "does this state co-occur with
# returns", which momentum alone can produce; the CONDITIONED arm holds the daily
# trend flag fixed and answers the only question that matters for CLAUDE.md —
# does the NEW block add anything over what the bundle already told the brain.
#
# indicators_by_ticker is deliberately absent. It is a projection of fields
# already computed elsewhere (technicals.build_indicator_map), not a signal: it
# has no predicate and no forward return of its own. Measuring it would mean
# measuring its components, and half of those are still inline arithmetic in
# universe_scanner.py rather than extractable pure functions.
NEW_SIGNAL_GROUPS = [
    # --- 1. Anchored VWAP -------------------------------------------------
    # CLAIM (CLAUDE.md): "above it, buyers since that event are collectively in
    # profit and it tends to act as support; below it, as resistance."
    ("aVWAP above", lambda r: r.get("avwap_above") is True),
    ("aVWAP below", lambda r: r.get("avwap_above") is False),
    # The control that matters: hold the daily 200-DMA trend fixed. If "above
    # aVWAP" only pays because names above their aVWAP are also in uptrends,
    # these two collapse toward each other and the level adds nothing.
    ("aVWAP above | 200dma up", lambda r: (r.get("avwap_above") is True
                                           and r.get("daily_above_200dma") is True)),
    ("aVWAP below | 200dma up", lambda r: (r.get("avwap_above") is False
                                           and r.get("daily_above_200dma") is True)),
    # Anchor provenance: a real breakout anchor vs the highest-volume fallback.
    # If only the fallback arm scores, the "breakout" framing is decoration.
    ("aVWAP above | breakout anchor",
     lambda r: (r.get("avwap_above") is True
                and r.get("avwap_anchor") == "breakout_above_20d_high")),
    ("aVWAP above | volume anchor",
     lambda r: (r.get("avwap_above") is True
                and r.get("avwap_anchor") == "highest_volume_session")),

    # --- 2. Weekly structure ----------------------------------------------
    # CLAIM (CLAUDE.md): "a name can be a daily uptrend while its weekly
    # structure rolls over - that divergence is a warning the daily set cannot
    # show you." That is a falsifiable statement about the DIVERGENCE cells.
    ("weekly uptrend", lambda r: r.get("wk_structure") == "uptrend"),
    ("weekly downtrend", lambda r: r.get("wk_structure") == "downtrend"),
    # THE DIVERGENCE TEST. Both arms are daily uptrends; they differ only in the
    # weekly read. If CLAUDE.md is right, the second underperforms the first.
    # If they match, the weekly block is telling the brain nothing new and the
    # prompt should stop claiming otherwise.
    ("wk uptrend | 200dma up", lambda r: (r.get("wk_structure") == "uptrend"
                                          and r.get("daily_above_200dma") is True)),
    ("wk ROLLOVER | 200dma up", lambda r: (r.get("wk_structure") == "downtrend"
                                           and r.get("daily_above_200dma") is True)),
    ("wk above 30w MA", lambda r: r.get("wk_above_30w") is True),
    ("wk 30w MA rising", lambda r: r.get("wk_30w_rising") is True),

    # --- 3. RS vs sector vs RS vs SPY -------------------------------------
    # Equal-n top quintiles drawn from an IDENTICAL pool (see cross_fn), so the
    # two arms differ only in the benchmark the ranking used.
    ("RS-vs-SPY top quintile", lambda r: r.get("sel_rs_spy_top") is True),
    ("RS-vs-SECTOR top quintile", lambda r: r.get("sel_rs_sector_top") is True),
    # THE ABLATION: same construction against an arbitrary grouping of the same
    # shape. Whatever this scores is what the METHOD earns without any sector
    # information; the real arm has to beat it, not merely beat zero.
    ("RS-vs-PSEUDO-sector (ablation)", lambda r: r.get("sel_rs_pseudo_top") is True),
    # The incremental cells: names each benchmark uniquely surfaces. This is the
    # actual decision — whether to cite sector RS when SPY RS already ranked it.
    ("RS sector-only (not SPY top)", lambda r: r.get("sel_rs_sector_only") is True),
    ("RS SPY-only (not sector top)", lambda r: r.get("sel_rs_spy_only") is True),
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
    sectors = {s: [t.upper() for t in v] for s, v in sectors.items()}
    tickers = sorted({t for v in sectors.values() for t in v})
    if args.limit:
        tickers = tickers[:args.limit]

    print("universe=%d years=%d step=%d top_n=%d"
          % (len(tickers), args.years, args.step, args.top_n))
    panel = load_panel(tickers, args.years, args.cache or None)
    print("panel names=%d" % len(panel))
    extras = build_extras(panel, tickers, sectors)
    signal_fn, cross_fn = make_lane_signal(top_n=args.top_n)
    rows = run(tickers, args.years, args.step, signal_fn, cross_fn=cross_fn,
               panel=panel, extras=extras, progress=True)
    if isinstance(rows, dict):
        print(rows)
        return
    report(rows, LANE_GROUPS + NEW_SIGNAL_GROUPS)

    if args.json_out:
        blob = {}
        for lbl, pred in LANE_GROUPS + NEW_SIGNAL_GROUPS:
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
