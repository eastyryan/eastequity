"""SEPA trend template + VCP — Minervini's stage-2 gate and contraction pattern.

*** RESEARCH ONLY — NOT WIRED INTO THE SCANNER OR THE BRAIN PACK. MEASURED, REJECTED. ***

scripts/sepa_study.py evaluated this over 182 names x 5 years (31,252 observations),
scored under THIS BOOK's risk rules (2xATR stop + 3xATR chandelier trail):

    group                 21d excess   63d excess
    template PASS             +0.22        -0.10
    template FAIL             -0.05        +0.02
    7 of 8 (near miss)        +0.06        -0.05
    PASS + VCP + buy zone     -0.51        -0.96

Verdict, and why it was cut rather than tuned:
  1. NO EDGE under real risk rules. The +1.15pp separation in a naive fixed-horizon
     test is a VOLATILITY-SELECTION artifact: the gate picks higher-ATR names
     (MFE 9.77 / MAE -5.84 vs 8.39 / -5.22 for failures) at a near-identical
     MFE/MAE ratio. It selects names that move more, not names that move better.
     The chandelier trail already harvests the trend persistence the template was
     meant to find — redundant, not additive.
  2. NO DISCRIMINATION at the margin: 7-of-8 performs the same as 8-of-8, so the
     AND-gate is not doing the work its structure implies.
  3. NO NEW INFORMATION: all 8 conditions are recombinations of fields the scan row
     already carries (above_200dma, trend_up_50_over_200, pct_from_52w_high,
     rel_strength_3m_pct). It re-encodes a binary and charges bundle weight for it.
  4. The VCP layer measured NEGATIVE at every horizon under every risk rule tested.
  5. Do NOT import SEPA's -8% stop rule either: tested directly, it stops out 54.8%
     of passing names by 63d and makes PASS *worse* than FAIL. The existing ATR
     noise-band floor in stop_engineering is correct and this is evidence for it.

Kept because scripts/sepa_study.py imports it and the harness generalises: it can
score any future signal against the book's actual risk rules. If you revisit this,
the ONE untested variable is condition 8 — RS is ranked here against the curated
universe, not the market (see attach_rs_percentile). Finding 2 argues the gate
structure is inert regardless, so treat that as a long shot, not a rescue.

Original design notes follow.

Why this was built: the scanner already ranks names by a momentum/setup blend, but a
score of 6.2 does not tell you whether a name is in a *buyable stage*. Minervini's
trend template is a hard 8-condition gate — a name either has stage-2 structure or
it does not — and VCP (volatility contraction pattern) is the entry-timing half:
successive pullbacks getting shallower on drying volume, resolving at a pivot. This
module produces both reads honestly and degrades to "unknown" rather than guessing.

DELIBERATELY OMITTED (do not add here):
- The SEPA fundamental screen (quarterly EPS >= 20%, annual >= 25% x 3yr). It is a
  growth-stock filter that would disqualify large-cap semis this book legitimately
  holds. Fundamentals stay in tools/fundamentals.py and the brain's judgment.
- Stops, sizing, and scale-out rules. Those are validator.py's authority; a research
  tool that also proposed position sizes would create two sources of truth.

Two phases, because condition 8 is cross-sectional:
1. IN-LOOP (per name, from its own bars) — conditions 1-7 + the VCP read. Called
   from universe_scanner's row loop alongside analyze_volume().
2. POST-LOOP (needs every row) — attach_rs_percentile() ranks 3-month relative
   strength across the scanned set and fills condition 8, then finalizes the gate.

Between the two phases a row's `template_pass` is None with status "pending_rs".
None NEVER means pass. A name whose history is too short for a real 200-DMA gets
template_pass=None / status="insufficient_history" — unverified, not failed, and
not buyable on this signal either way.

All helpers are pure (lists in, scalars/dicts out) and unit-tested offline in
tests/test_sepa_trend.py. No network, no file IO.

CLI: python -m tools.sepa_trend NVDA
"""

from __future__ import annotations

