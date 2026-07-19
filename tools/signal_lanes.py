"""Lane predicates, extracted from the scanner so they can be MEASURED.

*** RESEARCH MODULE. Nothing here is wired into the scanner or the brain pack. ***

Why this file exists: `tools/universe_scanner.py` ships roughly ten candidate lanes to
the decision-maker. Exactly one signal in this repo has ever been measured — SEPA, in
`scripts/sepa_study.py`, which rejected it. The rest ship on plausibility. Three of them
(contrarian_reversal, deep_value_200w, supplier_pullback) are inline list comprehensions
at `universe_scanner.py:817-866` with no named function, so there was nothing a harness
could point at. This module gives them names and a pure, offline-testable form.

COPIED, NOT IMPORTED — and deliberately so. The scanner is owned elsewhere and evolves;
a study must be frozen against the exact predicate it measured, or next month's results
describe code that no longer exists. Every copied block carries a line pointer to its
original. If the scanner's lanes change, these will silently disagree — that is the
intended failure mode, and `tests/test_signal_lanes.py` pins the copies. Named pure
functions (`earnings_reaction_read`, `_gap_events`) ARE imported: they are already
factored for exactly this and duplicating them would create two sources of truth.

WHAT IS AND IS NOT RECONSTRUCTIBLE. Every field these predicates read is derived from
price/volume bars, with three exceptions that are honest holes in any historical test:

  1. `ai_exposure` (supplier_pullback) is a TODAY label from data/ai_exposure.json. A
     name labelled ai_supplier in 2026 was not knowably one in 2018. Backtesting the
     supplier lane with it is a mild look-ahead on the CLASSIFICATION (not on price).
     Direction of bias: favourable — the label was assigned partly by knowing who the
     AI buildout eventually enriched.
  2. `_screen_quality_ok` (deep_value_200w) reads a live estimate-screen cache with no
     history. It fails open on missing data, so historically it is a no-op. The measured
     lane is therefore the lane WITHOUT its quality filter — slightly wider than what
     ships. Direction of bias: unfavourable to the lane (the filter removes wrecks).
  3. `_deep_value_sort_key` demotes ai_at_risk names, so lane ORDERING inherits (1).

Neither hole can be closed from a price panel. They are disclosed rather than papered
over, which is the whole point of measuring these at all.
"""

from __future__ import annotations

# Named pure functions the scanner already factored out — imported, not copied.
from tools.universe_scanner import (  # noqa: F401
    earnings_reaction_read, _gap_events,
)

# --- constants copied from tools/universe_scanner.py:35-102 ------------------
SESS_1M, SESS_3M, SESS_6M, SESS_52W = 21, 63, 126, 252
SESS_ADV, MIN_ADV_USD = 20, 20_000_000
WEEKS_200 = 200
AT_OR_BELOW_TOLERANCE_PCT = 2.0
MIN_BARS = SESS_3M + 1
DEFAULT_TOP_N = 15          # scan_universe's production top_n (runlib/context_gather.py:433)
LANE_LIMIT = 5              # every lane is truncated to [:5] in the scanner


# --- pure helpers copied from tools/universe_scanner.py:50-113 ---------------
def _median_dollar_volume(closes, volumes, sessions: int = SESS_ADV):
    """Copy of universe_scanner._median_dollar_volume (:50)."""
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
    """Copy of universe_scanner._return_over_sessions (:68)."""
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
    """Copy of universe_scanner._trailing_high (:84)."""
    if not highs:
        return None, False
    window = highs[-sessions:] if len(highs) >= sessions else list(highs)
    vals = [h for h in window if h is not None]
    if not vals:
        return None, False
    return max(vals), len(highs) >= sessions


def _ma_tail(values, n: int):
    """Copy of universe_scanner._ma_tail (:104)."""
    if not values:
        return None, False
    vals = [v for v in values[-n:] if v is not None]
    if not vals:
        return None, False
    return sum(vals) / len(vals), len(values) >= n


