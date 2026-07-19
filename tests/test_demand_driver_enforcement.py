"""demand_driver must be a KNOWN label, and unstamped lots must still be budgeted.

Two holes closed here, both in the theme-risk machinery that exists specifically to
stop DELL+HPE-style clustering — the pattern behind this book's entire realized loss
history (two closed trades, both losers, both hyperscaler_server_capex).

1. The field check validated FORMAT only, and the brain writes the field itself.
   Both caps match on the raw string, so one novel snake_case label escaped both.
2. The 2% risk cap read only position-stamped drivers, while the 35% notional cap
   already fell back to the universe map. Every lot predating the stamping — or one
   whose plan `backfill_position_plans` rewrote — was invisible to the risk cap.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validator  # noqa: E402
from tools.demand_drivers import KNOWN_DRIVERS, build_driver_map  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    return json.loads((ROOT / "autonomy_config.json").read_text())


def clustered_book(stamp_driver=True):
    """A book already at the theme cap on hyperscaler_server_capex."""
    pos = {"ticker": "DELL", "quantity": 8.0, "avg_cost": 450.0,
           "market_value_usd": 3600.0, "plan": {"stop_loss": 400.0}}
    if stamp_driver:
        pos["demand_driver"] = "hyperscaler_server_capex"
    return {"total_equity_usd": 10000.0, "cash_usd": 5000.0, "positions": [pos]}


def buy(driver, ticker="HPE"):
    return {
        "ticker": ticker, "action": "BUY", "instrument": "EQUITY",
        "position_size_usd": 200, "entry_price_max": 25.0, "stop_loss": 22.0,
        "target_price": 31.0, "holding_horizon_days": 30, "confidence": 0.72,
        "risk_reward_ratio": 2.0, "thesis": "x" * 80, "demand_driver": driver,
        "variant_perception": "y" * 120, "risk_map": "z" * 60,
        "macro_context": "m" * 40, "catalysts": ["a dated catalyst"],
        "scenarios": {"bull": {"price": 31, "prob": 0.3},
                      "base": {"price": 28, "prob": 0.5},
                      "bear": {"price": 22, "prob": 0.2}},
        "thesis_invalidators": {"invalidating_print": "p" * 90,
                                "invalidating_structure": "s" * 90,
                                "time_box": "t" * 90},
    }


def reasons_for(proposal, book, cfg):
    return validator.validate_proposals([proposal], book, cfg)[0].reasons


# --------------------------------------------------------------------------- #
# 1. Unknown labels
# --------------------------------------------------------------------------- #
def test_novel_label_no_longer_escapes_the_caps(cfg):
    """THE bypass: same trade, same book, only the label differs."""
    book = clustered_book()

    canonical = reasons_for(buy("hyperscaler_server_capex"), book, cfg)
    assert any("theme_risk_cap_exceeded" in r for r in canonical)

    novel = reasons_for(buy("hyperscaler_capex_supercycle"), book, cfg)
    assert any("demand_driver_unknown" in r for r in novel), (
        "a novel snake_case label must be rejected — it would group under its own "
        "key and see an empty theme bucket")


def test_every_known_driver_is_accepted(cfg):
    """Guard against over-correcting into rejecting legitimate drivers."""
    book = {"total_equity_usd": 10000.0, "cash_usd": 9000.0, "positions": []}
    for driver in KNOWN_DRIVERS:
        rs = reasons_for(buy(driver, ticker="NVDA"), book, cfg)
        assert not any("demand_driver_unknown" in r for r in rs), \
            f"canonical driver rejected: {driver}"


def test_well_formed_but_unknown_is_distinguished_from_malformed(cfg):
    book = {"total_equity_usd": 10000.0, "cash_usd": 9000.0, "positions": []}

    malformed = reasons_for(buy("Not A Driver"), book, cfg)
    assert any("demand_driver_invalid_format" in r for r in malformed)

    unknown = reasons_for(buy("perfectly_formed_nonsense"), book, cfg)
    assert any("demand_driver_unknown" in r for r in unknown)
    assert not any("invalid_format" in r for r in unknown)


def test_missing_driver_still_rejected(cfg):
    book = {"total_equity_usd": 10000.0, "cash_usd": 9000.0, "positions": []}
    p = buy("hyperscaler_server_capex")
    del p["demand_driver"]
    assert any("demand_driver_missing_or_weak" in r for r in reasons_for(p, book, cfg))


def test_membership_check_is_fail_soft_on_import_error(cfg, monkeypatch):
    """A pure-module import failure must not halt all trading — it degrades to the
    format check the system ran for its whole life until now."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "tools.demand_drivers":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    book = {"total_equity_usd": 10000.0, "cash_usd": 9000.0, "positions": []}
    rs = reasons_for(buy("some_unknown_label"), book, cfg)
    assert not any("demand_driver_unknown" in r for r in rs)


# --------------------------------------------------------------------------- #
# 2. Unstamped positions
# --------------------------------------------------------------------------- #
def test_unstamped_position_still_counts_toward_theme_risk(cfg):
    """A lot with no stamped driver was invisible to the 2% cap while the 35%
    notional cap already saw it. Same DELL exposure, driver only from the map."""
    driver = build_driver_map().get("DELL")
    assert driver == "hyperscaler_server_capex", "universe map changed; update fixture"

    rs = reasons_for(buy(driver), clustered_book(stamp_driver=False), cfg)
    assert any("theme_risk_cap_exceeded" in r for r in rs), (
        "the universe-derived fallback must budget positions whose stamped driver "
        "is missing")


def test_canonical_mapping_outranks_the_lot_stamp():
    """CONTRACT CHANGE (2026-07-19). The stamp used to win unconditionally.

    The stamp is written at fill time from the PROPOSAL, so trusting it first meant
    one mislabelled entry kept the 2% theme risk cap mis-bucketed for the entire life
    of the position — the persistence half of the relabelling exploit."""
    pos = {"ticker": "DELL", "demand_driver": "semi_memory"}
    assert validator._position_demand_driver(
        pos, {"DELL": "hyperscaler_server_capex"}) == "hyperscaler_server_capex"

    plan_stamped = {"ticker": "DELL", "plan": {"demand_driver": "networking"}}
    assert validator._position_demand_driver(
        plan_stamped, {"DELL": "x"}) == "hyperscaler_server_capex"


def test_stamp_still_wins_for_names_the_map_cannot_classify():
    """The mirror — without it, 'always return canonical' passes the test above."""
    unmapped = {"ticker": "ZZZZ", "demand_driver": "fintech"}
    assert validator._position_demand_driver(unmapped, {}) == "fintech"

    plan_only = {"ticker": "ZZZZ", "plan": {"demand_driver": "networking"}}
    assert validator._position_demand_driver(plan_only, {}) == "networking"

    bare = {"ticker": "ZZZZ"}
    assert validator._position_demand_driver(bare, {"ZZZZ": "networking"}) == "networking"
    assert validator._position_demand_driver(bare, None) is None
    assert validator._position_demand_driver(bare, {}) is None