# --- trend template thresholds (Minervini's published numbers) ---
SESS_50, SESS_150, SESS_200 = 50, 150, 200
SESS_52W = 252
SMA200_SLOPE_LOOKBACK = 21      # "200MA trending up >= 1 month"
MIN_PCT_ABOVE_52W_LOW = 30.0    # price must sit >= 30% above the 52w low
MAX_PCT_BELOW_52W_HIGH = 25.0   # ...and within 25% of the 52w high
MIN_RS_PERCENTILE = 70.0        # RS rating > 70th percentile (85+ preferred)

# Bars needed before condition 3 can be evaluated at all.
MIN_BARS_FULL_TEMPLATE = SESS_200 + SMA200_SLOPE_LOOKBACK  # 221

# --- VCP detection parameters ---
VCP_WINDOW = 65                 # ~3 months of consolidation to search
VCP_SWING_THRESHOLD_PCT = 3.0   # zigzag reversal size; below this is noise
VCP_MIN_CONTRACTIONS = 2        # Minervini: 2-6, typically 3-4
VCP_MAX_BASE_LEGS = 4           # only the final legs form the base (see _tightening_run)
VCP_MAX_FINAL_DEPTH_PCT = 10.0  # the last contraction must be tight
VCP_DRYUP_RATIO = 0.80          # recent volume vs consolidation average
BUY_ZONE_PCT = 5.0              # pivot to +5% is the buy zone
BREAKOUT_VOL_MULT = 1.5         # >= 1.5x average volume confirms a breakout


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _sma_at(closes, n: int, offset: int = 0):
    """Simple moving average of `n` closes ending `offset` bars before the last bar.
    None when history is too short — never a partial-window average wearing an
    'SMA200' label. Pure."""
    if not closes or n <= 0 or offset < 0:
        return None
    end = len(closes) - offset
    start = end - n
    if start < 0 or end <= 0:
        return None
    window = [c for c in closes[start:end] if c is not None]
    if len(window) < n:
        return None
    return sum(window) / n


def _trailing_low(lows, sessions: int):
    """(lowest low over the trailing `sessions` bars, is_full_window). Mirrors
    universe_scanner._trailing_high so the 52-week claim is gated the same way."""
    if not lows:
        return None, False
    window = lows[-sessions:] if len(lows) >= sessions else list(lows)
    vals = [v for v in window if v is not None]
    if not vals:
        return None, False
    return min(vals), len(lows) >= sessions


def _trailing_high(highs, sessions: int):
    """(highest high over the trailing `sessions` bars, is_full_window). Duplicated
    from universe_scanner rather than imported to keep this module import-free and
    testable in isolation; the semantics are identical and pinned by tests."""
    if not highs:
        return None, False
    window = highs[-sessions:] if len(highs) >= sessions else list(highs)
    vals = [v for v in window if v is not None]
    if not vals:
        return None, False
    return max(vals), len(highs) >= sessions


def percentile_rank(value, population) -> float | None:
    """Percent of `population` at or below `value`, 0-100. The RS-rating analog:
    IBD-style ratings are cross-sectional ranks, not absolute returns. None when
    the value or population is unusable. Pure."""
    if value is None:
        return None
    vals = [v for v in (population or []) if isinstance(v, (int, float))]
    if not vals:
        return None
    at_or_below = sum(1 for v in vals if v <= value)
    return round(at_or_below / len(vals) * 100, 1)