def _screen_quality_ok(screen_row) -> bool:
    """Copy of universe_scanner._screen_quality_ok (:140). Fails OPEN on missing data,
    which is every historical bar — see hole (2) in the module docstring."""
    if not isinstance(screen_row, dict):
        return True
    growth = screen_row.get("fwd_revenue_growth_pct")
    direction = screen_row.get("revision_direction")
    if isinstance(growth, (int, float)) and growth < 0 and direction == "down":
        return False
    return True


def _deep_value_sort_key(row: dict) -> tuple:
    """Copy of universe_scanner._deep_value_sort_key (:131)."""
    return (row.get("ai_exposure") == "ai_at_risk",
            row.get("pct_vs_200w_ma") if row.get("pct_vs_200w_ma") is not None else 0.0)


def _sma(values, n: int):
    if len(values) < n:
        return None
    w = values[-n:]
    return sum(w) / len(w)


def _atr_pct(highs, lows, closes, period: int = 14):
    """True-range mean over `period`, as a fraction of the last close. Matches the
    scanner's atr_pct (universe_scanner.py:700) closely enough for a selection check."""
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for j in range(n - period, n):
        trs.append(max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]),
                       abs(lows[j] - closes[j - 1])))
    last = closes[-1]
    if not last:
        return None
    return (sum(trs) / len(trs)) / last


def weekly_closes(dates, closes):
    """Week-end closes plus the DAILY index each one lands on, oldest first.

    The scanner gets its 200-week MA from a separate 5y/1wk yfinance download
    (universe_scanner.py:631-646). A study cannot re-download a point-in-time weekly
    series per bar, so weekly closes are derived from the daily panel instead: the last
    daily bar of each ISO week. Returns (closes, daily_positions) so a caller can take
    only the weeks that had CLOSED as of bar i — using the in-progress week's final
    close at mid-week would be look-ahead.
    """
    wc, pos, prev_key = [], [], None
    for k, d in enumerate(dates):
        try:
            key = d.isocalendar()[:2]
        except Exception:
            key = (getattr(d, "year", k), k)
        key = tuple(key)
        if prev_key is not None and key != prev_key:
            wc.append(closes[k - 1])
            pos.append(k - 1)
        prev_key = key
    return wc, pos


def weekly_ohlc(dates, opens, highs, lows, closes):
    """Weekly bars derived from the daily panel: (closes, highs, lows, positions).

    weekly_closes() above gives the closes only, which is all the 200-week MA
    needs. technicals.weekly_structure() additionally wants weekly HIGHS and LOWS
    — without them it computes wma_30w and weekly RSI but silently skips
    weekly_higher_highs / weekly_higher_lows / weekly_structure, which is the
    half of the block CLAUDE.md actually makes a claim about ("a name can be a
    daily uptrend while its weekly structure rolls over").

    Same ISO-week keying and the same closed-week discipline as weekly_closes:
    a week is emitted only once the NEXT week has started, and `positions` is the
    daily index of each week's last bar so a caller can take only the weeks that
    had closed as of bar i. Highs/lows are the max/min across the week's daily
    bars, which is what a real weekly bar is.

    `opens` is accepted and unused — it keeps the signature shaped like the OHLC
    tuple callers already hold, so nobody has to remember the argument order is
    different here.
    """
    wc, wh, wl, pos, prev_key = [], [], [], [], None
    cur_hi, cur_lo = None, None
    for k, d in enumerate(dates):
        try:
            key = tuple(d.isocalendar()[:2])
        except Exception:
            key = (getattr(d, "year", k), k)
        if prev_key is not None and key != prev_key:
            wc.append(closes[k - 1])
            wh.append(cur_hi)
            wl.append(cur_lo)
            pos.append(k - 1)
            cur_hi, cur_lo = None, None
        h, lo = highs[k], lows[k]
        cur_hi = h if cur_hi is None else max(cur_hi, h)
        cur_lo = lo if cur_lo is None else min(cur_lo, lo)
        prev_key = key
    return wc, wh, wl, pos


