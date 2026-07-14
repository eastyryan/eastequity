"""Macro Regime Tool — FRED-based regime snapshot for long swing exposure.

Answers one question for the brain: is the macro environment supportive,
neutral, or hostile for adding long swing exposure to AI/data-center equities?

Requires FRED_API_KEY in .env (free key: https://fred.stlouisfed.org/docs/api/api_key.html).
Degrades gracefully: if the key or network is missing, returns status="unavailable"
so Claude knows to reason without it rather than hallucinate.

The "3-months-ago" comparison is computed BY DATE per series (see _prior_index_by_date),
so it is correct whether a series is daily (DGS10/VIXCLS), weekly (NFCI), or monthly
(UNRATE/CPIAUCSL). A fixed index offset would compare years apart for monthly series.

CLI: python -m tools.macro_regime
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

SERIES = {
    "fed_funds_rate": "DFF",
    "ten_year_yield": "DGS10",
    "two_year_yield": "DGS2",
    "cpi_yoy_proxy": "CPIAUCSL",       # index level; we compute YoY
    "unemployment_rate": "UNRATE",
    "hy_credit_spread": "BAMLH0A0HYM2",
    "vix": "VIXCLS",
    "nfci": "NFCI",                    # Chicago Fed financial conditions (<0 = loose)
}

# Native units per FRED series id, surfaced additively so the dashboard/brain can
# label each value without guessing. Frequency is inferred from the observations.
SERIES_META = {
    "DFF":          {"units": "percent"},
    "DGS10":        {"units": "percent"},
    "DGS2":         {"units": "percent"},
    "CPIAUCSL":     {"units": "index_1982_84=100"},
    "UNRATE":       {"units": "percent"},
    "BAMLH0A0HYM2": {"units": "percent"},   # high-yield option-adjusted spread
    "VIXCLS":       {"units": "index"},
    "NFCI":         {"units": "index_zscore"},
}


# --- Pure-python date/frequency helpers (no pandas — offline-testable) ---------

def _to_date(x) -> date:
    """Coerce a FRED index label to a datetime.date.

    Accepts date/datetime, a pandas Timestamp (has .to_pydatetime), or an ISO
    string. Kept pandas-free so the comparison logic is unit-testable offline.
    """
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    to_py = getattr(x, "to_pydatetime", None)
    if callable(to_py):
        return to_py().date()
    return datetime.fromisoformat(str(x)[:10]).date()


def _closest_index(dates: list, target: date) -> int:
    """Index of the observation whose date is closest to `target` (abs gap)."""
    best_i, best_gap = 0, None
    for i, d in enumerate(dates):
        gap = abs((d - target).days)
        if best_gap is None or gap < best_gap:
            best_i, best_gap = i, gap
    return best_i


def _prior_index_by_date(dates: list, days_back: int = 90) -> int:
    """Index of the observation closest to ~`days_back` days before the latest.

    Latest is the last (max-date) observation, so this returns a genuine
    ~3-months-ago point regardless of series frequency. If the series does not
    reach that far back, it returns the earliest observation (index 0).
    """
    if not dates:
        return 0
    return _closest_index(dates, dates[-1] - timedelta(days=days_back))


def _infer_frequency(dates: list) -> str:
    """Best-effort native frequency from the median spacing between observations."""
    if len(dates) < 3:
        return "unknown"
    gaps = sorted((dates[i] - dates[i - 1]).days for i in range(1, len(dates)))
    mid = gaps[len(gaps) // 2]
    if mid <= 3:
        return "daily"
    if mid <= 10:
        return "weekly"
    if mid <= 45:
        return "monthly"
    if mid <= 100:
        return "quarterly"
    return "annual"


def _cpi_yoy(dates: list, vals: list, prior_i: int):
    """Year-over-year CPI (%) now and ~3 months ago, anchoring the year-ago
    comparison BY DATE so a missing monthly print cannot skew it.

    Returns (latest_yoy_pct, prior3m_yoy_pct, ok). ok is False when there is
    not ~12 months of history to compare against.
    """
    yb = _closest_index(dates, dates[-1] - timedelta(days=365))
    yb3 = _closest_index(dates, dates[prior_i] - timedelta(days=365))
    if (dates[-1] - dates[yb]).days < 300 or (dates[prior_i] - dates[yb3]).days < 300:
        return vals[-1], vals[prior_i], False
    if not vals[yb] or not vals[yb3]:
        return vals[-1], vals[prior_i], False
    latest = (vals[-1] / vals[yb] - 1) * 100
    prior = (vals[prior_i] / vals[yb3] - 1) * 100
    return latest, prior, True


def get_macro_snapshot() -> dict:
    api_key = os.environ.get("FRED_API_KEY")
    try:
        import pandas as pd
        from tools.net import get_fred_observations
        start = datetime.now() - timedelta(days=900)
        start_str = start.date().isoformat()
        fred = None
        if api_key:
            try:
                from fredapi import Fred
                fred = Fred(api_key=api_key)
            except Exception:
                fred = None

        def fetch_series(sid: str):
            """Direct fredapi first; fall back to our Vercel proxy (cloud sandbox)."""
            if fred is not None:
                try:
                    return fred.get_series(sid, observation_start=start)
                except Exception:
                    pass
            obs = get_fred_observations(sid, start_str)
            vals = {o["date"]: float(o["value"]) for o in obs if o.get("value") not in (".", None)}
            return pd.Series(vals)

        out: dict = {"status": "ok", "as_of": datetime.now().date().isoformat(), "indicators": {}}

        for name, sid in SERIES.items():
            try:
                s = fetch_series(sid).dropna().sort_index()
                if s.empty:
                    continue
                dates = [_to_date(ix) for ix in s.index]
                vals = [float(v) for v in s.values]

                prior_i = _prior_index_by_date(dates, days_back=90)
                latest = vals[-1]
                prior_3m = vals[prior_i]
                out_name = name
                units = SERIES_META.get(sid, {}).get("units")

                if sid == "CPIAUCSL":
                    latest, prior_3m, ok = _cpi_yoy(dates, vals, prior_i)
                    if ok:
                        out_name = "cpi_yoy_pct"
                        units = "percent_yoy"

                latest = round(latest, 2)
                prior_3m = round(prior_3m, 2)
                out["indicators"][out_name] = {
                    "latest": latest,
                    "three_months_ago": prior_3m,
                    "direction": "rising" if latest > prior_3m else "falling",
                    "as_of": dates[-1].isoformat(),
                    "units": units,
                    "frequency": _infer_frequency(dates),
                }
            except Exception as e:  # one bad series shouldn't kill the snapshot
                out["indicators"][name] = {"error": str(e)}

        ind = out["indicators"]
        ten, two = ind.get("ten_year_yield", {}), ind.get("two_year_yield", {})
        if "latest" in ten and "latest" in two:
            slope = round(ten["latest"] - two["latest"], 2)
            yc = {
                "latest": slope,
                "units": "percentage_points",
                "as_of": max(ten.get("as_of", ""), two.get("as_of", "")) or None,
                "inverted": slope < 0,
            }
            if "three_months_ago" in ten and "three_months_ago" in two:
                prior_slope = round(ten["three_months_ago"] - two["three_months_ago"], 2)
                yc["three_months_ago"] = prior_slope
                yc["direction"] = "steepening" if slope > prior_slope else "flattening"
            out["indicators"]["yield_curve_10y2y"] = yc

        if not any("latest" in v for v in ind.values()):
            return {"status": "unavailable",
                    "reason": "FRED unreachable for all series (network/policy restriction)",
                    "series_errors": {k: v.get("error") for k, v in ind.items() if "error" in v}}

        # Simple deterministic regime score — a hint, not a verdict. Claude interprets.
        # Missing series score with deliberately-hostile defaults (never wrongly
        # encourages buying), but the hint must SAY how much of it ran on
        # placeholders - a mostly-defaulted label is not a macro read.
        missing = sorted(k for k, v in ind.items() if "latest" not in v)
        score = 0
        if ind.get("hy_credit_spread", {}).get("latest", 5) < 4.0: score += 1
        if ind.get("vix", {}).get("latest", 25) < 20: score += 1
        if ind.get("nfci", {}).get("latest", 1) < 0: score += 1
        if ind.get("fed_funds_rate", {}).get("direction") == "falling": score += 1
        if ind.get("cpi_yoy_pct", {}).get("direction") == "falling": score += 1
        out["regime_hint"] = {
            "score_0_to_5": score,
            "label": "supportive" if score >= 4 else "neutral" if score >= 2 else "hostile",
            "series_missing": missing or None,
            "note": ("Deterministic hint only — the agent must form its own regime view."
                     + (f" CAUTION: {len(missing)} series failed to load and scored "
                        f"on conservative defaults ({', '.join(missing)})."
                        if missing else "")),
        }
        return out
    except Exception as e:
        return {"status": "unavailable", "reason": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(json.dumps(get_macro_snapshot(), indent=2))
