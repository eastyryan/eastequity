"""Book-level risk: factor concentration, gap-adjusted heat, beta, stress scenarios.

WHY THIS MODULE EXISTS. Every binding constraint in validator.py is arithmetic on a
single proposal. Every constraint on the SHAPE OF THE BOOK was either an LLM-authored
string (demand_driver) or a number computed and then used only as prose. tools/
correlation.py has produced portfolio_beta, shared_left_tail and corr_to_book since
day one; a grep for `correlation` in validator.py returned nothing. The book is where
money actually dies, and it was the unguarded surface.

Four controls, in the order they matter:

1. FACTOR-STACK CONCENTRATION. factor_map.AI_STACK_DRIVERS classifies 15 of the 27
   canonical drivers as AI-stack. The 2% per-driver risk cap therefore permits ~2
   positions per driver, and a book of 8 positions spread across 4 different AI-stack
   drivers is 100% AI-stack exposure that passes every existing hard limit. The
   per-driver cap was never a factor cap; this is.

2. GAP-ADJUSTED HEAT. _sum_committed_risk is `Σ max(0, (cost-stop)*qty)`, i.e. the
   FULLY-CORRELATED worst case under the assumption that every stop fills exactly at
   its level. That assumption is contradicted inside this system's own config:
   execution_costs.stop_gap_atr_fraction is 0.5, meaning the execution layer models
   stops gapping half an ATR through. The one closed stop in the ledger filled 4.5%
   below its level (DELL, plan stop 408.00, fill 389.75), turning a position sized
   for 1% risk into a -13.5% loss — 42% worse than the budget it was sized against.
   So the 8% heat cap is not an 8% cap; it is 8% plus however far the tape gaps, and
   on a correlated book every stop gaps on the same morning.

   Note the direction carefully: correlation-ADJUSTING the raw sum downward (the
   textbook sqrt(ΣΣρᵢⱼrᵢrⱼ) treatment) would LOOSEN this cap, because ρ=1 already
   recovers the plain sum. Correlation is used here only to decide how much of the
   book gaps TOGETHER — never to hand back budget.

3. PORTFOLIO BETA. A long-only book of high-beta AI names is a leveraged bet on the
   index wearing the costume of eight independent ideas.

4. STRESS SCENARIOS. Named shocks ("chips -20% while SPY is flat") priced against the
   proposed book. Deliberately NOT floored at the stop level: a shock that gaps
   through stops is precisely the event being stressed, and flooring at the stop
   would assume away the thing that makes the scenario dangerous.

Pure. No network, no file reads. Every function is a function of its arguments so the
validator stays deterministic and offline. Fail-CLOSED helpers return an explicit
`unverifiable` list rather than silently treating unknown risk as zero — the pattern
_sum_committed_risk already established after that exact bug.
"""

from __future__ import annotations

# Long-only: a position cannot lose more than its market value.
MAX_LOSS_FRACTION = 1.0


def _f(value):
    """float or None. Never raises."""
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def is_factor_stack(driver: str, stack_drivers) -> bool:
    return str(driver or "").strip().lower() in stack_drivers


# --------------------------------------------------------------------------- #
# 1. Factor-stack concentration
# --------------------------------------------------------------------------- #
def factor_stack_exposure(positions, driver_of, stack_drivers, equity):
    """Aggregate notional exposure to one co-moving factor group.

    `driver_of` is a callable(position) -> driver so the caller controls whether the
    canonical map or the lot stamp wins (validator passes the canonical-preferring
    resolver; see tools.demand_drivers.position_driver).

    Returns {"market_value_usd", "pct_of_equity", "tickers", "by_driver"}.
    """
    eq = _f(equity) or 0.0
    total, tickers, by_driver = 0.0, [], {}
    for pos in (positions or []):
        if not isinstance(pos, dict):
            continue
        driver = str(driver_of(pos) or "").strip().lower()
        if not is_factor_stack(driver, stack_drivers):
            continue
        mv = _f(pos.get("market_value_usd")) or 0.0
        if mv <= 0:
            continue
        ticker = str(pos.get("ticker") or "?").upper()
        total += mv
        if ticker not in tickers:
            tickers.append(ticker)
        by_driver[driver] = round(by_driver.get(driver, 0.0) + mv, 2)
    return {
        "market_value_usd": round(total, 2),
        "pct_of_equity": round(total / eq, 4) if eq > 0 else 0.0,
        "tickers": tickers,
        "by_driver": by_driver,
    }


