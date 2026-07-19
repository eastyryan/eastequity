"""Market breadth — how many names are participating, not just where the index is.

WHY THIS EXISTS (2026-07-19 weekly run)
---------------------------------------
The brain reached the run's sharpest conclusion — "a momentum-factor unwind
concentrated inside the AI stack" — by reading eleven sector momentum averages
and noticing that every AI-buildout sector was strongly positive on 3-month and
sharply negative on 1-month while SPY was flat. That inference was correct and it
was also slow, unauditable, and available nowhere else in the system: the bundle
carried no advance/decline, no percentage above the 200-DMA, no new-high/new-low
count. An index level plus sector averages is not breadth.

Breadth answers a question no index level can: is the move broad or is it four
names? A tape making highs on narrowing participation and a tape making highs on
broad participation are different regimes that look identical in SPY.

HONESTY ABOUT THE SAMPLE — read this before citing any number here
-----------------------------------------------------------------
Every figure is computed over a STATED sample and labelled with it, because
breadth over a curated 184-name AI-biased universe is NOT market breadth and
must never be cited as though it were:

  sample="universe"  ~184 names, every run. Rich fields (200-DMA, 1-day change,
                     52-week highs). Tells you about THIS BOOK'S HUNTING GROUND.
                     A -30% reading here during an AI unwind is a fact about the
                     universe's concentration, not about the market.
  sample="broad"     ~950 names (S&P 500 + Russell 1000 + NDX + EM ADR, minus
                     universe members), weekly from the discovery sweep. Thinner
                     fields — no 1-day bar, no 200-DMA — but a genuinely
                     market-wide participation read.

A metric that cannot be computed from the fields present returns None with the
reason recorded in `unavailable`. It is never defaulted to zero: "0% above their
200-DMA" and "we did not have 200-DMA data" are opposite claims, and conflating
them is the failure class this repo has spent its life eliminating.
"""

from __future__ import annotations

from typing import Any, Iterable

# A name is "at" its high when within this much of it, and "deep" when this far
# below. New-high/new-low counts over a trailing window are proximity reads, not
# literal same-session prints — the daily bars here cannot see intraday extremes.
AT_HIGH_PCT = 2.0
DEEP_BELOW_HIGH_PCT = 20.0


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def _share_true(rows: Iterable[dict], field: str) -> tuple[float | None, int]:
    """Percentage of rows where `field` is truthy, over rows that HAVE it.

    Returns (pct, n_with_data). Rows where the field is None are excluded from
    both numerator and denominator — a name with no 200-DMA history is not a
    name below its 200-DMA.
    """
    vals = [r.get(field) for r in rows]
    have = [v for v in vals if isinstance(v, bool)]
    if not have:
        return None, 0
    return _pct(sum(1 for v in have if v), len(have)), len(have)


def _share_positive(rows: Iterable[dict], field: str) -> tuple[float | None, int]:
    vals = [_num(r.get(field)) for r in rows]
    have = [v for v in vals if v is not None]
    if not have:
        return None, 0
    return _pct(sum(1 for v in have if v > 0), len(have)), len(have)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0, 2)


