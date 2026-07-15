"""Financial-line checklists by business-model type.

Every company type has different *important* lines. A neocloud with negative FCF
is not scored like a bank or a SaaS compounder. This module maps demand_driver
(and optional stack business_model_note) → a short checklist of lines + questions
the brain should answer before proposing a BUY.

Pure + demand_drivers. No network.

CLI: python -m tools.financial_checklists NBIS MSFT JPM MU
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# model_type -> checklist
# Each item: {line, why, good_looks_like, red_flag}
CHECKLISTS: dict[str, dict] = {
    "capex_growth_scaleup": {
        "label": "Capex-growth / AI capacity scale-up (neocloud, infra buildout)",
        "weight": "Revenue growth, estimate *direction*, liquidity, utilization narrative — NOT near-term FCF vs SaaS peers.",
        "lines": [
            {"line": "revenue_growth_yoy_and_fwd",
             "why": "Scale-ups are judged on whether demand is real and accelerating.",
             "good_looks_like": "High / sustained revenue growth; fwd growth still elevated; revisions stable or up.",
             "red_flag": "Growth decelerating hard while the multiple still prices perfection; large negative EPS revisions."},
            {"line": "cash_and_liquidity",
             "why": "Negative FCF is normal only if the balance sheet can fund the build.",
             "good_looks_like": "Large cash, manageable net debt, access to capital, current ratio healthy.",
             "red_flag": "Cash burn path that exhausts runway without clear funding; covenant / refinance stress."},
            {"line": "capex_and_fcf_path",
             "why": "FCF will look 'bad' during build — ask if capex is productive capacity.",
             "good_looks_like": "Capex tied to capacity customers will rent; management frames path to utilization.",
             "red_flag": "Capex without demand visibility; FCF worsening while growth also stalls."},
            {"line": "competitive_position",
             "why": "Hyperscalers and custom silicon can reprice the entire model.",
             "good_looks_like": "Sticky customers, differentiated capacity, pricing not in free-fall.",
             "red_flag": "Price wars, TPU/ASIC displacement narrative with no rebuttal."},
            {"line": "estimate_direction",
             "why": "Street estimates are the near-term scoreboard for a 3–90d swing.",
             "good_looks_like": "EPS/revenue revisions flat to up over 30d.",
             "red_flag": "Sharp downward revisions into a rich multiple."},
        ],
    },
    "semi_cyclical": {
        "label": "Semiconductor / hardware cyclical (memory, foundry-sensitive, equipment)",
        "weight": "Cycle position and inventory > headline P/E. Low fwd multiples at peak earnings are often traps.",
        "lines": [
            {"line": "cycle_position",
             "why": "Earnings are mean-reverting; multiples look cheapest at the top.",
             "good_looks_like": "Early/mid cycle: rising estimates, improving inventory, demand visibility (e.g. HBM).",
             "red_flag": "Record margins/EPS with falling revisions — classic late cycle."},
            {"line": "inventory_and_supply",
             "why": "Inventory build is the tell for semis.",
             "good_looks_like": "Inventory stable or declining vs sales; supply discipline.",
             "red_flag": "Inventory_building flag / channel fill while prices weaken."},
            {"line": "fwd_pe_vs_growth",
             "why": "Cheap fwd P/E on peak EPS is not value.",
             "good_looks_like": "Multiple reasonable vs mid-cycle earnings power, not peak.",
             "red_flag": "Thesis is only 'cheapest in group' without cycle underwriting."},
            {"line": "customer_concentration",
             "why": "GPU/hyperscaler buyers dominate some nodes.",
             "good_looks_like": "Diversified end demand or multi-year capacity contracts.",
             "red_flag": "One customer can cancel the cycle."},
            {"line": "estimate_direction",
             "why": "Revisions lead the cycle more than price alone.",
             "good_looks_like": "Revisions up or stabilizing after a washout.",
             "red_flag": "Revisions still rolling over into a bounce."},
        ],
    },
    "software_saas": {
        "label": "Software / SaaS / platforms (subscription, high incremental margin)",
        "weight": "Growth + retention + FCF conversion + rule-of-40 style quality.",
        "lines": [
            {"line": "revenue_growth_and_nrr",
             "why": "SaaS value is durable growth and expansion.",
             "good_looks_like": "Solid growth; net retention >100% if disclosed; guide intact.",
             "red_flag": "Growth cliff, guide cut, or AI-at-risk seat compression."},
            {"line": "free_cash_flow_and_margins",
             "why": "Mature software should convert earnings to cash.",
             "good_looks_like": "Positive/rising FCF margins; gross margin stable/high.",
             "red_flag": "FCF negative without hyper growth; gross margin collapse."},
            {"line": "sbc_and_dilution",
             "why": "SBC can fake profitability.",
             "good_looks_like": "SBC as % revenue contained; buybacks offset dilution.",
             "red_flag": "Huge SBC with no FCF; endless dilution."},
            {"line": "rule_of_40_style",
             "why": "Growth + FCF margin is the quality screen.",
             "good_looks_like": "Growth% + FCF margin% competitive for the subsector.",
             "red_flag": "Neither growth nor FCF working."},
            {"line": "estimate_direction",
             "why": "Multiple compression follows estimate cuts.",
             "good_looks_like": "Revisions up or stable.",
             "red_flag": "Persistent downward revisions at a rich multiple."},
        ],
    },
    "mega_platform": {
        "label": "Mega-cap platforms (ads/cloud/devices)",
        "weight": "Cloud growth, ad cycle, capex ROI on AI spend, FCF still matters at scale.",
        "lines": [
            {"line": "segment_growth",
             "why": "Cloud vs ads vs devices drive the multiple.",
             "good_looks_like": "Cloud accelerating or ads reaccelerating with spend discipline.",
             "red_flag": "Cloud deceleration + AI capex spike with no ROI narrative."},
            {"line": "free_cash_flow",
             "why": "Scale platforms are cash machines if healthy.",
             "good_looks_like": "Large, growing FCF; buybacks funded by ops.",
             "red_flag": "FCF collapse from capex without growth offset."},
            {"line": "ai_capex_roi",
             "why": "Market questions whether AI spend earns returns.",
             "good_looks_like": "AI revenue or cloud growth tracks spend.",
             "red_flag": "Spend up, growth flat, multiple down."},
            {"line": "estimate_direction",
             "why": "Street sets the near-term bar.",
             "good_looks_like": "Stable/up revisions.",
             "red_flag": "Cuts into event risk."},
        ],
    },
    "financials": {
        "label": "Banks / brokers / insurers / diversified financials",
        "weight": "Rates, credit, capital, NII/NIM — not SaaS FCF heuristics.",
        "lines": [
            {"line": "net_interest_income_or_spread",
             "why": "Core earnings engine for banks.",
             "good_looks_like": "NII/NIM stable or improving; deposit beta controlled.",
             "red_flag": "NII collapsing with no fee offset."},
            {"line": "credit_quality",
             "why": "Credit kills banks in cycles.",
             "good_looks_like": "Stable NCOs, reserves adequate, no surprise charge-offs.",
             "red_flag": "Rapidly rising delinquencies / reserve builds."},
            {"line": "capital_and_liquidity",
             "why": "Regulatory capital constrains returns and survival.",
             "good_looks_like": "CET1 / capital ratios comfortable vs requirements.",
             "red_flag": "Capital shortfall risk, deposit flight narrative."},
            {"line": "valuation_vs_book_or_earnings",
             "why": "P/B and ROE matter more than P/S.",
             "good_looks_like": "Discount to history with stable ROE.",
             "red_flag": "Cheap for a reason (credit)."},
            {"line": "estimate_direction",
             "why": "Revisions embed rate and credit views.",
             "good_looks_like": "Revisions stabilizing.",
             "red_flag": "Street still cutting hard."},
        ],
    },
    "reit_datacenter": {
        "label": "Data-center / infrastructure REITs",
        "weight": "Occupancy, rent growth, power/interconnect constraints, AFFO, leverage.",
        "lines": [
            {"line": "occupancy_and_rent_growth",
             "why": "REIT cash flows from leased capacity.",
             "good_looks_like": "High occupancy, positive rent spreads, backlog of MWs.",
             "red_flag": "Vacancy rising; power delays freeze growth."},
            {"line": "affo_or_ffo_path",
             "why": "Cash available for dividends/growth.",
             "good_looks_like": "AFFO growing; payout sustainable.",
             "red_flag": "AFFO cut; dividend coverage stress."},
            {"line": "leverage_and_maturity_wall",
             "why": "Rate-sensitive capital structures.",
             "good_looks_like": "Manageable LTV, staggered maturities.",
             "red_flag": "Refi wall into higher rates."},
            {"line": "power_and_supply",
             "why": "Data-center growth is power-gated.",
             "good_looks_like": "Secured power for development pipeline.",
             "red_flag": "Projects stuck on interconnect."},
        ],
    },
    "energy_power": {
        "label": "Energy / power generation / equipment",
        "weight": "Commodity or load outlook, spark spreads, backlog, balance sheet.",
        "lines": [
            {"line": "commodity_or_load_outlook",
             "why": "Cash flows track power prices / utilization / data-center load.",
             "good_looks_like": "Supportive curves or contracted offtake.",
             "red_flag": "Collapsing prices with high fixed costs."},
            {"line": "backlog_and_orders",
             "why": "Equipment names live on orders.",
             "good_looks_like": "Backlog multi-year; book-to-bill >1.",
             "red_flag": "Order cliff."},
            {"line": "leverage_and_fcf",
             "why": "Cyclical cash flows + debt is dangerous.",
             "good_looks_like": "Deleveraging, positive FCF at mid-cycle.",
             "red_flag": "Peak leverage at peak prices."},
            {"line": "estimate_direction",
             "why": "Street embeds commodity views.",
             "good_looks_like": "Revisions stable/up.",
             "red_flag": "Cuts with no cycle bottom signal."},
        ],
    },
    "healthcare": {
        "label": "Healthcare (devices, services, large-cap biopharma)",
        "weight": "Growth durability, reimbursement, pipeline/litigation, margins, FCF.",
        "lines": [
            {"line": "organic_growth",
             "why": "Volume/price/mix drives devices and services.",
             "good_looks_like": "Steady organic growth; procedure volumes healthy.",
             "red_flag": "Growth stall + multiple still rich."},
            {"line": "margins_and_fcf",
             "why": "Quality healthcare compounds cash.",
             "good_looks_like": "Stable/expanding margins; solid FCF.",
             "red_flag": "Margin compression without growth offset."},
            {"line": "pipeline_or_reimbursement",
             "why": "Binary risk differs by subsector.",
             "good_looks_like": "Clear catalysts with manageable binary risk for swing horizon.",
             "red_flag": "Major patent cliff or adverse CMS decision inside the hold."},
            {"line": "estimate_direction",
             "why": "Revisions matter.",
             "good_looks_like": "Stable/up.",
             "red_flag": "Persistent cuts."},
        ],
    },
    "industrial_cyclical": {
        "label": "Industrials / consumer cyclical / materials",
        "weight": "Orders, backlog, margins, inventory, cycle — plus FCF at mid-cycle.",
        "lines": [
            {"line": "orders_and_backlog",
             "why": "Leading indicators for industrials.",
             "good_looks_like": "Book-to-bill healthy; backlog growing.",
             "red_flag": "Order cancels; backlog burn without replacement."},
            {"line": "margins",
             "why": "Operating leverage cuts both ways.",
             "good_looks_like": "Margins stable or expanding with volume.",
             "red_flag": "Peak margins + slowing volumes."},
            {"line": "free_cash_flow",
             "why": "Quality cyclicals still throw cash mid-cycle.",
             "good_looks_like": "Positive FCF, working capital controlled.",
             "red_flag": "Cash tied in inventory at the top."},
            {"line": "estimate_direction",
             "why": "Revisions track the cycle.",
             "good_looks_like": "Bottoming or rising.",
             "red_flag": "Still rolling over into a bounce."},
        ],
    },
    "mature_compounder": {
        "label": "Quality compounder / broad-market leader (default quality screen)",
        "weight": "FCF, ROIC-style quality, growth durability, balance sheet, valuation vs history.",
        "lines": [
            {"line": "free_cash_flow",
             "why": "Compounders buy back stock and fund growth from ops.",
             "good_looks_like": "Consistent positive FCF; FCF margin healthy.",
             "red_flag": "FCF collapse or chronic miss."},
            {"line": "revenue_and_earnings_growth",
             "why": "Need growth to support the multiple.",
             "good_looks_like": "Steady mid-to-high single digit or better with stability.",
             "red_flag": "Stagnation at a growth multiple."},
            {"line": "balance_sheet",
             "why": "Net debt / leverage must be sane.",
             "good_looks_like": "Net cash or modest net debt / EBITDA.",
             "red_flag": "Leverage spike without earnings power."},
            {"line": "margins",
             "why": "Moat shows up in margins.",
             "good_looks_like": "Stable/expanding.",
             "red_flag": "Structural margin decay."},
            {"line": "valuation_vs_history_and_growth",
             "why": "Quality is not always cheap.",
             "good_looks_like": "Multiple reasonable vs growth and history.",
             "red_flag": "Top-decile multiple with slowing growth."},
            {"line": "estimate_direction",
             "why": "Revisions drive near-term swings.",
             "good_looks_like": "Flat to up.",
             "red_flag": "Cuts into elevated expectations."},
        ],
    },
}

# demand_driver -> model_type
_DRIVER_TO_MODEL: dict[str, str] = {
    "hyperscaler_cloud_infra": "capex_growth_scaleup",
    "hyperscaler_server_capex": "capex_growth_scaleup",  # OEM buildout; still growth/capex heavy
    "ai_compute_gpu": "semi_cyclical",
    "semi_memory": "semi_cyclical",
    "semi_foundry_analog": "semi_cyclical",
    "semi_equipment": "semi_cyclical",
    "semi_design_ip": "software_saas",  # high-margin IP/EDA-like
    "networking": "semi_cyclical",
    "datacenter_power": "energy_power",
    "datacenter_reit": "reit_datacenter",
    "datacenter_construction": "industrial_cyclical",
    "software_platforms": "software_saas",
    "cybersecurity": "software_saas",
    "mega_tech_platforms": "mega_platform",
    "consumer_platforms": "mega_platform",
    "fintech": "financials",
    "financials": "financials",
    "energy": "energy_power",
    "materials": "industrial_cyclical",
    "healthcare": "healthcare",
    "consumer_industrial": "industrial_cyclical",
    "robotics_industrial": "industrial_cyclical",
    "broad_market": "mature_compounder",
    "ai_supplier_other": "semi_cyclical",
    "ai_beneficiary": "mature_compounder",
    "ai_at_risk": "software_saas",
    "other": "mature_compounder",
}


def model_type_for_driver(demand_driver: str | None) -> str:
    d = (demand_driver or "other").strip().lower()
    return _DRIVER_TO_MODEL.get(d, "mature_compounder")


def checklist_for_model(model_type: str) -> dict:
    mt = model_type if model_type in CHECKLISTS else "mature_compounder"
    base = CHECKLISTS[mt]
    return {
        "model_type": mt,
        "label": base["label"],
        "weight": base["weight"],
        "lines": list(base["lines"]),
    }


def checklist_for_ticker(ticker: str, demand_driver: str | None = None) -> dict:
    """Full checklist card for one ticker."""
    t = str(ticker or "").upper()
    driver = demand_driver
    if not driver:
        try:
            from tools.demand_drivers import driver_for_ticker
            driver = driver_for_ticker(t)
        except Exception:
            driver = "other"
    mt = model_type_for_driver(driver)
    card = checklist_for_model(mt)
    card["ticker"] = t
    card["demand_driver"] = driver
    card["note"] = (
        "Answer each line briefly before a BUY. Model-specific: do not force SaaS FCF "
        "rules on capex-growth names, or ignore FCF on mature compounders."
    )
    return card


def build_financial_checklists(tickers: list[str] | None) -> dict:
    """{by_ticker: {T: checklist}, models_used: [...]} for focus set."""
    by = {}
    models = set()
    for t in tickers or []:
        if not t:
            continue
        c = checklist_for_ticker(str(t))
        by[c["ticker"]] = c
        models.add(c["model_type"])
    return {
        "note": "Per-focus-name financial-line checklists by business model. "
                "Use the weight line: different models, different scoreboards.",
        "by_ticker": by,
        "models_present": sorted(models),
        "n": len(by),
    }


if __name__ == "__main__":
    import sys
    names = [a.upper() for a in sys.argv[1:]] or ["NBIS", "MSFT", "JPM", "MU", "EQIX"]
    print(json.dumps(build_financial_checklists(names), indent=2))
