"""The volatility and market-cap floors can be made to fail closed.

Both currently fail OPEN on missing data, which means a proposal on a ticker absent
from market_context skips the stop noise-band check entirely — a 1% stop on an
8%-ATR name passes — and a name with no market-cap figure clears the $1B floor by
default. A "hard floor" that waves through every name whose size it cannot read is
not a floor.

The enforcement is built and OFF by default. These tests pin the mechanism so
enabling it later is a config flip against proven behaviour rather than a leap. See
book_risk._tradeability_note for the evidence each toggle is waiting on: ATR coverage
is already 182/187 with zero universe names absent; market cap is resolved per
proposal via a live lookup and could block a whole run on a network hiccup.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator  # noqa: E402
from tools.demand_drivers import driver_for_ticker  # noqa: E402


def buy(ticker="NVDA", size=900.0, entry=100.0, stop=92.0, target=120.0):
    return {
        "ticker": ticker, "action": "BUY", "instrument": "EQUITY",
        "position_size_usd": size, "entry_price_max": entry, "stop_loss": stop,
        "target_price": target, "holding_horizon_days": 30, "confidence": 0.7,
        "risk_reward_ratio": 2.5,
        "thesis": "Clean swing setup with a real catalyst and rising estimates.",
        "macro_context": "Regime supports adding measured long exposure.",
        "variant_perception": ("Consensus sees a fair multiple; I see mispriced "
                               "growth because estimates lag; resolves next print."),
        "thesis_invalidators": {
            "invalidating_print": "EPS guide cut or estimate cuts >5%",
            "invalidating_structure": "Close below the 50-DMA on rising volume",
            "time_box": "If no progress toward thesis in 25 trading days, exit"},
        "demand_driver": driver_for_ticker(ticker),
        "catalysts": ["Earnings in ~3 weeks"],
        "risk_map": "Stop below recent structure; invalid if leadership fails.",
        "scenarios": {"bull": {"price": entry * 1.25, "prob": 0.3},
                      "base": {"price": entry * 1.12, "prob": 0.5},
                      "bear": {"price": entry * 0.92, "prob": 0.2}},
    }


def _cfg_with(monkeypatch, **book_risk):
    real = validator.load_config

    def patched():
        cfg = real()
        cfg.setdefault("book_risk", {}).update(book_risk)
        return cfg
    monkeypatch.setattr(validator, "load_config", patched)


def _reasons(monkeypatch, market_context, **book_risk):
    _cfg_with(monkeypatch, **book_risk)
    pf = {"total_equity_usd": 10_000.0, "cash_usd": 10_000.0, "positions": []}
    return validator.validate_proposals([buy()], pf, market_context)[0].reasons


def test_missing_volatility_data_is_rejected_when_required(monkeypatch):
    r = _reasons(monkeypatch, {}, require_volatility_data=True)
    assert any(x.startswith("volatility_data_missing") for x in r), r


def test_missing_volatility_data_passes_when_not_required(monkeypatch):
    """The mirror, and today's shipped default."""
    r = _reasons(monkeypatch, {}, require_volatility_data=False)
    assert not any(x.startswith("volatility_data_missing") for x in r), r


def test_present_volatility_data_passes_even_when_required(monkeypatch):
    """Without this, 'reject everything' would pass the first test."""
    ctx = {"NVDA": {"atr_pct": 3.0, "expected_move_pct": 5.0,
                    "market_cap_usd": 2_000_000_000_000}}
    r = _reasons(monkeypatch, ctx, require_volatility_data=True)
    assert not any(x.startswith("volatility_data_missing") for x in r), r


def test_missing_market_cap_is_rejected_when_required(monkeypatch):
    ctx = {"NVDA": {"atr_pct": 3.0, "expected_move_pct": 5.0}}   # no market cap
    r = _reasons(monkeypatch, ctx, require_market_cap_data=True)
    assert any(x.startswith("market_cap_unverifiable") for x in r), r


def test_a_real_sub_billion_name_is_still_rejected_either_way(monkeypatch):
    """The original hard floor must be untouched by the new toggle."""
    ctx = {"NVDA": {"atr_pct": 3.0, "expected_move_pct": 5.0,
                    "market_cap_usd": 500_000_000}}
    for required in (True, False):
        r = _reasons(monkeypatch, ctx, require_market_cap_data=required)
        assert any(x.startswith("below_min_market_cap") for x in r), (required, r)


def test_both_toggles_ship_off_so_this_is_a_config_flip_not_a_surprise():
    cfg = validator.load_config()
    br = cfg.get("book_risk") or {}
    assert br.get("require_volatility_data") is False
    assert br.get("require_market_cap_data") is False