def factor_stack_breach(positions, proposed_driver, proposed_size_usd, equity,
                        driver_of, stack_drivers, max_pct):
    """Reason string when the proposal pushes factor-stack notional over max_pct.

    Fail-open on unusable inputs, matching demand_drivers.theme_concentration_breach.
    """
    eq, size, cap = _f(equity), _f(proposed_size_usd), _f(max_pct)
    if not eq or eq <= 0 or not size or size <= 0 or not cap or cap <= 0:
        return None
    if not is_factor_stack(proposed_driver, stack_drivers):
        return None
    held = factor_stack_exposure(positions, driver_of, stack_drivers, eq)
    combined = held["market_value_usd"] + size
    if combined > eq * cap + 1e-6:
        drivers = ", ".join(sorted(held["by_driver"])) or "none"
        return (f"factor_stack_concentration_exceeded:ai_stack "
                f"{combined:.0f} > {cap:.0%} of {eq:.0f} "
                f"(held {held['market_value_usd']:.0f} across [{drivers}] "
                f"+ proposed {size:.0f}) — separate demand_drivers inside one "
                f"co-moving factor are ONE bet, not several")
    return None


def factor_stack_risk_breach(positions, proposed_driver, proposed_risk_usd, equity,
                             driver_of, risk_of, stack_drivers, max_risk_pct):
    """Same idea on COMMITTED RISK rather than notional.

    Fails CLOSED: a factor-stack position whose risk cannot be computed makes the
    aggregate unknowable, and approving on unknown risk is a guess in the
    risk-increasing direction. Mirrors validator._check_portfolio_heat.
    """
    eq, risk, cap = _f(equity), _f(proposed_risk_usd), _f(max_risk_pct)
    if not eq or eq <= 0 or not cap or cap <= 0:
        return None
    if risk is None or risk <= 0:
        return None
    if not is_factor_stack(proposed_driver, stack_drivers):
        return None
    total, unverifiable = 0.0, []
    for pos in (positions or []):
        if not isinstance(pos, dict):
            continue
        if not is_factor_stack(driver_of(pos), stack_drivers):
            continue
        r = risk_of(pos)
        if r is None:
            unverifiable.append(str(pos.get("ticker") or "?").upper())
        else:
            total += r
    if unverifiable:
        return (f"factor_stack_risk_unverifiable:{','.join(sorted(unverifiable))} "
                f"— no parseable stop, so aggregate factor risk is unknown")
    if total + risk > eq * cap + 1e-6:
        return (f"factor_stack_risk_exceeded:ai_stack "
                f"{total + risk:.0f} > {cap:.1%} of {eq:.0f}")
    return None


# --------------------------------------------------------------------------- #
# 2. Gap-adjusted heat
# --------------------------------------------------------------------------- #
def gap_penalty(positions, atr_pct_of, gap_atr_fraction, avg_pairwise_corr=None,
                corr_threshold=0.7):
    """Extra loss beyond the stop when stops gap through, in dollars.

    Per position: gap_atr_fraction × ATR% × avg_cost × qty — the same model the
    execution layer already applies on the simulated path
    (simulated_broker._sell_fill_price), so the risk layer and the fill layer finally
    agree about what a stop is worth.

    The correlation input decides how much of the book gaps on the SAME morning.
    Above the threshold, assume all of it does (the shared_left_tail case). Below it,
    apply a linear haircut. Unknown correlation assumes the worst — this is a risk
    control, and an absent input must never be the cheapest input.

    Returns (penalty_usd, [tickers with no usable ATR]).
    """
    frac = _f(gap_atr_fraction)
    if frac is None or frac <= 0:
        return 0.0, []
    corr = _f(avg_pairwise_corr)
    thresh = _f(corr_threshold) or 0.7
    if corr is None or corr >= thresh:
        share = 1.0
    else:
        share = max(0.0, min(1.0, corr / thresh)) if thresh > 0 else 1.0

    total, missing = 0.0, []
    for pos in (positions or []):
        if not isinstance(pos, dict):
            continue
        ticker = str(pos.get("ticker") or "?").upper()
        atr_pct = _f(atr_pct_of(pos))
        cost = _f(pos.get("avg_cost"))
        qty = _f(pos.get("quantity"))
        if atr_pct is None or atr_pct <= 0:
            missing.append(ticker)
            continue
        if not cost or not qty or cost <= 0 or qty <= 0:
            continue
        total += frac * (atr_pct / 100.0) * cost * qty
    return round(total * share, 2), missing


def gap_adjusted_heat(committed_risk_usd, positions, atr_pct_of, gap_atr_fraction,
                      avg_pairwise_corr=None, corr_threshold=0.7):
    """(heat_usd, detail). Committed risk plus the modelled gap-through.

    `detail` carries the pieces so a rejection reason can explain itself and the
    dashboard can show why the effective cap is tighter than the nominal one.
    """
    base = _f(committed_risk_usd) or 0.0
    penalty, missing_atr = gap_penalty(
        positions, atr_pct_of, gap_atr_fraction,
        avg_pairwise_corr=avg_pairwise_corr, corr_threshold=corr_threshold)
    return round(base + penalty, 2), {
        "committed_risk_usd": round(base, 2),
        "gap_penalty_usd": penalty,
        "avg_pairwise_corr": _f(avg_pairwise_corr),
        "tickers_without_atr": missing_atr,
    }


