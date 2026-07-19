"""Demand-driver themes — primary economic bet, not GICS sector.

DELL and HPE can live in different sector sleeves yet share one driver
(hyperscaler_server_capex). Theme concentration uses this map so the book
cannot stack the same factor under different sector labels.

Pure helpers + universe/ai_exposure files. Fail-soft. No network.

CLI: python -m tools.demand_drivers
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Coarse primary demand drivers (theme/correlation risk, not GICS).
KNOWN_DRIVERS = (
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
    "software_platforms",
    "mega_tech_platforms",
    "cybersecurity",
    "fintech",
    "consumer_platforms",
    "robotics_industrial",
    "financials",
    "energy",
    "materials",
    "healthcare",
    "consumer_industrial",
    "broad_market",
    "ai_supplier_other",
    "ai_beneficiary",
    "ai_at_risk",
    "other",
)

# universe.json sector key -> demand_driver
_SECTOR_TO_DRIVER: dict[str, str] = {
    "gpu_accelerators": "ai_compute_gpu",
    "semi_manufacturing": "semi_foundry_analog",
    "semi_equipment": "semi_equipment",
    "networking": "networking",
    "servers_hardware": "hyperscaler_server_capex",
    "data_center_reits": "datacenter_reit",
    "power_generation": "datacenter_power",
    "power_equipment": "datacenter_power",
    "design_ip": "semi_design_ip",
    "hyperscaler_enablers": "hyperscaler_cloud_infra",
    "construction_industrial": "datacenter_construction",
    "mega_tech": "mega_tech_platforms",
    "software_ai": "software_platforms",
    "cybersecurity": "cybersecurity",
    "consumer_tech_platforms": "consumer_platforms",
    "fintech_leaders": "fintech",
    "robotics_automation": "robotics_industrial",
    "broad_market_leaders": "broad_market",
    "financials_brokers_insurers": "financials",
    "energy_power": "energy",
    "gold_materials": "materials",
    "healthcare": "healthcare",
    "consumer_industrial_leaders": "consumer_industrial",
}

# Explicit ticker overrides (memory vs foundry, storage vs servers, etc.)
_TICKER_OVERRIDE: dict[str, str] = {
    "MU": "semi_memory",
    "WDC": "semi_memory",
    "STX": "semi_memory",
    "SNDK": "semi_memory",
    "NVDA": "ai_compute_gpu",
    "AMD": "ai_compute_gpu",
    "AVGO": "ai_compute_gpu",
    "MRVL": "ai_compute_gpu",
    "DELL": "hyperscaler_server_capex",
    "HPE": "hyperscaler_server_capex",
    "SMCI": "hyperscaler_server_capex",
    "CLS": "hyperscaler_server_capex",
}


def load_driver_overrides() -> dict[str, str]:
    """Optional data/demand_drivers.json -> {TICKER: driver}.

    Expected shape: {"version": 1, "drivers": {"DELL": "hyperscaler_server_capex", ...}}
    or a bare {TICKER: driver} map. Missing/invalid file -> {}.
    """
    path = ROOT / "data" / "demand_drivers.json"
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text())
        raw = blob.get("drivers") if isinstance(blob, dict) else blob
        if not isinstance(raw, dict):
            return {}
        return {
            str(k).upper(): str(v).strip().lower()
            for k, v in raw.items()
            if k and v
        }
    except Exception:
        return {}


def _ai_exposure_map() -> dict[str, str]:
    path = ROOT / "data" / "ai_exposure.json"
    try:
        labels = json.loads(path.read_text()).get("labels") or {}
        return {
            str(t).upper(): str((v or {}).get("exposure") or "")
            for t, v in labels.items()
        }
    except Exception:
        return {}


def _universe_sector_map() -> dict[str, str]:
    """{TICKER: sector_key} from data/universe.json. Fail-soft."""
    try:
        sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
        out: dict[str, str] = {}
        for sector, tickers in sectors.items():
            for t in tickers:
                out[str(t).upper()] = sector
        return out
    except Exception:
        return {}


# Resolution sources strong enough to CONTRADICT a proposal's self-declared driver.
# The ai_exposure fallback and the "other" default are deliberately excluded: they
# are guesses about a name we do not really classify, and a guess must never
# override a human/LLM judgement — only a real mapping may.
AUTHORITATIVE_SOURCES = ("file_override", "ticker_override", "sector_map")


def resolve_driver(
    ticker: str,
    sector: str | None = None,
    ai_exposure: str | None = None,
) -> tuple[str, str]:
    """(demand_driver, source) for one ticker.

    Source is one of: file_override, ticker_override, sector_map, ai_exposure,
    unresolved. Callers that need to ENFORCE the canonical answer must check the
    source against AUTHORITATIVE_SOURCES first — see driver_mismatch().
    """
    t = str(ticker or "").upper()
    if not t:
        return "other", "unresolved"
    overrides = load_driver_overrides()
    if t in overrides:
        return overrides[t], "file_override"
    if t in _TICKER_OVERRIDE:
        return _TICKER_OVERRIDE[t], "ticker_override"
    sec = sector or _universe_sector_map().get(t)
    if sec and sec in _SECTOR_TO_DRIVER:
        return _SECTOR_TO_DRIVER[sec], "sector_map"
    exp = ai_exposure or _ai_exposure_map().get(t)
    if exp == "ai_supplier":
        return "ai_supplier_other", "ai_exposure"
    if exp == "ai_beneficiary":
        return "ai_beneficiary", "ai_exposure"
    if exp == "ai_at_risk":
        return "ai_at_risk", "ai_exposure"
    return "other", "unresolved"


def driver_for_ticker(
    ticker: str,
    sector: str | None = None,
    ai_exposure: str | None = None,
) -> str:
    """Resolve demand_driver for one ticker.

    Priority: file overrides > code ticker overrides > sector alias map >
    weak AI-exposure fallback > other.
    """
    return resolve_driver(ticker, sector=sector, ai_exposure=ai_exposure)[0]


def driver_mismatch(
    ticker: str,
    stated: str,
    sector: str | None = None,
    ai_exposure: str | None = None,
) -> str | None:
    """Reason string when `stated` contradicts this ticker's canonical driver.

    THE HOLE THIS CLOSES. validator._check_demand_driver_field only ever verified
    that the stated label was a MEMBER of KNOWN_DRIVERS. That fix was written after
    an exploit was reproduced (validator.py docstring): a book at the theme cap
    rejected a same-theme BUY under `hyperscaler_server_capex` and approved the
    identical BUY under `hyperscaler_capex_supercycle`. Blocking novel labels closed
    the NOVEL-label path and left the ADJACENT-CANONICAL path wide open — DELL is
    mapped to hyperscaler_server_capex, but `ai_supplier_other`, `software_platforms`
    and `broad_market` are all equally legal KNOWN_DRIVERS. Declaring the second
    server OEM under any of them reset both theme caps to zero.

    DELL+HPE — one economic bet entered twice — is this book's entire realized loss
    history. The 2% theme risk cap exists because of it, and until now a one-word
    relabel defeated that cap.

    Fail-open by design when the canonical answer is a GUESS (ai_exposure fallback
    or unresolved): rejecting there would block every genuinely new name the
    universe_candidates path is meant to admit. Only a real mapping may contradict.
    """
    s = str(stated or "").strip().lower()
    if not s:
        return None
    canonical, source = resolve_driver(ticker, sector=sector, ai_exposure=ai_exposure)
    if source not in AUTHORITATIVE_SOURCES:
        return None
    if s == canonical:
        return None
    return (f"demand_driver_mismatch:{str(ticker).upper()} is canonically "
            f"'{canonical}' (source: {source}), proposed as '{s}' — a same-theme bet "
            f"relabelled under an adjacent driver escapes both theme caps; use the "
            f"canonical driver, or correct the map in data/demand_drivers.json")


def build_driver_map(
    universe_sectors: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """{TICKER: demand_driver} for the full universe (or provided sectors map)."""
    if universe_sectors is None:
        try:
            universe_sectors = json.loads(
                (ROOT / "data" / "universe.json").read_text()
            )["sectors"]
        except Exception:
            universe_sectors = {}
    ai = _ai_exposure_map()
    out: dict[str, str] = {}
    for sector, tickers in (universe_sectors or {}).items():
        for t in tickers:
            tu = str(t).upper()
            out[tu] = driver_for_ticker(tu, sector=sector, ai_exposure=ai.get(tu))
    # Ensure overrides apply even if not in universe file yet
    for t, d in {**_TICKER_OVERRIDE, **load_driver_overrides()}.items():
        out.setdefault(t, d)
    return out


def theme_exposure(
    positions: list[dict],
    driver_map: dict[str, str],
    equity: float | None = None,
) -> list[dict]:
    """Aggregate open positions by demand_driver.

    Returns list of:
      {demand_driver, market_value_usd, pct_of_equity, tickers}
    sorted by market_value_usd descending. pct_of_equity uses equity when
    provided, else sum of position market values.
    """
    buckets: dict[str, dict] = {}
    total = 0.0
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        t = str(pos.get("ticker") or "").upper()
        try:
            mv = float(pos.get("market_value_usd") or 0)
        except (TypeError, ValueError):
            continue
        if mv <= 0 or not t:
            continue
        d = position_driver(pos, driver_map)
        b = buckets.setdefault(
            d,
            {"demand_driver": d, "market_value_usd": 0.0, "tickers": []},
        )
        b["market_value_usd"] += mv
        if t not in b["tickers"]:
            b["tickers"].append(t)
        total += mv
    eq = float(equity) if isinstance(equity, (int, float)) and equity > 0 else total
    rows = []
    for b in buckets.values():
        rows.append({
            "demand_driver": b["demand_driver"],
            "market_value_usd": round(b["market_value_usd"], 2),
            "pct_of_equity": round(b["market_value_usd"] / eq, 4) if eq else 0.0,
            "tickers": b["tickers"],
        })
    rows.sort(key=lambda r: r["market_value_usd"], reverse=True)
    return rows


def position_driver(pos: dict, driver_map: dict[str, str]) -> str:
    """The demand_driver a HELD position is budgeted under.

    Canonical mapping wins over the lot's own stamp whenever the mapping is
    authoritative. The stamp is written at fill time from the PROPOSAL
    (runlib/brain_io.py), so trusting it first meant one mislabelled entry kept
    poisoning every later theme calculation for as long as the position lived —
    the persistence half of the relabelling exploit. The stamp still wins for names
    the map does not really classify, where it is the only information we have.
    """
    t = str(pos.get("ticker") or "").upper()
    stamped = str(pos.get("demand_driver") or "").strip().lower()
    if t:
        canonical, source = resolve_driver(t)
        if source in AUTHORITATIVE_SOURCES:
            return canonical
    return stamped or str(driver_map.get(t) or "other").lower()


def theme_concentration_breach(
    positions: list[dict],
    proposed_ticker: str,
    proposed_size_usd: float,
    equity: float,
    driver_map: dict[str, str],
    max_pct: float,
) -> str | None:
    """If adding proposed_size to proposed_ticker's driver breaches max_pct of equity.

    Returns a rejection reason string, or None when within cap / inputs unusable
    (fail-open on bad equity/size/cap).
    """
    if not isinstance(equity, (int, float)) or equity <= 0:
        return None
    if not isinstance(proposed_size_usd, (int, float)) or proposed_size_usd <= 0:
        return None
    if not isinstance(max_pct, (int, float)) or max_pct <= 0:
        return None
    t = str(proposed_ticker or "").upper()
    driver = str(driver_map.get(t) or "other").lower()
    same = 0.0
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        pd = position_driver(pos, driver_map)
        if pd == driver:
            try:
                same += float(pos.get("market_value_usd") or 0)
            except (TypeError, ValueError):
                continue
    if same + proposed_size_usd > equity * max_pct + 1e-6:
        return (
            f"theme_concentration_exceeded:{driver} "
            f"{same + proposed_size_usd:.0f} > {max_pct:.0%} of {equity:.0f}"
        )
    return None


if __name__ == "__main__":
    m = build_driver_map()
    c = Counter(m.values())
    print(json.dumps({
        "tickers": len(m),
        "by_driver": dict(c.most_common()),
        "sample": {
            k: m[k]
            for k in ("DELL", "HPE", "NVDA", "MU", "ANET", "VST", "EQIX", "MSFT")
            if k in m
        },
    }, indent=2))
