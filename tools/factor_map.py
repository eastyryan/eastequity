"""Factor map — book-level theme concentration + pre-committed unwind playbook.

Answers: what single narrative would hit most of the equity book, how stacked
is AI-stack factor risk, and what the agent should do *before* an unwind
(not after). Complements portfolio_risk (pairwise corr) with demand_driver
economics.

Pure helpers offline-testable. No network.

CLI: python -m tools.factor_map
"""

from __future__ import annotations

from typing import Any

from tools.demand_drivers import build_driver_map, theme_exposure

NOTE = (
    "PROFIT DISCIPLINE: geometric return dies when one factor erases a multi-month "
    "run. Read concentration_level + what_kills_the_book + unwind_playbook BEFORE "
    "adding risk. When concentration is high/extreme, output factor_response in "
    "JSON acknowledging the map and the plan (cash / no new same-theme / trim heat / "
    "rotate layer). Staying AI-focused is fine — but size and new BUYs must respect "
    "the playbook."
)

# Drivers that typically co-move on an "AI buildout / semis / DC" shock.
AI_STACK_DRIVERS = frozenset({
    "hyperscaler_server_capex",
    "ai_compute_gpu",
    "semi_memory",
    "semi_foundry_analog",
    "semi_equipment",
    "semi_design_ip",
    "networking",
    "datacenter_power",
    "datacenter_reit",
    "datacenter_construction",
    "hyperscaler_cloud_infra",
    "ai_supplier_other",
    "software_platforms",  # often AI-adjacent beta in this universe
    "mega_tech_platforms",
    "cybersecurity",
})

# Harder core: memory + GPU + servers tend to be the left-tail cluster.
AI_CORE_DRIVERS = frozenset({
    "hyperscaler_server_capex",
    "ai_compute_gpu",
    "semi_memory",
    "networking",
    "datacenter_power",
    "ai_supplier_other",
})


def _pct(x) -> float:
    try:
        v = float(x)
        return v if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0


def concentration_level(
    top_driver_pct: float,
    ai_stack_pct: float,
    ai_core_pct: float,
    n_positions: int,
    shared_left_tail: bool | None,
) -> str:
    """low | moderate | high | extreme."""
    if n_positions <= 0:
        return "empty"
    if (
        top_driver_pct >= 0.50
        or ai_core_pct >= 0.75
        or (ai_stack_pct >= 0.90 and n_positions >= 2)
        or (shared_left_tail and ai_stack_pct >= 0.60)
    ):
        return "extreme"
    if (
        top_driver_pct >= 0.35
        or ai_stack_pct >= 0.70
        or ai_core_pct >= 0.55
        or (shared_left_tail and n_positions >= 2)
    ):
        return "high"
    if top_driver_pct >= 0.25 or ai_stack_pct >= 0.50 or ai_core_pct >= 0.40:
        return "moderate"
    return "low"