# --------------------------------------------------------------------------- #
# Trend template (conditions 1-8)
# --------------------------------------------------------------------------- #
def trend_template(closes, highs, lows, rs_percentile=None) -> dict:
    """The 8-condition stage-2 gate from oldest-first daily bars.

    Each condition is True / False / None (None = not enough history to judge).
    `template_pass` is True only when all 8 are True; None when any is unknown;
    False when at least one is genuinely violated. Callers must treat None as
    "unverified", never as a pass.
    """
    if not closes or len(closes) < 2:
        return {"status": "insufficient_history", "template_pass": None,
                "conditions": {}, "passed": 0, "failed": 0, "unknown": 8}

    last = closes[-1]
    sma50 = _sma_at(closes, SESS_50)
    sma150 = _sma_at(closes, SESS_150)
    sma200 = _sma_at(closes, SESS_200)
    sma200_prior = _sma_at(closes, SESS_200, offset=SMA200_SLOPE_LOOKBACK)

    hi_52w, full_hi = _trailing_high(highs, SESS_52W)
    lo_52w, full_lo = _trailing_low(lows, SESS_52W)

    def _and(*flags):
        """True if all True, False if any False, None if any unknown and none False."""
        if any(f is False for f in flags):
            return False
        if any(f is None for f in flags):
            return None
        return True

    pct_above_low = ((last / lo_52w - 1) * 100) if lo_52w else None
    pct_below_high = ((1 - last / hi_52w) * 100) if hi_52w else None

    c = {
        # 1. Price above both the 150- and 200-day averages.
        "price_above_150_200": _and(
            (last > sma150) if sma150 is not None else None,
            (last > sma200) if sma200 is not None else None),
        # 2. The 150-day sits above the 200-day.
        "ma150_above_ma200": (sma150 > sma200)
        if (sma150 is not None and sma200 is not None) else None,
        # 3. The 200-day has been rising for at least a month.
        "ma200_trending_up": (sma200 > sma200_prior)
        if (sma200 is not None and sma200_prior is not None) else None,
        # 4. The 50-day sits above both longer averages.
        "ma50_above_150_200": _and(
            (sma50 > sma150) if (sma50 is not None and sma150 is not None) else None,
            (sma50 > sma200) if (sma50 is not None and sma200 is not None) else None),
        # 5. Price above the 50-day.
        "price_above_ma50": (last > sma50) if sma50 is not None else None,
        # 6. Well off the lows — not a bounce in a downtrend.
        "pct_above_52w_low_ok": (pct_above_low >= MIN_PCT_ABOVE_52W_LOW)
        if (pct_above_low is not None and full_lo) else None,
        # 7. Near the highs — leadership, not a laggard.
        "near_52w_high": (pct_below_high <= MAX_PCT_BELOW_52W_HIGH)
        if (pct_below_high is not None and full_hi) else None,
        # 8. Cross-sectional strength; filled by attach_rs_percentile().
        "rs_percentile_ok": (rs_percentile > MIN_RS_PERCENTILE)
        if rs_percentile is not None else None,
    }

    passed = sum(1 for v in c.values() if v is True)
    failed = sum(1 for v in c.values() if v is False)
    unknown = sum(1 for v in c.values() if v is None)

    if failed:
        template_pass = False
    elif unknown:
        template_pass = None
    else:
        template_pass = True

    # Distinguish "we could not check" from "it failed" — the brain treats a short
    # history very differently from a broken trend. Note the ordering: a FAILED gate
    # is already final (an unknown condition cannot rescue a violated one), so a
    # decided verdict always reports "ok" no matter what else is missing.
    if template_pass is not None:
        status = "ok"
    elif len(closes) < MIN_BARS_FULL_TEMPLATE:
        status = "insufficient_history"
    elif c["rs_percentile_ok"] is None and unknown == 1:
        status = "pending_rs"
    else:
        status = "incomplete"

    return {
        "status": status,
        "template_pass": template_pass,
        "conditions": c,
        "passed": passed, "failed": failed, "unknown": unknown,
        "failed_conditions": sorted(k for k, v in c.items() if v is False),
        "sma50": round(sma50, 2) if sma50 is not None else None,
        "sma150": round(sma150, 2) if sma150 is not None else None,
        "sma200": round(sma200, 2) if sma200 is not None else None,
        "pct_above_52w_low": round(pct_above_low, 1)
        if pct_above_low is not None else None,
        "pct_below_52w_high": round(pct_below_high, 1)
        if pct_below_high is not None else None,
        "rs_percentile": rs_percentile,
    }