# --- the row the lanes read --------------------------------------------------
def lane_features(closes, highs, lows, vols, i, spy_closes=None, spy_i=None,
                  ai_exposure=None, weekly_closes_upto=None, ticker=None):
    """The subset of a `scan_universe` row (universe_scanner.py:764-800) that the three
    lane comprehensions actually read, computed from bars ENDING AT bar i.

    Strictly backward-looking: only closes[:i+1] etc. are touched. Returns None when
    there is not enough history for the scanner's own MIN_BARS gate. Pure — lists in,
    dict out — so the predicates below are testable with no network and no pandas.
    """
    c = closes[:i + 1]
    h = highs[:i + 1]
    lo = lows[:i + 1]
    v = vols[:i + 1]
    n = len(c)
    if n < MIN_BARS:
        return None
    last = c[-1]
    if not last:
        return None

    mom_1m = _return_over_sessions(c, SESS_1M)
    mom_3m = _return_over_sessions(c, SESS_3M)
    if mom_1m is None or mom_3m is None:
        return None

    rel_1m = rel_3m = None
    if spy_closes is not None and spy_i is not None:
        s = spy_closes[:spy_i + 1]
        s1, s3 = _return_over_sessions(s, SESS_1M), _return_over_sessions(s, SESS_3M)
        rel_1m = None if s1 is None else mom_1m - s1
        rel_3m = None if s3 is None else mom_3m - s3

    hi_52w, full_52w = _trailing_high(h, SESS_52W)
    pct_from_hi = (last / hi_52w - 1) * 100 if hi_52w else None

    sma20, sma50 = _sma(c, 20), _sma(c, 50)
    sma100 = _sma(c, 100) if n >= 100 else None
    sma200 = _sma(c, 200) if n >= 200 else None
    above_200 = (last > sma200) if sma200 is not None else None

    adv = _median_dollar_volume(c, v)
    pullback_20d = last / max(h[-20:]) - 1 if n >= 20 else None
    # THE DIVIDE-BY-ONE BUG, kept in sync with universe_scanner.py:920-926.
    #
    # This module still carried the expression the scanner replaced:
    #     (sum(v[-5:]) / 5) / max(sum(v[-65:-5]) / 60, 1)
    # The max(..., 1) floor avoided ZeroDivisionError by turning an ABSENT volume
    # baseline into a denominator of ONE SHARE, so a halted or freshly-listed name
    # produced a surge in the millions — and vol_surge > 1.3 scores +0.5 on
    # swing_setup_score, which is what the study's top_setups lane RANKS on. The
    # measured top_setups verdict was therefore scoring a name's own missing data.
    #
    # That is worse here than it was in the scanner: a study exists to tell you
    # what the shipped lane does, and this one was measuring a lane the scanner
    # no longer has. No usable baseline -> None, no score.
    vol_surge = None
    if n >= 65:
        base_vol = sum(v[-65:-5]) / 60.0
        recent_vol = sum(v[-5:]) / 5.0
        if base_vol > 0 and base_vol == base_vol and recent_vol == recent_vol:
            vol_surge = recent_vol / base_vol

    # 200-week MA context (universe_scanner.py:640-646, 794-797).
    w_ma = w_full = pct_vs_200w = None
    if weekly_closes_upto:
        w_ma, w_full = _ma_tail(weekly_closes_upto, WEEKS_200)
        if w_ma:
            pct_vs_200w = (weekly_closes_upto[-1] / w_ma - 1) * 100

    row = {
        "ticker": ticker, "i": i, "last_close": last,
        "momentum_1m_pct": round(mom_1m * 100, 1),
        "momentum_3m_pct": round(mom_3m * 100, 1),
        "rel_strength_1m_pct": None if rel_1m is None else round(rel_1m * 100, 1),
        "rel_strength_3m_pct": None if rel_3m is None else round(rel_3m * 100, 1),
        "pct_from_52w_high": None if pct_from_hi is None else round(pct_from_hi, 1),
        "is_full_52w_window": bool(full_52w),
        "above_200dma": above_200,
        "liquid": adv is not None and adv >= MIN_ADV_USD,
        "avg_dollar_volume_20d_usd": adv,
        "wma_200w": w_ma,
        "pct_vs_200w_ma": None if pct_vs_200w is None else round(pct_vs_200w, 1),
        "is_full_200w_window": bool(w_full),
        "ai_exposure": ai_exposure,
        "atr_pct": _atr_pct(h, lo, c),
        "pullback_from_20d_high_pct": (None if pullback_20d is None
                                       else round(pullback_20d * 100, 1)),
        "volume_surge_5d_vs_3m": None if vol_surge is None else round(vol_surge, 2),
    }
    row["swing_setup_score"] = swing_setup_score(
        last, sma20, sma50, sma100, sma200, mom_1m, mom_3m, pullback_20d,
        vol_surge, rel_1m)
    return row


