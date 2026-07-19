"""Book-level risk controls: factor stack, gap-adjusted heat, beta, stress.

These pin the four controls added 2026-07-19 after an audit found that every
binding constraint in validator.py was arithmetic on a SINGLE proposal, while
portfolio_beta / shared_left_tail / corr_to_book were computed by tools/correlation.py
and consumed only as prose. Each test asserts the control BINDS and — separately —
that it does not bind when it should not. A one-directional safety test passes just
as well when the implementation is `return "rejected"`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.book_risk import (  # noqa: E402
    factor_stack_breach, factor_stack_risk_breach, gap_adjusted_heat, gap_penalty,
    projected_portfolio_beta, stress_breach, stress_losses,
)

STACK = frozenset({"ai_compute_gpu", "hyperscaler_server_capex", "semi_memory",
                   "datacenter_power", "software_platforms"})


def position(ticker, driver, mv, cost=None, qty=None, stop=None):
    return {"ticker": ticker, "demand_driver": driver, "market_value_usd": mv,
            "avg_cost": cost, "quantity": qty, "plan": {"stop_loss": stop}}


def driver_of(pos):
    return pos.get("demand_driver")


# --------------------------------------------------------------------------- #
# 1. Factor-stack concentration
# --------------------------------------------------------------------------- #
def test_four_different_drivers_inside_one_factor_breach_the_cap():
    """THE gap: the 2% per-driver cap permits ~2 positions each, so four drivers
    inside the same co-moving factor was a 100% AI book passing every hard limit."""
    book = [position("NVDA", "ai_compute_gpu", 1200),
            position("DELL", "hyperscaler_server_capex", 1200),
            position("MU", "semi_memory", 1200),
            position("CEG", "datacenter_power", 1200)]
    breach = factor_stack_breach(book, "software_platforms", 1000, 10_000,
                                 driver_of, STACK, max_pct=0.5)
    assert breach and breach.startswith("factor_stack_concentration_exceeded"), breach


def test_non_stack_driver_is_never_capped_by_the_factor_rule():
    book = [position("NVDA", "ai_compute_gpu", 4500)]
    assert factor_stack_breach(book, "healthcare", 1000, 10_000,
                               driver_of, STACK, max_pct=0.5) is None


def test_stack_exposure_under_the_cap_passes():
    book = [position("NVDA", "ai_compute_gpu", 2000)]
    assert factor_stack_breach(book, "semi_memory", 1000, 10_000,
                               driver_of, STACK, max_pct=0.5) is None


def test_factor_stack_risk_fails_closed_on_an_unstopped_position():
    """A stack position with no parseable stop makes aggregate factor risk unknown.

    Approving there would be a guess in the risk-increasing direction — the same
    reasoning as validator's portfolio_heat_unverifiable.
    """
    book = [position("NVDA", "ai_compute_gpu", 1000, cost=100, qty=10, stop=None)]
    out = factor_stack_risk_breach(
        book, "semi_memory", 100.0, 10_000, driver_of,
        risk_of=lambda p: None, stack_drivers=STACK, max_risk_pct=0.04)
    assert out and out.startswith("factor_stack_risk_unverifiable"), out


# --------------------------------------------------------------------------- #
# 2. Gap-adjusted heat
# --------------------------------------------------------------------------- #
def test_gap_penalty_reflects_the_execution_layers_own_gap_model():
    """0.5 x ATR% x cost x qty — the same model simulated_broker already fills at."""
    book = [position("X", "ai_compute_gpu", 1000, cost=100, qty=10)]
    penalty, missing = gap_penalty(book, lambda p: 4.0, 0.5, avg_pairwise_corr=0.9)
    # 0.5 * 4% * 100 * 10 = 20
    assert penalty == 20.0 and missing == []


def test_heat_is_tightened_never_loosened_by_the_adjustment():
    """Direction matters. The plain sum is already the fully-correlated case, so a
    textbook correlation adjustment would HAND BACK budget. This must only add."""
    book = [position("X", "ai_compute_gpu", 1000, cost=100, qty=10)]
    heat, detail = gap_adjusted_heat(100.0, book, lambda p: 4.0, 0.5,
                                     avg_pairwise_corr=0.9)
    assert heat > 100.0
    assert detail["gap_penalty_usd"] > 0


def test_unknown_correlation_assumes_the_worst():
    """An absent input must never be the cheapest input in a risk control."""
    book = [position("X", "ai_compute_gpu", 1000, cost=100, qty=10)]
    unknown, _ = gap_penalty(book, lambda p: 4.0, 0.5, avg_pairwise_corr=None)
    correlated, _ = gap_penalty(book, lambda p: 4.0, 0.5, avg_pairwise_corr=1.0)
    assert unknown == correlated


def test_a_diversified_book_gaps_less_than_a_correlated_one():
    book = [position("X", "ai_compute_gpu", 1000, cost=100, qty=10)]
    low, _ = gap_penalty(book, lambda p: 4.0, 0.5, avg_pairwise_corr=0.1)
    high, _ = gap_penalty(book, lambda p: 4.0, 0.5, avg_pairwise_corr=0.9)
    assert low < high


def test_missing_atr_is_reported_not_silently_zero():
    book = [position("X", "ai_compute_gpu", 1000, cost=100, qty=10)]
    _, missing = gap_penalty(book, lambda p: None, 0.5)
    assert missing == ["X"]


# --------------------------------------------------------------------------- #
# 3. Portfolio beta
# --------------------------------------------------------------------------- #
def test_beta_is_weighted_against_equity_not_invested_capital():
    """A half-cash book genuinely carries half the market exposure."""
    book = [position("X", "ai_compute_gpu", 5000)]
    beta, missing = projected_portfolio_beta(book, lambda p: 2.0, equity=10_000)
    assert beta == 1.0 and missing == []


def test_beta_includes_the_proposed_position():
    book = [position("X", "ai_compute_gpu", 5000)]
    beta, _ = projected_portfolio_beta(book, lambda p: 1.0, equity=10_000,
                                       proposed_ticker="Y", proposed_size_usd=5000,
                                       proposed_beta=3.0)
    assert beta == 2.0   # (1.0*5000 + 3.0*5000) / 10000


def test_missing_beta_is_reported_so_the_caller_can_fail_closed():
    book = [position("X", "ai_compute_gpu", 5000)]
    _, missing = projected_portfolio_beta(book, lambda p: None, equity=10_000)
    assert missing == ["X"]


# --------------------------------------------------------------------------- #
# 4. Stress scenarios
# --------------------------------------------------------------------------- #
def test_ai_stack_wildcard_shocks_the_whole_factor_group():
    """__ai_stack__ saves a scenario from enumerating 15 driver labels."""
    book = [position("NVDA", "ai_compute_gpu", 3000),
            position("ABBV", "healthcare", 3000)]
    rows = stress_losses(book, [{"name": "ai_break", "market_pct": 0.0,
                                 "driver_shocks": {"__ai_stack__": -0.2}}],
                         driver_of, lambda p: 1.0, 10_000, STACK)
    assert rows[0]["loss_usd"] == 600.0          # only the AI name is hit
    assert rows[0]["contributors"][0]["ticker"] == "NVDA"


def test_market_shock_applies_through_beta():
    book = [position("HI", "healthcare", 1000)]
    rows = stress_losses(book, [{"name": "selloff", "market_pct": -0.1}],
                         driver_of, lambda p: 2.0, 10_000, STACK)
    assert rows[0]["loss_usd"] == 200.0          # 1000 * 2.0 * 10%


def test_stress_loss_is_not_floored_at_the_stop():
    """A shock worth naming is one where stops gap. Flooring at the stop would
    assume away exactly the risk being measured."""
    book = [position("X", "ai_compute_gpu", 1000, cost=100, qty=10, stop=98)]
    rows = stress_losses(book, [{"name": "crash", "market_pct": 0.0,
                                 "driver_shocks": {"__ai_stack__": -0.30}}],
                         driver_of, lambda p: 1.0, 10_000, STACK)
    assert rows[0]["loss_usd"] == 300.0          # not clipped to the 2% stop


def test_long_only_loss_cannot_exceed_market_value():
    book = [position("X", "ai_compute_gpu", 1000)]
    rows = stress_losses(book, [{"name": "wipeout", "market_pct": 0.0,
                                 "driver_shocks": {"__ai_stack__": -5.0}}],
                         driver_of, lambda p: 1.0, 10_000, STACK)
    assert rows[0]["loss_usd"] == 1000.0


def test_stress_breach_reports_the_worst_scenario():
    rows = [{"name": "bad", "loss_pct_of_equity": 0.2, "loss_usd": 2000,
             "contributors": [{"ticker": "NVDA", "loss_usd": 2000}]},
            {"name": "mild", "loss_pct_of_equity": 0.01, "loss_usd": 100,
             "contributors": []}]
    out = stress_breach(rows, 0.12)
    assert out and "bad" in out and "NVDA" in out


def test_stress_within_the_ceiling_passes():
    rows = [{"name": "mild", "loss_pct_of_equity": 0.05, "loss_usd": 500,
             "contributors": []}]
    assert stress_breach(rows, 0.12) is None


def test_scenarios_are_ranked_worst_first():
    book = [position("NVDA", "ai_compute_gpu", 5000)]
    rows = stress_losses(book, [
        {"name": "small", "market_pct": -0.01},
        {"name": "large", "market_pct": -0.20},
    ], driver_of, lambda p: 1.0, 10_000, STACK)
    assert [r["name"] for r in rows] == ["large", "small"]