# --------------------------------------------------------------------------- #
# VCP — volatility contraction pattern
# --------------------------------------------------------------------------- #
def swing_points(highs, lows, threshold_pct: float = VCP_SWING_THRESHOLD_PCT):
    """Zigzag swing highs/lows: alternating pivots confirmed only after price
    retraces `threshold_pct` from the running extreme. Returns oldest-first
    [(index, price, "high"|"low"), ...]. Smaller wiggles are noise and are ignored
    by design — VCP is a structure of real pullbacks, not of every red bar. Pure."""
    n = min(len(highs or []), len(lows or []))
    if n < 3:
        return []

    thr = threshold_pct / 100.0
    points: list[tuple[int, float, str]] = []
    direction = None           # "up" while tracking a high, "down" while tracking a low
    ext_idx, ext_price = 0, highs[0]
    cand_idx, cand_price = 0, lows[0]

    for i in range(1, n):
        hi, lo = highs[i], lows[i]
        if hi is None or lo is None:
            continue
        if direction is None:
            # Establish direction from whichever threshold breaks first.
            if hi >= lows[0] * (1 + thr):
                direction, ext_idx, ext_price = "up", i, hi
            elif lo <= highs[0] * (1 - thr):
                direction, ext_idx, ext_price = "down", i, lo
            continue
        if direction == "up":
            if hi > ext_price:
                ext_idx, ext_price = i, hi
            elif lo <= ext_price * (1 - thr):
                points.append((ext_idx, ext_price, "high"))
                direction, ext_idx, ext_price = "down", i, lo
        else:
            if lo < ext_price:
                ext_idx, ext_price = i, lo
            elif hi >= ext_price * (1 + thr):
                points.append((ext_idx, ext_price, "low"))
                direction, ext_idx, ext_price = "up", i, hi

    # The in-progress leg is a real, unconfirmed extreme — include it so a pattern
    # that just made its final low is not invisible until it reverses.
    if direction is not None:
        points.append((ext_idx, ext_price, "high" if direction == "up" else "low"))
    return points


def _contractions_from_swings(points):
    """Each confirmed high -> following low becomes one contraction with its depth.
    Pure; operates on the output of swing_points()."""
    out = []
    for a, b in zip(points, points[1:]):
        (hi_idx, hi_px, hi_kind), (lo_idx, lo_px, lo_kind) = a, b
        if hi_kind != "high" or lo_kind != "low" or hi_px in (None, 0):
            continue
        out.append({
            "high_idx": hi_idx, "low_idx": lo_idx,
            "high": round(hi_px, 2), "low": round(lo_px, 2),
            "depth_pct": round((1 - lo_px / hi_px) * 100, 1),
        })
    return out


def _leg_volume(volumes, start_idx, end_idx):
    """Mean volume across one contraction leg; None when unusable. Pure."""
    if not volumes:
        return None
    seg = [v for v in volumes[start_idx:end_idx + 1] if v is not None]
    return (sum(seg) / len(seg)) if seg else None


def _tightening_run(depths, max_legs: int = VCP_MAX_BASE_LEGS,
                    tolerance: float = 0.05) -> int:
    """Length of the decreasing run of contraction depths ENDING at the last one,
    capped at `max_legs`.

    Why a trailing run and not the whole list: a VCP is the base immediately before
    the pivot, not every swing in the lookback. A volatile leader can print a dozen
    swings in three months; demanding all of them contract monotonically would mean
    no real name ever qualifies (NVDA printed 11 legs on a live pull). Minervini
    counts the 2-6 contractions that FORM the base — so walk backwards from the last
    leg and stop at the first one that did not tighten. Pure.
    """
    clean = [d for d in depths if isinstance(d, (int, float))]
    if len(clean) < 2:
        return 0
    run = 1
    for a, b in zip(reversed(clean[:-1]), reversed(clean)):
        # `a` is the earlier leg, `b` the later one: the later must be shallower.
        if b < a * (1 + tolerance) and b < a:
            run += 1
            if run >= max_legs:
                break
        else:
            break
    return run if run >= 2 else 0