def swing_setup_score(last, sma20, sma50, sma100, sma200, mom_1m, mom_3m,
                      pullback_20d, vol_surge, rel_1m):
    """Copy of the score block at universe_scanner.py:748-762.

    Needed only because all three lanes EXCLUDE names already surfaced in the top-N by
    this score. Measuring the lanes without the exclusion would measure a different,
    easier population than the one the brain sees.
    """
    if sma50 is None:
        return 0.0
    if sma200 is not None:
        in_uptrend = last > sma50 > sma200
    elif sma100 is not None:
        in_uptrend = last > sma50 > sma100
    else:
        in_uptrend = last > sma50
    score = 0.0
    score += 2.0 if in_uptrend else 0.0
    score += 1.0 if (sma20 is not None and last > sma20) else 0.0
    score += max(min(mom_3m * 5, 2.0), -1.0)
    score += max(min(mom_1m * 5, 1.0), -1.0)
    if pullback_20d is not None:
        score += 1.0 if -0.10 <= pullback_20d <= -0.02 else 0.0
    if vol_surge is not None:
        score += 0.5 if vol_surge > 1.3 else 0.0
    if rel_1m is not None:
        score += max(min(rel_1m * 4, 0.4), -0.2)
    return round(score, 2)


# --- the three predicates ----------------------------------------------------
# Each is the FILTER half of a scanner comprehension, with the cross-sectional half
# (exclusions + sort + [:5]) in select_lanes() below. Splitting them that way is what
# lets the study report both populations: the raw predicate (larger n, the cleaner
# statistical question) and the top-5 the brain is actually shown.

def contrarian_reversal_pred(row) -> bool:
    """universe_scanner.py:817-826. Quality names 20%+ off a GENUINE 52-week high that
    are turning up (1-month momentum > +2%)."""
    if not row or not row.get("is_full_52w_window"):
        return False
    pfh = row.get("pct_from_52w_high")
    mom = row.get("momentum_1m_pct")
    if pfh is None or mom is None:
        return False
    return pfh <= -20 and mom > 2


def deep_value_200w_pred(row, screen_row=None) -> bool:
    """universe_scanner.py:835-846. Liquid names with a real 200-week history trading
    at (within +2%) or below their 200-week MA."""
    if not row or not row.get("liquid") or not row.get("is_full_200w_window"):
        return False
    pct = row.get("pct_vs_200w_ma")
    if pct is None:
        return False
    return pct <= AT_OR_BELOW_TOLERANCE_PCT and _screen_quality_ok(screen_row)


def supplier_pullback_pred(row) -> bool:
    """universe_scanner.py:854-866. AI suppliers 8-30% off their 52-week high that
    still hold the 200-DMA — the 'extended leader coming back in' setup."""
    if not row:
        return False
    if row.get("ai_exposure") != "ai_supplier":
        return False
    if not (row.get("liquid") and row.get("is_full_52w_window")
            and row.get("above_200dma")):
        return False
    pfh = row.get("pct_from_52w_high")
    if pfh is None:
        return False
    return -30 <= pfh <= -8


def supplier_pullback_pred_no_label(row) -> bool:
    """ABLATION of supplier_pullback_pred: identical, minus the `ai_exposure` test.

    Not a shipping lane — a control. The ai_supplier label is a 2026 classification
    applied to 2015 bars (hole (1) in the module docstring), so any measured edge in the
    supplier lane could be the SETUP (liquid name 8-30% off its high, still above its
    200-DMA) or could be the LABEL quietly encoding who the AI buildout went on to
    enrich. Running both separates them: if the ablation performs the same, the label
    contributes nothing and the lane is a generic pullback screen wearing an AI badge.
    """
    if not row:
        return False
    if not (row.get("liquid") and row.get("is_full_52w_window")
            and row.get("above_200dma")):
        return False
    pfh = row.get("pct_from_52w_high")
    if pfh is None:
        return False
    return -30 <= pfh <= -8