def build_unwind_playbook(
    level: str,
    top_driver: str | None,
    top_driver_pct: float,
    ai_stack_pct: float,
    orthogonal_tickers: list[str],
    shared_left_tail: bool | None,
) -> dict:
    """Pre-committed actions the brain should follow at each concentration level."""
    td = top_driver or "dominant_theme"

    base_triggers = [
        "AI/semiconductor factor unwind: leaders gap down together while SPY is flat/up",
        "Momentum-health / factor-unwind flag if present in momentum_health",
        f"Thesis invalidators firing on 2+ names in {td}",
        "Macro regime flips hostile with rising real yields / risk-off in growth",
    ]

    if level in ("empty", "low"):
        actions = [
            "Normal fat-pitch bar; theme cap still binds.",
            "Prefer adding a *different* demand_driver when RR is comparable.",
        ]
        new_buy = (
            "New BUYs allowed within theme and risk caps. Prefer diversifying "
            "drivers when two ideas have similar RR."
        )
    elif level == "moderate":
        actions = [
            "Do not add a third name that deepens the same top demand_driver unless "
            "it is a clear fat pitch with distinct catalyst.",
            "Size new same-theme risk at the low end of the risk budget.",
            "Re-check portfolio_risk.shared_left_tail before any add.",
        ]
        new_buy = (
            "Same-theme adds require stronger variant_perception + geometry than "
            "cross-theme adds. Prefer orthogonal seats when available."
        )
    elif level == "high":
        actions = [
            f"No new BUY that increases {td} weight unless swapping (sell/trim first).",
            "Trim or reduce the weakest same-theme seat if competition marks it under_pressure.",
            "Raise cash toward min reserve + optional dry powder rather than force a same-theme add.",
            "If momentum_health flags unwind, cut theme heat before averaging down.",
        ]
        new_buy = (
            "Default: only diversifying demand_drivers or true swaps. Same-theme "
            "BUY only after an explicit trim/sell frees theme room AND the setup "
            "is top-tier."
        )
    else:  # extreme
        actions = [
            "PRIORITY: reduce one-factor risk — trim/sell weakest seat(s) in the "
            f"dominant cluster ({td}) before any new risk.",
            "New same-theme BUY is presumptively wrong; require a swap with net "
            "theme heat flat or down.",
            "Park freed capital in cash until concentration_level falls to high or below.",
            "Do not 'average down the theme' on a factor washout.",
        ]
        new_buy = (
            "Extreme concentration: net theme heat must not rise. Only cross-theme "
            "or risk-reducing swaps. Prefer cash over forced activity."
        )

    if shared_left_tail:
        actions = list(actions) + [
            "shared_left_tail=true: treat the book as ONE bet for risk sizing; "
            "corr>0.7 candidates need diversifier-level justification."
        ]

    return {
        "concentration_level": level,
        "triggers": base_triggers,
        "actions": actions,
        "new_buy_rules": new_buy,
        "orthogonal_seats_to_protect": orthogonal_tickers[:8],
        "ai_stack_pct_of_equity": round(ai_stack_pct, 4),
        "top_driver_pct_of_equity": round(top_driver_pct, 4),
    }


def what_kills_the_book(
    top_driver: str | None,
    top_tickers: list[str],
    ai_stack_pct: float,
    shared_left_tail: bool | None,
) -> str:
    if not top_driver:
        return "Empty or unmapped book — no single factor map yet."
    names = ", ".join(top_tickers[:4]) if top_tickers else "the dominant theme names"
    tail = (
        " Pairwise correlations confirm a shared left tail."
        if shared_left_tail else ""
    )
    return (
        f"A sharp selloff in demand_driver={top_driver} "
        f"({names}; ~{ai_stack_pct:.0%} of equity in AI-stack-linked drivers) "
        f"would hit most of the book at once.{tail}"
    )