def _strictly_decreasing(vals, tolerance: float = 0.0) -> bool:
    """True when each value is below its predecessor (allowing `tolerance` slack, so a
    12.0% -> 12.1% wobble is not a hard disqualifier). Pure."""
    clean = [v for v in vals if v is not None]
    if len(clean) < 2:
        return False
    return all(b <= a * (1 + tolerance) and b < a * (1 + 1e-9)
               for a, b in zip(clean, clean[1:]))


def detect_vcp(highs, lows, closes, volumes, window: int = VCP_WINDOW,
               last_bar_partial: bool = False) -> dict:
    """Volatility contraction read over the trailing `window` sessions.

    A VCP is successive pullbacks of DECREASING depth on DECREASING volume, ending
    tight, with a pivot at the consolidation high. This returns the components
    separately so a near-miss ("contracting but volume never dried up") is legible
    rather than collapsing to a bare False.

    `last_bar_partial` MUST be True when the newest bar is a still-forming session
    (any run before 16:00 ET). A partial bar carries a fraction of a day's volume,
    which corrupts both volume reads — and corrupts them in opposite directions:
      - breakout_volume_mult reads far too LOW (a real breakout looks unconfirmed);
      - volume_dryup reads far too HIGH, and dry-up is a BULLISH tell, so a partial
        bar manufactures the confirmation that completes a VCP.
    The second is the dangerous one, so when the flag is set the partial bar is
    excluded from every volume statistic and the breakout read reports None rather
    than a number computed from a fifth of a session. PRICE is left alone — a live
    price is exactly what you want for a buy-zone decision.
    """
    n = min(len(highs or []), len(lows or []), len(closes or []))
    if n < 30:
        return {"status": "insufficient_history", "vcp_detected": False,
                "contraction_count": 0, "contractions": []}

    w = min(window, n)
    h, lo_, c = highs[-w:], lows[-w:], closes[-w:]
    v = volumes[-w:] if volumes else []
    # Volume series excluding any still-forming bar; prices keep the live bar.
    v_done = v[:-1] if (last_bar_partial and v) else v

    points = swing_points(h, lo_)
    contractions = _contractions_from_swings(points)
    for leg in contractions:
        leg["avg_volume"] = _leg_volume(v, leg["high_idx"], leg["low_idx"])

    if len(contractions) < VCP_MIN_CONTRACTIONS:
        return {"status": "no_consolidation", "vcp_detected": False,
                "contraction_count": len(contractions), "contractions": contractions}

    depths_all = [x["depth_pct"] for x in contractions]
    # The BASE is the trailing tightening sequence, not every swing in the window.
    run = _tightening_run(depths_all)
    base = contractions[-run:] if run else []
    depths = [x["depth_pct"] for x in base]
    leg_vols = [x.get("avg_volume") for x in base]
    depths_contracting = run >= VCP_MIN_CONTRACTIONS
    volume_contracting = _strictly_decreasing(leg_vols, tolerance=0.05)

    # Volume dry-up: the last 5 COMPLETED sessions vs the consolidation's own
    # average — the "no one left to sell" tell that precedes a clean breakout.
    recent_v = [x for x in v_done[-5:] if x is not None]
    base_v = [x for x in v_done if x is not None]
    dryup_ratio = ((sum(recent_v) / len(recent_v)) / (sum(base_v) / len(base_v))
                   if recent_v and base_v and sum(base_v) else None)
    volume_dryup = (dryup_ratio is not None and dryup_ratio <= VCP_DRYUP_RATIO)

    final_depth = depths_all[-1]
    # Pivot = the high of the BASE (the price that must break), not the highest
    # swing in the whole lookback — an older, higher swing is overhead supply, a
    # different pattern entirely, and quoting it would place the buy zone far too high.
    pivot = max((x["high"] for x in (base or contractions)), default=None)
    last = c[-1]
    pct_to_pivot = ((last / pivot - 1) * 100) if pivot else None
    in_buy_zone = (pivot is not None and pivot <= last <= pivot * (1 + BUY_ZONE_PCT / 100))

    # Breakout sponsorship is unknowable mid-session: today's running volume against
    # a full-day average is not a comparison, it is a clock reading.
    avg_v = (sum(base_v) / len(base_v)) if base_v else None
    breakout_vol_mult = (None if last_bar_partial
                         else (v[-1] / avg_v) if (v and v[-1] and avg_v) else None)

    vcp_detected = bool(depths_contracting
                        and final_depth <= VCP_MAX_FINAL_DEPTH_PCT
                        and (volume_contracting or volume_dryup))

    return {
        "status": "ok",
        "vcp_detected": vcp_detected,
        "last_bar_partial": bool(last_bar_partial),
        "volume_bars_used": len([x for x in v_done if x is not None]),
        "contraction_count": len(contractions),
        "base_leg_count": run,
        "contractions": contractions,
        "depths_pct": depths_all,
        "base_depths_pct": depths,
        "depths_contracting": depths_contracting,
        "volume_contracting": volume_contracting,
        "volume_dryup": volume_dryup,
        "dryup_ratio": round(dryup_ratio, 2) if dryup_ratio is not None else None,
        "final_depth_pct": final_depth,
        "pivot": pivot,
        "pct_to_pivot": round(pct_to_pivot, 1) if pct_to_pivot is not None else None,
        "in_buy_zone": in_buy_zone,
        "breakout_volume_mult": round(breakout_vol_mult, 2)
        if breakout_vol_mult is not None else None,
        "breakout_volume_ok": (breakout_vol_mult >= BREAKOUT_VOL_MULT)
        if breakout_vol_mult is not None else None,
    }


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def evaluate_sepa(closes, highs, lows, volumes, rs_percentile=None,
                  last_bar_partial: bool = False) -> dict:
    """Phase-1 read for one name: trend template (conditions 1-7, plus 8 if
    `rs_percentile` is already known) and the VCP pattern. Never raises — a bad
    series degrades to status flags. Call from the scanner's row loop.

    Pass `last_bar_partial=True` on any intraday run — see detect_vcp for why a
    still-forming bar manufactures a false volume-dry-up confirmation."""
    try:
        tt = trend_template(closes, highs, lows, rs_percentile=rs_percentile)
    except Exception:
        tt = {"status": "error", "template_pass": None, "conditions": {},
              "passed": 0, "failed": 0, "unknown": 8}
    try:
        vcp = detect_vcp(highs, lows, closes, volumes,
                         last_bar_partial=last_bar_partial)
    except Exception:
        vcp = {"status": "error", "vcp_detected": False,
               "contraction_count": 0, "contractions": []}
    return {"trend_template": tt, "vcp": vcp, "read": _headline(tt, vcp)}


