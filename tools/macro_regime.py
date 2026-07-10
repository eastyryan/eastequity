"""Macro Regime Tool — FRED-based regime snapshot for long swing exposure.

Answers one question for the brain: is the macro environment supportive,
neutral, or hostile for adding long swing exposure to AI/data-center equities?

Requires FRED_API_KEY in .env (free key: https://fred.stlouisfed.org/docs/api/api_key.html).
Degrades gracefully: if the key or network is missing, returns status="unavailable"
so Claude knows to reason without it rather than hallucinate.

CLI: python -m tools.macro_regime
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

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


def get_macro_snapshot() -> dict:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return {"status": "unavailable", "reason": "FRED_API_KEY not set in environment/.env"}
    try:
        from fredapi import Fred
        fred = Fred(api_key=api_key)
        start = datetime.now() - timedelta(days=900)
        out: dict = {"status": "ok", "as_of": datetime.now().date().isoformat(), "indicators": {}}

        for name, sid in SERIES.items():
            try:
                s = fred.get_series(sid, observation_start=start).dropna()
                if s.empty:
                    continue
                latest = float(s.iloc[-1])
                prior_3m = float(s.iloc[-64]) if len(s) > 64 else float(s.iloc[0])
                if name == "cpi_yoy_proxy" and len(s) > 16:
                    latest = round((float(s.iloc[-1]) / float(s.iloc[-13]) - 1) * 100, 2)
                    prior_3m = round((float(s.iloc[-4]) / float(s.iloc[-16]) - 1) * 100, 2)
                    name = "cpi_yoy_pct"
                out["indicators"][name] = {
                    "latest": round(latest, 2),
                    "three_months_ago": round(prior_3m, 2),
                    "direction": "rising" if latest > prior_3m else "falling",
                }
            except Exception as e:  # one bad series shouldn't kill the snapshot
                out["indicators"][name] = {"error": str(e)}

        ind = out["indicators"]
        if "latest" in ind.get("ten_year_yield", {}) and "latest" in ind.get("two_year_yield", {}):
            yc = round(ind["ten_year_yield"]["latest"] - ind["two_year_yield"]["latest"], 2)
            out["indicators"]["yield_curve_10y2y"] = {"latest": yc}

        if not any("latest" in v for v in ind.values()):
            return {"status": "unavailable",
                    "reason": "FRED unreachable for all series (network/policy restriction)",
                    "series_errors": {k: v.get("error") for k, v in ind.items() if "error" in v}}

        # Simple deterministic regime score — a hint, not a verdict. Claude interprets.
        score = 0
        if ind.get("hy_credit_spread", {}).get("latest", 5) < 4.0: score += 1
        if ind.get("vix", {}).get("latest", 25) < 20: score += 1
        if ind.get("nfci", {}).get("latest", 1) < 0: score += 1
        if ind.get("fed_funds_rate", {}).get("direction") == "falling": score += 1
        if ind.get("cpi_yoy_pct", {}).get("direction") == "falling": score += 1
        out["regime_hint"] = {
            "score_0_to_5": score,
            "label": "supportive" if score >= 4 else "neutral" if score >= 2 else "hostile",
            "note": "Deterministic hint only — the agent must form its own regime view.",
        }
        return out
    except Exception as e:
        return {"status": "unavailable", "reason": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(json.dumps(get_macro_snapshot(), indent=2))