def _sort_desc(key, default=-1e9):
    return lambda r: (r.get(key) if r.get(key) is not None else default)


def select_lanes(rows, top_n: int = DEFAULT_TOP_N, screen_rows=None, limit=LANE_LIMIT):
    """Reproduce the scanner's same-day lane CASCADE (universe_scanner.py:815-869).

    Order matters and is load-bearing: the top-N by swing_setup_score are taken off the
    board first, then contrarian, then deep_value, then supplier — each lane only sees
    what the ones before it did not take. A lane measured in isolation would be scored
    on names it never actually gets.

    Args:
        rows: every same-day lane_features() row in the scanned universe.
        top_n: how many top setups the scan surfaces before the lanes run.
    Returns {lane_name: [rows]}, each already truncated to `limit`.
    """
    screen_rows = screen_rows or {}
    ranked = sorted([r for r in rows if r],
                    key=_sort_desc("swing_setup_score"), reverse=True)
    top_tickers = {r["ticker"] for r in ranked[:top_n]}

    contrarian = sorted(
        (r for r in ranked
         if r["ticker"] not in top_tickers and contrarian_reversal_pred(r)),
        key=_sort_desc("momentum_1m_pct"), reverse=True)[:limit]

    taken = top_tickers | {r["ticker"] for r in contrarian}
    deep_value = sorted(
        (r for r in ranked
         if r["ticker"] not in taken
         and deep_value_200w_pred(r, screen_rows.get(r["ticker"]))),
        key=_deep_value_sort_key)[:limit]

    taken |= {r["ticker"] for r in deep_value}
    supplier = sorted(
        (r for r in ranked
         if r["ticker"] not in taken and supplier_pullback_pred(r)),
        key=_sort_desc("rel_strength_3m_pct", -999), reverse=True)[:limit]

    return {
        "top_setups": ranked[:top_n],
        "contrarian_reversal": contrarian,
        "deep_value_200w": deep_value,
        "supplier_pullback": supplier,
    }


# --- the earnings-reaction lane ---------------------------------------------
# earnings_reaction_read() is imported above rather than copied. It is already pure and
# already unit-testable; what it is NOT is historically reconstructible, for one reason:
#
#   `revision_direction` is a point-in-time analyst-estimate field. No free source
#   publishes the as-of revision state for an arbitrary past date, and the flag REQUIRES
#   it ("up"). So the shipping lane cannot be backtested as it ships.
#
# What CAN be measured from bars is its price leg — the gap-and-hold reaction that
# Brandt et al. (2008) is the evidence for — with the revisions leg held at "unknown".
# That is a strictly WEAKER signal than the one shipping, so a positive result on it is
# encouraging-but-incomplete and a NEGATIVE result is damning: if the reaction leg alone
# carries nothing, the lane is resting entirely on the unmeasurable half.
def gap_hold_reaction(opens, closes, i, window: int = 10):
    """Price-only half of the EAR lane: was there a >=2% up-gap in the last `window`
    sessions that has NOT filled and that HELD into its own close (retained >= 0.5)?

    Feeds the imported _gap_events()/earnings_reaction_read() pair with an assumed
    days_since_earnings = `window` (the lane's EAR_MAX_DAYS_SINCE_EARNINGS) and
    revision_direction = None -> "unknown". Returns (reaction, read) where reaction is
    the bare reaction label; the shipping FLAG is deliberately not returned, because
    without revisions it can never be True and reporting it would imply otherwise.
    """
    ga = _gap_events(opens[:i + 1], closes[:i + 1])
    _flag, read = earnings_reaction_read(window, ga, None, None)
    reaction = None
    if isinstance(read, str) and "_revisions_" in read:
        reaction = read.split("_revisions_")[0]
    return reaction, read