def _headline(tt: dict, vcp: dict) -> str:
    """One-token summary — the field the slim brain pack carries when the full
    nested dict is too heavy. Pure."""
    if tt.get("template_pass") is True:
        # Intentionally does NOT branch on the VCP: that layer measured negative and
        # must not colour the headline the brain reads. See VALIDATION_NOTE.
        return "stage2_structure"
    if tt.get("template_pass") is False:
        return "not_stage2"
    if tt.get("status") == "insufficient_history":
        return "unverified_short_history"
    return "unverified"


# What the brain is told about how much this signal has earned. Measured by
# scripts/sepa_study.py over 182 names x 5y (31,252 observations), scored under this
# book's own risk rules (2xATR stop + 3xATR chandelier trail):
#
#   group                 21d excess   63d excess
#   template PASS             +0.22        -0.10
#   template FAIL             -0.05        +0.02
#   PASS + VCP + buy zone     -0.51        -0.96
#
# The template's separation in a naive fixed-horizon test (+1.15pp at 21d) does NOT
# survive ATR-normalised risk: the gate selects higher-ATR names, which flatters
# unstopped returns and washes out once the trail does its job. The chandelier already
# harvests the trend persistence the template was supposed to identify — they are
# redundant, not additive. The VCP buy zone is the one CONSISTENT result in the study,
# and it is consistently NEGATIVE across all three risk regimes tested.
VALIDATION_NOTE = ("descriptive only — no measured edge over this book's ATR stop and "
                   "chandelier trail (see scripts/sepa_study.py). Use as a structure "
                   "label and a rejection aid; do NOT size, gate, or raise confidence "
                   "on it. The VCP/buy-zone layer measured NEGATIVE and is withheld.")