# --------------------------------------------------------------------------- #
# 3. Portfolio beta
# --------------------------------------------------------------------------- #
def projected_portfolio_beta(positions, beta_of, equity, proposed_ticker=None,
                             proposed_size_usd=0.0, proposed_beta=None):
    """(beta, [tickers with no beta]) for the book INCLUDING the proposal.

    Market-value weighted against EQUITY, not against invested capital: a book that
    is half cash genuinely carries half the market exposure, and weighting by
    invested-only would report a fully-invested beta for a 50%-cash book.
    """
    eq = _f(equity)
    if not eq or eq <= 0:
        return None, []
    weighted, missing = 0.0, []
    for pos in (positions or []):
        if not isinstance(pos, dict):
            continue
        ticker = str(pos.get("ticker") or "?").upper()
        mv = _f(pos.get("market_value_usd")) or 0.0
        if mv <= 0:
            continue
        b = _f(beta_of(pos))
        if b is None:
            missing.append(ticker)
            continue
        weighted += b * mv
    size = _f(proposed_size_usd) or 0.0
    if proposed_ticker and size > 0:
        pb = _f(proposed_beta)
        if pb is None:
            missing.append(str(proposed_ticker).upper())
        else:
            weighted += pb * size
    return round(weighted / eq, 3), missing


# --------------------------------------------------------------------------- #
# 4. Stress scenarios
# --------------------------------------------------------------------------- #
def _shock_for(driver, beta, scenario, stack_drivers):
    """Fractional price shock for one position under one scenario.

    market_pct applies through beta; driver_shocks apply on top. The reserved key
    "__ai_stack__" shocks every driver in the factor group at once, so a scenario
    does not have to enumerate 15 labels to say "the AI trade breaks".
    """
    market = _f(scenario.get("market_pct")) or 0.0
    b = _f(beta)
    shock = market * (b if b is not None else 1.0)
    shocks = scenario.get("driver_shocks") or {}
    d = str(driver or "").strip().lower()
    if d in shocks:
        shock += _f(shocks[d]) or 0.0
    elif "__ai_stack__" in shocks and is_factor_stack(d, stack_drivers):
        shock += _f(shocks["__ai_stack__"]) or 0.0
    return shock


def stress_losses(positions, scenarios, driver_of, beta_of, equity, stack_drivers,
                  proposed=None):
    """Loss under each named scenario, as dollars and as a fraction of equity.

    `proposed` is an optional {"ticker","market_value_usd","demand_driver","beta"}
    so the check prices the book the proposal WOULD create, not the one it has.

    Losses are NOT floored at each position's stop. A scenario severe enough to be
    worth naming is a scenario in which stops gap; flooring at the stop would assume
    away exactly the risk being measured. Losses are capped at position market value
    (long-only cannot exceed -100%).
    """
    eq = _f(equity)
    if not eq or eq <= 0:
        return []
    rows = []
    book = list(positions or [])
    for scenario in (scenarios or []):
        if not isinstance(scenario, dict):
            continue
        loss, contributors = 0.0, []
        for pos in book:
            if not isinstance(pos, dict):
                continue
            mv = _f(pos.get("market_value_usd")) or 0.0
            if mv <= 0:
                continue
            shock = _shock_for(driver_of(pos), beta_of(pos), scenario, stack_drivers)
            if shock >= 0:
                continue
            hit = min(abs(shock), MAX_LOSS_FRACTION) * mv
            loss += hit
            contributors.append({"ticker": str(pos.get("ticker") or "?").upper(),
                                 "loss_usd": round(hit, 2)})
        if proposed:
            mv = _f(proposed.get("market_value_usd")) or 0.0
            if mv > 0:
                shock = _shock_for(proposed.get("demand_driver"),
                                   proposed.get("beta"), scenario, stack_drivers)
                if shock < 0:
                    hit = min(abs(shock), MAX_LOSS_FRACTION) * mv
                    loss += hit
                    contributors.append({
                        "ticker": str(proposed.get("ticker") or "?").upper(),
                        "loss_usd": round(hit, 2), "proposed": True})
        contributors.sort(key=lambda r: r["loss_usd"], reverse=True)
        rows.append({
            "name": str(scenario.get("name") or "unnamed"),
            "loss_usd": round(loss, 2),
            "loss_pct_of_equity": round(loss / eq, 4),
            "contributors": contributors[:5],
        })
    rows.sort(key=lambda r: r["loss_pct_of_equity"], reverse=True)
    return rows


def stress_breach(rows, max_loss_pct):
    """Reason string for the worst scenario exceeding the ceiling, else None."""
    cap = _f(max_loss_pct)
    if not cap or cap <= 0 or not rows:
        return None
    worst = rows[0]
    if worst["loss_pct_of_equity"] > cap + 1e-9:
        top = ", ".join(f"{c['ticker']} {c['loss_usd']:.0f}"
                        for c in worst["contributors"][:3])
        return (f"stress_scenario_loss_exceeded:{worst['name']} "
                f"{worst['loss_pct_of_equity']:.1%} > {cap:.1%} of equity "
                f"(worst contributors: {top})")
    return None
