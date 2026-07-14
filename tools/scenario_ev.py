"""Honest expected-value math over a proposal's bull/base/bear scenarios.

Pure stdlib, dict-in/dict-out, offline. The brain states ONE holding horizon
per proposal, so this deliberately does NOT fabricate 1M/3M/12M return
ladders: ev_pct is the probability-weighted return over the stated scenarios
at the stated entry, and the only annualization offered is the trivial
365/horizon scaling — emitted only when the horizon is a sane 3-365 days.

Tolerates missing keys and string numbers; unusable inputs fail soft to a
warning with all values None.
"""

from __future__ import annotations

_VALUE_KEYS = ("ev_pct", "ev_annualized_pct", "prob_weighted_upside_pct",
               "prob_weighted_downside_pct", "upside_downside_ratio", "prob_sum")


def _f(x):
    """Coerce to float; None on failure/NaN."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN guard


def _unusable(warning: str) -> dict:
    out = {k: None for k in _VALUE_KEYS}
    out["warning"] = warning
    return out


def expected_value(scenarios, entry_price, holding_horizon_days=None) -> dict:
    """Probability-weighted return math for {"bull": {"price": P, "prob": p}, ...}
    vs an entry price, over one stated holding horizon."""
    entry = _f(entry_price)
    if entry is None or entry <= 0:
        return _unusable("bad_entry_price")

    rows = []  # (price, prob) for every usable scenario
    if isinstance(scenarios, dict):
        for sc in scenarios.values():
            if not isinstance(sc, dict):
                continue
            price, prob = _f(sc.get("price")), _f(sc.get("prob"))
            if price is None or prob is None or price <= 0 or prob < 0:
                continue
            rows.append((price, prob))
    if not rows:
        return _unusable("missing_scenarios")

    prob_sum = sum(p for _, p in rows)
    if prob_sum <= 0:
        return _unusable("missing_scenarios")
    # NORMALIZE by prob_sum: probabilities that sum to 0.9 or 1.2 otherwise
    # scale the EV magnitude by that same factor, publishing a confidently
    # wrong number next to a soft warning. Normalization is a no-op when the
    # scenarios already sum to 1; the warning below still discloses the slip.
    ev = sum(p * (price / entry - 1.0) for price, p in rows) / prob_sum
    up = sum(p * (price / entry - 1.0) for price, p in rows if price > entry) / prob_sum
    down = sum(p * (price / entry - 1.0) for price, p in rows if price < entry) / prob_sum

    horizon = _f(holding_horizon_days)
    ev_ann = (ev * 365.0 / horizon
              if horizon is not None and 3 <= horizon <= 365 else None)

    ratio = abs(up / down) if down != 0 else None

    warning = None
    if not (0.95 <= prob_sum <= 1.05):
        warning = f"scenario_probs_sum_to_{round(prob_sum, 2)}"

    return {
        "ev_pct": round(ev * 100, 2),
        "ev_annualized_pct": round(ev_ann * 100, 2) if ev_ann is not None else None,
        "prob_weighted_upside_pct": round(up * 100, 2),
        "prob_weighted_downside_pct": round(down * 100, 2),
        "upside_downside_ratio": round(ratio, 2) if ratio is not None else None,
        "prob_sum": round(prob_sum, 2),
        "warning": warning,
    }