def compact_sepa(block: dict) -> dict:
    """Trim a full evaluate_sepa() result for carriage in a scan row.

    Carries the trend template only. The VCP block is DELIBERATELY WITHHELD from the
    brain: it measured negative excess return at every horizon and under every risk
    rule tested (worst: -0.96pp at 63d with the book's own trail). Shipping a signal
    that rigorous-looking with that record invites the brain to act on it. The code
    and its tests stay for research — scripts/sepa_study.py still consumes detect_vcp
    directly — but it does not reach a trading decision.

    Everything phase 2 needs (`trend_template.conditions`) is preserved, so this must
    run BEFORE attach_rs_percentile, not after. Pure.
    """
    if not isinstance(block, dict):
        return {}
    tt = block.get("trend_template") or {}
    return {
        "read": block.get("read"),
        "validation": VALIDATION_NOTE,
        "trend_template": {k: v for k, v in tt.items()
                           if k not in ("sma50", "sma150", "sma200")},
    }


def attach_rs_percentile(rows, field: str = "rel_strength_3m_pct",
                         key: str = "sepa") -> list:
    """Phase-2 pass: rank `field` across every scanned row, write the percentile into
    each row's SEPA block as condition 8, and re-finalize `template_pass`.

    Mutates and returns `rows`. Rows without a SEPA block are skipped. This is the
    only place condition 8 can be evaluated honestly — an RS *rating* is a rank
    against a population, and a single name's bars cannot produce one.

    CAVEAT, and it matters: the population is whatever `rows` you pass, which for
    scan_universe is the CURATED universe — not the whole market. Minervini's RS
    rating is market-wide. Ranking inside a hand-picked basket of AI/momentum names
    compresses the spread: in a strong tape most of the basket clears 70 and the
    condition stops filtering, and in a broad selloff the least-bad name still ranks
    90th. `rs_population_n` is written alongside the percentile so a reader can see
    how narrow the comparison was. For a market-wide read, pass discovery_screen's
    ~600-name sweep instead.
    """
    if not rows:
        return rows
    population = [r.get(field) for r in rows
                  if isinstance(r.get(field), (int, float))]
    if not population:
        return rows

    for r in rows:
        block = r.get(key)
        if not isinstance(block, dict):
            continue
        tt = block.get("trend_template")
        if not isinstance(tt, dict) or not isinstance(tt.get("conditions"), dict):
            continue

        pct = percentile_rank(r.get(field), population)
        tt["rs_percentile"] = pct
        # Population size travels WITH the percentile: "90th of 12 names" and
        # "90th of 600" are different claims and must not read as the same one.
        tt["rs_population_n"] = len(population)
        tt["conditions"]["rs_percentile_ok"] = (pct > MIN_RS_PERCENTILE
                                                if pct is not None else None)

        c = tt["conditions"]
        tt["passed"] = sum(1 for v in c.values() if v is True)
        tt["failed"] = sum(1 for v in c.values() if v is False)
        tt["unknown"] = sum(1 for v in c.values() if v is None)
        tt["failed_conditions"] = sorted(k for k, v in c.items() if v is False)

        if tt["failed"]:
            tt["template_pass"] = False
        elif tt["unknown"]:
            tt["template_pass"] = None
        else:
            tt["template_pass"] = True

        if tt["template_pass"] is not None:
            tt["status"] = "ok"
        elif tt["status"] == "pending_rs":
            tt["status"] = "incomplete"

        block["read"] = _headline(tt, block.get("vcp") or {})
    return rows


if __name__ == "__main__":
    import json
    import sys
    import yfinance as yf
    t = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    df = yf.Ticker(t).history(period="2y", interval="1d",
                              auto_adjust=True).dropna()
    print(json.dumps(evaluate_sepa(
        [float(x) for x in df["Close"]], [float(x) for x in df["High"]],
        [float(x) for x in df["Low"]], [float(x) for x in df["Volume"]]), indent=1))