def compute_breadth(rows: list[dict], *, sample: str, note: str = "") -> dict:
    """Breadth over one stated sample. Pure: list-in, dict-out, no network."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    out: dict[str, Any] = {
        "sample": sample,
        "n_names": len(rows),
        "note": note,
        "unavailable": {},
    }
    if not rows:
        out["status"] = "empty"
        return out
    out["status"] = "ok"

    def _record(key: str, pair: tuple[float | None, int], why: str):
        val, n = pair
        out[key] = val
        if val is None:
            out["unavailable"][key] = why
        else:
            out[f"{key}_n"] = n

    # --- trend participation -------------------------------------------------
    _record("pct_above_200dma", _share_true(rows, "above_200dma"),
            "no above_200dma field on this sample")
    _record("pct_above_50dma", _share_true(rows, "above_50dma"),
            "no above_50dma field on this sample")
    _record("pct_trend_up_50_over_200", _share_true(rows, "trend_up_50_over_200"),
            "no trend_up_50_over_200 field on this sample")

    # --- participation vs the index -----------------------------------------
    # The cleanest narrowing signal available: what fraction is actually BEATING
    # SPY. An index carried by a handful of names shows a low number here while
    # its own level looks fine.
    _record("pct_beating_spy_1m", _share_positive(rows, "rel_strength_1m_pct"),
            "no rel_strength_1m_pct on this sample")
    _record("pct_beating_spy_3m", _share_positive(rows, "rel_strength_3m_pct"),
            "no rel_strength_3m_pct on this sample")

    # --- advance / decline (needs a 1-day bar) -------------------------------
    chg = [_num(r.get("pct_change_1d")) for r in rows]
    chg = [c for c in chg if c is not None]
    if chg:
        adv = sum(1 for c in chg if c > 0)
        dec = sum(1 for c in chg if c < 0)
        out["advancers"] = adv
        out["decliners"] = dec
        out["unchanged"] = len(chg) - adv - dec
        out["net_advancers"] = adv - dec
        out["advance_decline_ratio"] = round(adv / dec, 2) if dec else None
        out["pct_advancing"] = _pct(adv, len(chg))
        out["advance_decline_n"] = len(chg)
    else:
        out["unavailable"]["advance_decline"] = (
            "no pct_change_1d on this sample (weekly sweeps carry no 1-day bar)")

    # --- highs / lows --------------------------------------------------------
    # Proximity to the trailing high, not literal same-session prints: daily bars
    # cannot see intraday extremes, and claiming otherwise would be fabrication.
    dist_field = ("dist_from_52w_high_pct" if any(
        r.get("dist_from_52w_high_pct") is not None for r in rows)
        else "dist_from_6m_high_pct")
    dists = []
    for r in rows:
        d = _num(r.get(dist_field))
        if d is not None:
            dists.append(abs(d))          # both fields are emitted signed/unsigned
    if dists:
        window = "52w" if dist_field.startswith("dist_from_52w") else "6m"
        out["high_low_window"] = window
        out[f"near_{window}_high"] = sum(1 for d in dists if d <= AT_HIGH_PCT)
        out[f"deep_below_{window}_high"] = sum(
            1 for d in dists if d >= DEEP_BELOW_HIGH_PCT)
        out["pct_near_high"] = _pct(
            sum(1 for d in dists if d <= AT_HIGH_PCT), len(dists))
        out["pct_deep_below_high"] = _pct(
            sum(1 for d in dists if d >= DEEP_BELOW_HIGH_PCT), len(dists))
        out["high_low_n"] = len(dists)
        out["high_low_note"] = (
            f"proximity to the trailing {window} high on daily bars — NOT literal "
            f"same-session new highs/lows, which these bars cannot see")
    else:
        out["unavailable"]["highs_lows"] = "no distance-from-high field on this sample"

    # --- distribution --------------------------------------------------------
    for field, key in (("momentum_1m_pct", "median_momentum_1m_pct"),
                       ("momentum_3m_pct", "median_momentum_3m_pct")):
        vals = [v for v in (_num(r.get(field)) for r in rows) if v is not None]
        if vals:
            out[key] = _median(vals)

    out["read"] = _breadth_read(out)
    return out


def _breadth_read(b: dict) -> str:
    """One plain sentence. Explicitly says when it cannot conclude."""
    parts: list[str] = []
    p200 = b.get("pct_above_200dma")
    if p200 is not None:
        if p200 >= 60:
            parts.append(f"{p200}% hold their 200-DMA (broad participation)")
        elif p200 >= 40:
            parts.append(f"{p200}% hold their 200-DMA (mixed)")
        else:
            parts.append(f"only {p200}% hold their 200-DMA (narrow)")
    beat = b.get("pct_beating_spy_1m")
    if beat is not None:
        if beat < 40:
            parts.append(f"just {beat}% beat SPY over 1m — the index is being "
                         f"carried by a minority")
        elif beat > 60:
            parts.append(f"{beat}% beat SPY over 1m — leadership is broad")
        else:
            parts.append(f"{beat}% beat SPY over 1m")
    ad = b.get("advance_decline_ratio")
    if ad is not None:
        parts.append(f"A/D {ad} ({b.get('advancers')}/{b.get('decliners')})")
    if not parts:
        return ("insufficient fields on this sample to read breadth — see "
                "`unavailable` for which measures could not be computed")
    return f"[{b.get('sample')}, n={b.get('n_names')}] " + "; ".join(parts) + "."
