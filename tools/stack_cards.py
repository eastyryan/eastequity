"""Stack cards — short supply-chain differential for focus names.

Each card answers: which layer of the stack, who buys, what substitutes,
and one differential question (who eats margin / concentration risk).
File overrides in data/stack_cards.json win over driver/sector templates.

Pure + local files. No network.

CLI: python -m tools.stack_cards NVDA DELL MU
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# demand_driver -> template card fields
_DRIVER_TEMPLATES: dict[str, dict] = {
    "ai_compute_gpu": {
        "layer": "AI accelerators / high-end compute silicon",
        "typical_customers": ["hyperscalers", "neoclouds", "enterprise AI training/inference"],
        "peer_substitutes": ["other GPU vendors", "custom ASICs/TPUs", "FPGA niches"],
        "differential_question": "Who gains if GPU supply eases or customers shift to custom silicon?",
    },
    "semi_memory": {
        "layer": "HBM / DRAM / storage semiconductors",
        "typical_customers": ["GPU makers", "server OEMs", "hyperscalers"],
        "peer_substitutes": ["other memory majors", "SLC/QLC mix shifts"],
        "differential_question": "Is the cheap multiple cycle-peak supply or under-owned growth?",
    },
    "semi_equipment": {
        "layer": "WFE / process tools for advanced nodes",
        "typical_customers": ["foundries", "IDMs", "memory fabs"],
        "peer_substitutes": ["other WFE peers by process step"],
        "differential_question": "Does capex pause hit tools before or after silicon demand?",
    },
    "semi_foundry_analog": {
        "layer": "Foundry / analog / specialty process",
        "typical_customers": ["fabless designers", "auto/industrial OEMs"],
        "peer_substitutes": ["other foundries", "IDM capacity"],
        "differential_question": "Is share gain real or just cycle restocking?",
    },
    "semi_design_ip": {
        "layer": "Chip design IP / EDA",
        "typical_customers": ["fabless semis", "systems companies designing silicon"],
        "peer_substitutes": ["competing IP blocks", "in-house design"],
        "differential_question": "Does AI chip complexity expand the TAM faster than competition?",
    },
    "networking": {
        "layer": "Data-center networking / optics / interconnect",
        "typical_customers": ["hyperscalers", "enterprises building AI clusters"],
        "peer_substitutes": ["other switch/optics vendors", "in-house designs"],
        "differential_question": "Is growth port-speed migration or share shift among vendors?",
    },
    "hyperscaler_server_capex": {
        "layer": "AI servers / systems OEM",
        "typical_customers": ["hyperscalers", "enterprise AI buyers"],
        "peer_substitutes": ["other OEMs", "ODM direct", "cloud instances vs on-prem"],
        "differential_question": "Is demand GPU-box shipments or broader server refresh?",
    },
    "datacenter_power": {
        "layer": "Power generation / power equipment for data centers",
        "typical_customers": ["utilities", "hyperscalers", "colos", "industrials"],
        "peer_substitutes": ["other generators", "gas peaker vs nuclear narratives"],
        "differential_question": "Is the bottleneck generation, interconnection, or equipment lead times?",
    },
    "datacenter_reit": {
        "layer": "Data-center real estate / colocation",
        "typical_customers": ["hyperscalers", "enterprises", "cloud providers"],
        "peer_substitutes": ["other REITs", "self-build campuses"],
        "differential_question": "Is rent growth power-constrained or overbuilt in key markets?",
    },
    "datacenter_construction": {
        "layer": "Data-center construction / specialty electrical",
        "typical_customers": ["hyperscalers", "colos", "utilities projects"],
        "peer_substitutes": ["other E&C / electrical contractors"],
        "differential_question": "Is backlog multi-year or a one-cycle surge?",
    },
    "hyperscaler_cloud_infra": {
        "layer": "Cloud / GPU-cloud / infra platforms",
        "typical_customers": ["enterprises", "AI startups", "developers"],
        "peer_substitutes": ["Big-3 cloud", "neocloud peers"],
        "differential_question": "Is growth rental GPUs, software attach, or capacity scarcity?",
    },
    "software_platforms": {
        "layer": "Enterprise / AI software platforms",
        "typical_customers": ["enterprises", "developers"],
        "peer_substitutes": ["horizontal SaaS peers", "hyperscaler native tools"],
        "differential_question": "Is AI a real attach or a multiple re-rate story?",
    },
    "mega_tech_platforms": {
        "layer": "Mega-cap platforms (ads/cloud/devices)",
        "typical_customers": ["consumers", "advertisers", "enterprises"],
        "peer_substitutes": ["other mega platforms"],
        "differential_question": "Is the AI spend ROI showing in cloud growth or still cost?",
    },
    "cybersecurity": {
        "layer": "Cybersecurity software / platforms",
        "typical_customers": ["enterprises", "governments"],
        "peer_substitutes": ["platform suite vendors", "point products"],
        "differential_question": "Is growth new logo or expansion in existing estates?",
    },
    "fintech": {
        "layer": "Fintech / digital finance",
        "typical_customers": ["consumers", "SMBs", "merchants"],
        "peer_substitutes": ["banks", "other fintech rails"],
        "differential_question": "Is the story rates, volumes, or take-rate expansion?",
    },
}

# High-value per-ticker overrides (customers / substitutes more specific)
_TICKER_CARDS: dict[str, dict] = {
    "NVDA": {
        "layer": "AI GPUs + CUDA software stack",
        "typical_customers": ["Microsoft", "Meta", "Google", "Amazon", "neoclouds"],
        "peer_substitutes": ["AMD", "Google TPU", "Trainium/Inferentia", "custom ASICs"],
        "differential_question": "Does CUDA lock-in hold if custom silicon share rises at hyperscalers?",
        "customer_concentration": "high (few hyperscalers drive training spend)",
    },
    "AMD": {
        "layer": "AI GPUs + CPU/server silicon",
        "typical_customers": ["hyperscalers", "OEMs", "enterprises"],
        "peer_substitutes": ["NVDA", "custom ASICs", "Intel server"],
        "differential_question": "Is MI-series share real deployment or design-win headlines?",
        "customer_concentration": "high",
    },
    "DELL": {
        "layer": "AI servers / storage systems OEM",
        "typical_customers": ["hyperscalers", "enterprises", "governments"],
        "peer_substitutes": ["HPE", "SMCI", "ODM direct"],
        "differential_question": "Is AI server growth incremental profit or low-margin GPU pass-through?",
        "customer_concentration": "medium-high",
    },
    "HPE": {
        "layer": "AI servers / hybrid cloud systems",
        "typical_customers": ["enterprises", "hyperscalers", "telco"],
        "peer_substitutes": ["DELL", "SMCI", "Lenovo"],
        "differential_question": "Juniper/networking mix vs pure AI server cyclicality?",
        "customer_concentration": "medium",
    },
    "MU": {
        "layer": "DRAM / HBM memory",
        "typical_customers": ["GPU makers", "server OEMs", "hyperscalers", "handsets"],
        "peer_substitutes": ["Samsung", "SK Hynix"],
        "differential_question": "HBM scarcity vs DRAM cycle peak — which dominates the multiple?",
        "customer_concentration": "high into AI GPUs",
    },
    "ANET": {
        "layer": "Data-center ethernet switching",
        "typical_customers": ["hyperscalers", "cloud", "enterprises"],
        "peer_substitutes": ["Cisco", "Juniper", "Nvidia networking"],
        "differential_question": "Is 400/800G a multi-year upgrade cycle or a lump?",
        "customer_concentration": "high hyperscaler",
    },
    "ASML": {
        "layer": "EUV / lithography tools",
        "typical_customers": ["TSMC", "Samsung", "Intel"],
        "peer_substitutes": ["none at EUV frontier; DUV peers limited"],
        "differential_question": "Does China restriction / High-NA timing change the order book?",
        "customer_concentration": "very high (few fabs)",
    },
    "TSM": {
        "layer": "Leading-edge foundry",
        "typical_customers": ["NVDA", "Apple", "AMD", "Broadcom", "others"],
        "peer_substitutes": ["Samsung foundry", "Intel foundry (longer dated)"],
        "differential_question": "Is AI wafer demand durable through a consumer semi lull?",
        "customer_concentration": "high (top customers dominate advanced nodes)",
    },
    "VRT": {
        "layer": "Thermal / power management for data centers",
        "typical_customers": ["colos", "hyperscalers", "enterprises"],
        "peer_substitutes": ["other thermal/power equipment vendors"],
        "differential_question": "Is liquid cooling a multi-year attach or a one-time retrofit wave?",
        "customer_concentration": "medium",
    },
    "VST": {
        "layer": "Independent power generation (data-center adjacent)",
        "typical_customers": ["utilities offtakers", "retail power", "industrial load"],
        "peer_substitutes": ["other IPPs", "new generation projects"],
        "differential_question": "Is data-center load growth already in forward curves or still optionality?",
        "customer_concentration": "market/region specific",
    },
    "NBIS": {
        "layer": "AI cloud / GPU infrastructure (neocloud)",
        "what_they_do": (
            "Nebius Group operates large-scale GPU cloud and AI infrastructure: "
            "renting training/inference capacity and related platform services to "
            "companies that need AI compute without building their own clusters. "
            "It is a capacity/provider story in the AI supercycle — not a chip designer "
            "and not a classic SaaS compounder. Growth often shows up as soaring revenue "
            "with heavy capex and negative free cash flow while they build."
        ),
        "typical_customers": ["AI startups", "enterprises training/inference", "developers"],
        "peer_substitutes": ["CoreWeave-type neoclouds", "AWS/GCP/Azure GPU instances", "self-build clusters"],
        "differential_question": (
            "Is utilization and pricing power durable if hyperscalers cut GPU prices "
            "or push custom silicon (TPU/ASIC) into the same customers?"
        ),
        "customer_concentration": "high (few large AI capacity buyers)",
        "business_model_note": (
            "Capex-growth / scale-up: judge on revenue growth, backlog/utilization narrative, "
            "liquidity (cash + debt capacity), and competitive position — NOT on near-term FCF "
            "looking like a mature software firm. Still require a swing setup (structure, "
            "estimate direction, event path); supercycle is not a free pass."
        ),
    },
    "CRWV": {
        "layer": "AI cloud / GPU infrastructure (neocloud)",
        "what_they_do": (
            "GPU-cloud / AI infrastructure capacity provider — rents accelerated compute "
            "to AI builders. Same economic family as other neoclouds: growth vs capex intensity."
        ),
        "typical_customers": ["AI labs", "enterprises", "startups"],
        "peer_substitutes": ["Nebius", "hyperscaler GPU clouds", "other neoclouds"],
        "differential_question": "Utilization durability vs hyperscaler price/capacity competition?",
        "customer_concentration": "high",
        "business_model_note": "Capex-growth scale-up; FCF often negative while building.",
    },
}


def load_stack_overrides() -> dict[str, dict]:
    path = ROOT / "data" / "stack_cards.json"
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text())
        cards = blob.get("cards") if isinstance(blob, dict) else blob
        if not isinstance(cards, dict):
            return {}
        return {str(k).upper(): v for k, v in cards.items() if isinstance(v, dict)}
    except Exception:
        return {}


def _driver_for(ticker: str) -> str:
    try:
        from tools.demand_drivers import driver_for_ticker
        return driver_for_ticker(ticker)
    except Exception:
        return "other"


def _sector_for(ticker: str) -> str | None:
    try:
        sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
        tu = ticker.upper()
        for s, ts in sectors.items():
            if tu in {str(x).upper() for x in ts}:
                return s
    except Exception:
        pass
    return None


def stack_card_for(ticker: str, sector: str | None = None,
                   demand_driver: str | None = None) -> dict:
    """One stack card for a ticker. Always returns a dict; never raises."""
    t = str(ticker or "").upper()
    if not t:
        return {"ticker": "", "status": "empty"}
    driver = (demand_driver or _driver_for(t) or "other").strip().lower()
    sector = sector or _sector_for(t)
    file_ov = load_stack_overrides().get(t) or {}
    ticker_ov = _TICKER_CARDS.get(t) or {}
    tmpl = _DRIVER_TEMPLATES.get(driver) or {
        "layer": f"unspecified ({driver})",
        "typical_customers": ["see filings / 10-K customer discussion"],
        "peer_substitutes": ["same-sector peers"],
        "differential_question": "What is the mispriced mechanism vs consensus this cycle?",
    }
    # Merge priority: file override > ticker card > driver template
    base = {**tmpl, **ticker_ov, **file_ov}
    card = {
        "ticker": t,
        "sector": sector,
        "demand_driver": driver,
        "layer": base.get("layer"),
        "what_they_do": base.get("what_they_do"),
        "business_model_note": base.get("business_model_note"),
        "typical_customers": list(base.get("typical_customers") or [])[:6],
        "peer_substitutes": list(base.get("peer_substitutes") or [])[:6],
        "differential_question": base.get("differential_question"),
        "customer_concentration": base.get("customer_concentration"),
        "note": "Use this card to write variant_perception and to avoid stacking "
                "identical demand drivers. Prefer names with different layers. "
                "Read what_they_do / business_model_note before judging FCF.",
        "source": ("file_override" if file_ov else
                   "ticker_card" if ticker_ov else "driver_template"),
    }
    # Drop nulls
    return {k: v for k, v in card.items() if v is not None}


def build_stack_cards(tickers: list[str] | None) -> dict:
    """{TICKER: card} for a focus list."""
    out = {}
    for t in tickers or []:
        if not t:
            continue
        c = stack_card_for(str(t))
        out[c["ticker"]] = c
    return {
        "note": "Supply-chain differential cards for THIS run's focus set. "
                "Answer differential_question in thesis when proposing a BUY.",
        "by_ticker": out,
        "n": len(out),
    }


if __name__ == "__main__":
    import sys
    names = [a.upper() for a in sys.argv[1:]] or ["NVDA", "DELL", "MU", "ANET"]
    print(json.dumps(build_stack_cards(names), indent=2))