def build_factor_map(
    portfolio: dict | None,
    *,
    driver_map: dict[str, str] | None = None,
    portfolio_risk: dict | None = None,
    theme_cap_pct: float | None = None,
) -> dict:
    """Assemble factor map + unwind playbook for the context bundle."""
    positions = []
    equity = None
    if isinstance(portfolio, dict):
        positions = [p for p in (portfolio.get("positions") or []) if isinstance(p, dict)]
        equity = portfolio.get("total_equity_usd")

    dmap = driver_map if isinstance(driver_map, dict) else build_driver_map()
    buckets = theme_exposure(positions, dmap, equity=equity)

    if not positions or not buckets:
        return {
            "status": "empty_book",
            "note": NOTE,
            "concentration_level": "empty",
            "theme_buckets": [],
            "unwind_playbook": build_unwind_playbook("empty", None, 0, 0, [], None),
            "what_kills_the_book": "No open positions.",
            "how_to_respond": "Factor map idle until the book has positions.",
        }

    top = buckets[0]
    top_driver = top.get("demand_driver")
    top_pct = _pct(top.get("pct_of_equity"))
    top_tickers = list(top.get("tickers") or [])

    ai_stack_mv = sum(
        _pct(b.get("market_value_usd"))
        for b in buckets if b.get("demand_driver") in AI_STACK_DRIVERS
    )
    ai_core_mv = sum(
        _pct(b.get("market_value_usd"))
        for b in buckets if b.get("demand_driver") in AI_CORE_DRIVERS
    )
    total_mv = sum(_pct(b.get("market_value_usd")) for b in buckets) or 1.0
    eq = _pct(equity) if equity else total_mv
    if eq <= 0:
        eq = total_mv
    ai_stack_pct = ai_stack_mv / eq
    ai_core_pct = ai_core_mv / eq

    # Orthogonal = not in the single largest driver bucket
    orthogonal = []
    for b in buckets[1:]:
        for t in b.get("tickers") or []:
            if t not in top_tickers:
                orthogonal.append(t)

    pr = portfolio_risk if isinstance(portfolio_risk, dict) else {}
    shared = pr.get("shared_left_tail")
    if shared is not None:
        shared = bool(shared)
    avg_corr = pr.get("avg_pairwise_holding_corr")
    port_beta = pr.get("portfolio_beta")

    level = concentration_level(
        top_pct, ai_stack_pct, ai_core_pct, len(positions), shared)

    playbook = build_unwind_playbook(
        level, top_driver, top_pct, ai_stack_pct, orthogonal, shared)

    requires_response = level in ("high", "extreme")

    return {
        "status": "ok",
        "note": NOTE,
        "concentration_level": level,
        "requires_factor_response": requires_response,
        "theme_buckets": buckets,
        "top_driver": top_driver,
        "top_driver_pct_of_equity": round(top_pct, 4),
        "top_driver_tickers": top_tickers,
        "ai_stack_pct_of_equity": round(ai_stack_pct, 4),
        "ai_core_pct_of_equity": round(ai_core_pct, 4),
        "n_positions": len(positions),
        "n_drivers": len(buckets),
        "orthogonal_tickers": orthogonal,
        "shared_left_tail": shared,
        "avg_pairwise_holding_corr": avg_corr,
        "portfolio_beta": port_beta,
        "theme_concentration_cap_pct": theme_cap_pct,
        "what_kills_the_book": what_kills_the_book(
            top_driver, top_tickers, ai_stack_pct, shared),
        "unwind_playbook": playbook,
        "how_to_respond": (
            "When requires_factor_response is true, output factor_response: "
            "{concentration_level, plan (1-3 sentences), actions: [trim|cash|no_same_theme_buy|rotate|hold_plan]} "
            "aligned with unwind_playbook. Even when false, use the map before any BUY "
            "that would raise top_driver or ai_stack weight."
        ),
    }


def validate_factor_response(
    factor_response: Any,
    *,
    required: bool,
    expected_level: str | None = None,
    min_plan_chars: int = 20,
) -> tuple[dict | None, list[str]]:
    """Normalize brain factor_response. Returns (clean_or_none, issues)."""
    issues: list[str] = []
    if not required:
        if factor_response is None:
            return None, []
        # Optional but if present, shape-check lightly
    if factor_response is None:
        return None, ["factor_response_missing"] if required else []
    if not isinstance(factor_response, dict):
        return None, ["factor_response_not_object"]

    plan = str(factor_response.get("plan") or "").strip()
    level = str(factor_response.get("concentration_level") or expected_level or "").strip().lower()
    actions = factor_response.get("actions")
    if isinstance(actions, str):
        actions = [actions]
    if not isinstance(actions, list):
        actions = []
    actions = [str(a).strip().lower() for a in actions if a]

    if required and len(plan) < min_plan_chars:
        issues.append("factor_response_plan_weak")
    if required and not actions:
        issues.append("factor_response_actions_empty")

    known = frozenset({
        "trim", "cash", "no_same_theme_buy", "rotate", "hold_plan",
        "swap", "reduce_heat", "wait",
    })
    for a in actions:
        if a not in known:
            # allow free-form but flag unknown soft
            issues.append(f"factor_response_action_unrecognized:{a}")

    clean = {
        "concentration_level": level or (expected_level or "unknown"),
        "plan": plan[:600],
        "actions": actions[:8],
    }
    return clean, issues


if __name__ == "__main__":
    import json
    from tools.portfolio_state import get_portfolio_state
    print(json.dumps(build_factor_map(get_portfolio_state()), indent=2))
