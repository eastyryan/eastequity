"""Offline tests for tools.demand_drivers (pure Python, no network).

    .venv/bin/python tests/test_demand_drivers.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.demand_drivers import (  # noqa: E402
    build_driver_map,
    driver_for_ticker,
    load_driver_overrides,
    theme_concentration_breach,
    theme_exposure,
)

FAILURES = []

# Minimal in-memory universe — no dependency on live data/universe.json layout.
FAKE_SECTORS = {
    "servers_hardware": ["DELL", "HPE", "SMCI"],
    "gpu_accelerators": ["NVDA", "AMD"],
    "semi_manufacturing": ["MU", "TSM"],
    "semi_equipment": ["ASML", "AMAT"],
    "networking": ["ANET", "CRDO"],
    "power_generation": ["VST", "CEG"],
    "power_equipment": ["ETN", "VRT"],
    "data_center_reits": ["EQIX", "DLR"],
    "software_ai": ["MSFT", "NOW"],
    "cybersecurity": ["CRWD", "PANW"],
    "fintech_leaders": ["HOOD", "V"],
    "broad_market_leaders": ["JPM", "COST"],
}


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_sector_aliases_and_overrides():
    print("sector aliases + ticker overrides:")
    check(
        "servers_hardware -> hyperscaler_server_capex",
        driver_for_ticker("DELL", sector="servers_hardware")
        == "hyperscaler_server_capex",
    )
    check(
        "gpu_accelerators -> ai_compute_gpu",
        driver_for_ticker("NVDA", sector="gpu_accelerators") == "ai_compute_gpu",
    )
    check(
        "MU override -> semi_memory (not foundry)",
        driver_for_ticker("MU", sector="semi_manufacturing") == "semi_memory",
    )
    check(
        "TSM stays semi_foundry_analog",
        driver_for_ticker("TSM", sector="semi_manufacturing")
        == "semi_foundry_analog",
    )
    check(
        "power_generation + power_equipment both datacenter_power",
        driver_for_ticker("VST", sector="power_generation") == "datacenter_power"
        and driver_for_ticker("ETN", sector="power_equipment") == "datacenter_power",
    )
    check(
        "unknown sector + no AI label -> other",
        driver_for_ticker("ZZZZ", sector="not_a_real_sector") == "other",
    )
    check(
        "unknown sector + ai_supplier -> ai_supplier_other",
        driver_for_ticker("ZZZZ", sector=None, ai_exposure="ai_supplier")
        == "ai_supplier_other",
    )
    check("empty ticker -> other", driver_for_ticker("") == "other")


def test_dell_hpe_same_nvda_different():
    print("DELL/HPE same driver; NVDA different:")
    dmap = build_driver_map(FAKE_SECTORS)
    check("DELL mapped", dmap.get("DELL") == "hyperscaler_server_capex",
          str(dmap.get("DELL")))
    check("HPE mapped", dmap.get("HPE") == "hyperscaler_server_capex",
          str(dmap.get("HPE")))
    check("DELL and HPE share driver", dmap["DELL"] == dmap["HPE"])
    check("NVDA is ai_compute_gpu", dmap.get("NVDA") == "ai_compute_gpu",
          str(dmap.get("NVDA")))
    check("NVDA != DELL driver", dmap["NVDA"] != dmap["DELL"])
    check("SMCI same as DELL", dmap.get("SMCI") == dmap["DELL"])


def test_theme_exposure():
    print("theme_exposure aggregation:")
    dmap = build_driver_map(FAKE_SECTORS)
    positions = [
        {"ticker": "DELL", "market_value_usd": 2000},
        {"ticker": "HPE", "market_value_usd": 1500},
        {"ticker": "NVDA", "market_value_usd": 1000},
        {"ticker": "bad", "market_value_usd": 0},  # skip zero
        {"ticker": "JPM", "market_value_usd": 500},
    ]
    rows = theme_exposure(positions, dmap, equity=10_000)
    by = {r["demand_driver"]: r for r in rows}
    check("hyperscaler row present", "hyperscaler_server_capex" in by)
    check(
        "server MV sums DELL+HPE",
        by["hyperscaler_server_capex"]["market_value_usd"] == 3500.0,
        str(by.get("hyperscaler_server_capex")),
    )
    check(
        "server tickers listed",
        set(by["hyperscaler_server_capex"]["tickers"]) == {"DELL", "HPE"},
    )
    check(
        "server pct_of_equity 35%",
        by["hyperscaler_server_capex"]["pct_of_equity"] == 0.35,
        str(by["hyperscaler_server_capex"]["pct_of_equity"]),
    )
    check(
        "gpu separate bucket",
        by["ai_compute_gpu"]["market_value_usd"] == 1000.0
        and by["ai_compute_gpu"]["tickers"] == ["NVDA"],
    )
    check(
        "sorted by MV desc (servers first)",
        rows[0]["demand_driver"] == "hyperscaler_server_capex",
        str([r["demand_driver"] for r in rows]),
    )
    # CONTRACT CHANGE (2026-07-19): the canonical mapping now outranks the lot's
    # stamp. The stamp comes from the proposal at fill time, so honouring it first
    # let one mislabelled entry keep the theme caps mis-bucketed for the life of the
    # position. DELL is canonically hyperscaler_server_capex and cannot be relabelled.
    stamped = theme_exposure(
        [{"ticker": "DELL", "market_value_usd": 100, "demand_driver": "fintech"}],
        dmap,
        equity=1000,
    )
    check(
        "canonical mapping overrides a mislabelled lot stamp",
        stamped[0]["demand_driver"] == "hyperscaler_server_capex",
        str(stamped),
    )
    # The mirror: for a name the map cannot classify, the stamp is the only real
    # information there is and must still win.
    unmapped = theme_exposure(
        [{"ticker": "ZZZZ", "market_value_usd": 100, "demand_driver": "fintech"}],
        dmap,
        equity=1000,
    )
    check(
        "stamp still wins for an unmapped ticker",
        unmapped[0]["demand_driver"] == "fintech",
        str(unmapped),
    )


def test_theme_concentration_breach():
    print("theme_concentration_breach (servers already 35%):")
    dmap = build_driver_map(FAKE_SECTORS)
    equity = 10_000.0
    max_pct = 0.35
    # DELL + HPE already at the 35% theme cap
    positions = [
        {"ticker": "DELL", "market_value_usd": 2000},
        {"ticker": "HPE", "market_value_usd": 1500},
    ]
    breach = theme_concentration_breach(
        positions, "SMCI", 500.0, equity, dmap, max_pct
    )
    check("breach when adding another server name", breach is not None, str(breach))
    check(
        "reason names theme_concentration_exceeded + driver",
        breach is not None
        and breach.startswith("theme_concentration_exceeded:hyperscaler_server_capex"),
        str(breach),
    )
    # Same book but adding a different theme is fine
    ok = theme_concentration_breach(
        positions, "NVDA", 500.0, equity, dmap, max_pct
    )
    check("no breach for different driver (NVDA gpu)", ok is None, str(ok))
    # Under the cap: only 20% in servers
    light = [{"ticker": "DELL", "market_value_usd": 1000}]
    ok2 = theme_concentration_breach(
        light, "HPE", 500.0, equity, dmap, max_pct
    )
    check("under cap (10%+5% < 35%)", ok2 is None, str(ok2))
    # Fail-open on bad equity
    check(
        "fail-open bad equity",
        theme_concentration_breach(positions, "SMCI", 500, 0, dmap, max_pct) is None,
    )


def test_load_overrides_missing_file():
    print("load_driver_overrides fail-soft:")
    # No data/demand_drivers.json in repo by default — must not raise.
    ov = load_driver_overrides()
    check("returns dict", isinstance(ov, dict), str(type(ov)))


def test_build_from_live_universe_if_present():
    print("live universe smoke (file-backed, offline):")
    path = Path(__file__).resolve().parent.parent / "data" / "universe.json"
    if not path.exists():
        print("  [SKIP] no data/universe.json")
        return
    dmap = build_driver_map()
    check("DELL present", "DELL" in dmap)
    check("HPE same as DELL", dmap.get("DELL") == dmap.get("HPE") == "hyperscaler_server_capex")
    check("NVDA different", dmap.get("NVDA") == "ai_compute_gpu" and dmap["NVDA"] != dmap["DELL"])
    # counts sanity
    check("universe non-empty", len(dmap) > 50, str(len(dmap)))


if __name__ == "__main__":
    test_sector_aliases_and_overrides()
    test_dell_hpe_same_nvda_different()
    test_theme_exposure()
    test_theme_concentration_breach()
    test_load_overrides_missing_file()
    test_build_from_live_universe_if_present()
    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("All demand_drivers tests passed.")
