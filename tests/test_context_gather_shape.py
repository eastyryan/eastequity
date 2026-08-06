"""Shape tests for the 2026-08-05 gather_context decomposition. Pure, no network:

    EE_BROKER=simulation python3 tests/test_context_gather_shape.py

Context: runlib/context_gather.py's gather_context was a 777-line depth-switch
monolith. It is now a sequencer dispatching to three per-depth research
functions plus module-level _gather_/_build_ helpers. These tests pin the seams
that decomposition could silently break:

  * the research-dict contract: all three _gather_*_research functions must
    return the SAME key set, and gather_context must unpack exactly those keys
    (checked statically via AST — no network run needed);
  * the emitted bundle's top-level keys AND their order (the brain's ~2,000-line
    read window makes ordering load-bearing, per the market_breadth comment);
  * the pure transforms (_build_digest, _fundamentals_freshness,
    _market_checkin_payload, ...) against dict fixtures, including the
    monolith's exact edge behaviors (None-stripping, calendar-over-news
    next_earnings preference, [:15] check-in caps, skipped-block wording).

Every fixture is an in-memory dict; no state/ writes; the two _gather_late_tape
paths exercised are the ones that cannot fetch.
"""

import ast
import inspect
import os
import sys
from pathlib import Path

os.environ.setdefault("EE_BROKER", "simulation")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runlib.context_gather as cg  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# --- static seam checks (AST over the module source) -----------------------

_TREE = ast.parse(inspect.getsource(cg))
_FUNCS = {n.name: n for n in _TREE.body if isinstance(n, ast.FunctionDef)}

# The bundle's top-level keys IN ORDER, verbatim from the pre-decomposition
# monolith. If assembly ever drops, adds, or reorders a key, this fails.
EXPECTED_BUNDLE_KEYS = [
    "run_date", "as_of_et", "digest", "trading_mode", "run_depth",
    "run_depth_note", "allows_new_buys", "market_events", "momentum_health",
    "market_radar", "discovery_screen", "market_checkin", "hard_limits",
    "benchmark_close", "macro_regime", "market_breadth", "portfolio",
    "position_histories", "watchlist_trigger_alerts", "tape_focus_promotions",
    "earnings_deep_dive", "earnings_week", "reasoning_process",
    "earnings_lanes", "engagement", "stack_cards", "financial_checklists",
    "concept_memory", "universe_scan", "sec_filings",
    "fundamentals_freshness", "deep_fundamentals", "filing_texts",
    "filing_texts_note", "guidance_ledger", "fundamental_screen",
    "options_signals", "stop_engineering", "position_stop_cushion",
    "smart_money_13f", "news_and_catalysts", "market_news", "todays_8ks",
    "portfolio_risk", "factor_map", "portfolio_competition", "ownership_flow",
    "insider_activity", "partnerships", "track_record", "lessons_learned",
]


def _returned_dict_keysets(fn_node):
    """Key sets of every `return {...}` statement directly in this function."""
    out = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = []
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.append(k.value)
            out.append(keys)
    return out


def test_research_functions_share_one_contract():
    print("research-dict contract (static):")
    keysets = {}
    for name in ("_gather_light_research", "_gather_holdings_watchlist_research",
                 "_gather_full_research"):
        check(f"{name} exists", name in _FUNCS)
        rets = _returned_dict_keysets(_FUNCS[name])
        check(f"{name} has exactly one dict return", len(rets) == 1, str(len(rets)))
        keysets[name] = set(rets[0]) if rets else set()
    base = keysets["_gather_light_research"]
    for name, ks in keysets.items():
        check(f"{name} contract matches light's",
              ks == base, f"diff={ks ^ base}")

    gc = _FUNCS["gather_context"]
    unpacked = set()
    for node in ast.walk(gc):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "research"
                and isinstance(node.slice, ast.Constant)):
            unpacked.add(node.slice.value)
    check("gather_context unpacks exactly the contract keys",
          unpacked == base, f"diff={unpacked ^ base}")


def test_bundle_top_level_keys_and_order():
    print("bundle top-level keys and ORDER (static):")
    gc = _FUNCS["gather_context"]
    rets = [n for n in ast.walk(gc)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)]
    check("gather_context has exactly one dict return", len(rets) == 1,
          str(len(rets)))
    got = [k.value for k in rets[0].value.keys
           if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    check("all bundle keys are literal strings",
          len(got) == len(rets[0].value.keys))
    check("key list identical AND in the monolith's order",
          got == EXPECTED_BUNDLE_KEYS,
          f"first divergence: {next((i for i, (a, b) in enumerate(zip(got, EXPECTED_BUNDLE_KEYS)) if a != b), 'length')} "
          f"got={len(got)} expected={len(EXPECTED_BUNDLE_KEYS)}")


# --- pure transform fixtures ------------------------------------------------

def test_reporters_in_universe():
    print("_reporters_in_universe:")
    check("None trigger -> []",
          cg._reporters_in_universe(None, {"NVDA"}) == [])
    got = cg._reporters_in_universe(
        {"reporters": ["nvda", "NVDA", "amd", None, "", "zzzz"]},
        {"NVDA", "AMD"})
    check("uppercased, de-duped, universe-filtered, order kept",
          got == ["NVDA", "AMD"], str(got))


def test_held_and_watch():
    print("_held_and_watch:")
    portfolio = {"positions": [{"ticker": "DELL"}, {"ticker": "NET"}]}
    watchlist = [{"ticker": "ANET"}, {"ticker": ""}, {"note": "no ticker"},
                 {"ticker": "MPC"}]
    held, watch = cg._held_and_watch(portfolio, watchlist)
    check("held from positions", held == ["DELL", "NET"], str(held))
    check("watch skips falsy/missing tickers", watch == ["ANET", "MPC"], str(watch))
    held2, watch2 = cg._held_and_watch({}, [])
    check("empty inputs -> empty lists", held2 == [] and watch2 == [])


def test_company_names_from_filings():
    print("_company_names_from_filings:")
    check("non-dict -> {}", cg._company_names_from_filings(None) == {})
    got = cg._company_names_from_filings({
        "dell": {"company": "Dell Technologies Inc."},
        "X": "not a dict",
        "Y": {"status": "ok"},          # no company field
    })
    check("upper-cased tickers, company rows only",
          got == {"DELL": "Dell Technologies Inc."}, str(got))


def test_fundamentals_freshness():
    print("_fundamentals_freshness:")
    fresh, stale = cg._fundamentals_freshness(None)
    check("None filings -> ({}, [])", fresh == {} and stale == [])
    filings = {
        "ZZZ": {"status": "ok", "fundamentals_current_through": "2026-03-31",
                "latest_periodic_filing": {"period_end": "2026-06-30"},
                "stale_fundamentals_warning": True},
        "AAA": {"status": "ok", "fundamentals_current_through": "2026-06-30",
                "latest_periodic_filing": {"period_end": "2026-06-30"}},
        "ERR": {"status": "error"},
        "BAD": "not a dict",
    }
    fresh, stale = cg._fundamentals_freshness(filings)
    check("only status=ok rows kept", set(fresh) == {"ZZZ", "AAA"}, str(set(fresh)))
    check("row shape", fresh["ZZZ"] == {"current_through": "2026-03-31",
                                        "latest_filing_period": "2026-06-30",
                                        "stale": True}, str(fresh["ZZZ"]))
    check("fresh row stale=False", fresh["AAA"]["stale"] is False)
    check("stale_names sorted, stale-only", stale == ["ZZZ"], str(stale))


def _digest_fixture():
    filings = {
        "AAA": {
            "status": "ok",
            "stale_fundamentals_warning": True,
            "quarterly_fundamentals": {
                "revenue": [
                    {"period_end": "2025-06-30", "value_usd": 100.0},
                    {"period_end": "2025-09-30", "value_usd": 101.0},
                    {"period_end": "2025-12-31", "value_usd": 102.0},
                    {"period_end": "2026-03-31", "value_usd": 105.0},
                    {"period_end": "2026-06-30", "value_usd": 110.0},
                ],
                "net_income": [{"period_end": "2026-06-30", "value_usd": 9.0}],
            },
        },
        # BBB: no filing brief at all (light-run shape).
    }
    deep = {"AAA": {"quality_ratios": {"ratios": {
        "ebitda_ttm_usd": {"value": 50.0},          # dict-valued ratio
        "net_debt_to_ebitda": 1.5,                  # plain-valued ratio
    }}}}
    scan = {
        "top_setups": [{"ticker": "AAA", "last_close": 12.3, "rsi_14": 55,
                        "days_to_earnings": 21, "market_cap_usd": 5e9,
                        "macd": {"state": "above_zero"},
                        "volume_signal": {"read": "no_supply_pullback"},
                        "ai_exposure": "ai_supplier"}],
        "contrarian_setups": [{"ticker": "BBB", "last_close": 7.7}],
        "deep_value_200w": [], "supplier_pullbacks": [],
    }
    news = {"tickers": {"AAA": {"next_earnings": "2099-01-02"}}}
    insiders = {"tickers": {"AAA": {"summary": {
        "signal": "bullish_cluster_buying", "distinct_discretionary_buyers": 3}}}}
    smart = {"by_ticker": {"AAA": {"net_activity": "net_buying"}}}
    partners = {"tickers": {"AAA": {"material_agreement_8ks": [{}, {}]}}}
    return filings, deep, scan, news, insiders, smart, partners


def test_build_digest_shapes_and_none_stripping():
    print("_build_digest (fixtures, injected earnings_next):")
    filings, deep, scan, news, insiders, smart, partners = _digest_fixture()
    # Calendar knows BBB only; AAA must fall back to the news scrape.
    cal = {"BBB": "2099-05-05"}
    digest = cg._build_digest(["AAA", "BBB"], filings, deep, scan, news,
                              insiders, smart, partners,
                              earnings_next=lambda t: cal.get(t))
    a, b = digest["AAA"], digest["BBB"]
    check("both focus names present", set(digest) == {"AAA", "BBB"})
    check("latest_quarter_end cited", a["latest_quarter_end"] == "2026-06-30")
    check("revenue yoy = rev[-1]/rev[-5]-1", a["revenue_yoy_pct"] == 10.0,
          str(a.get("revenue_yoy_pct")))
    check("dict-valued ratio unwrapped", a["ttm_ebitda_usd"] == 50.0)
    check("plain-valued ratio passed through", a["net_debt_to_ebitda"] == 1.5)
    check("stale flag surfaces", a["fundamentals_stale"] is True)
    check("calendar miss falls back to news scrape",
          a["next_earnings"] == "2099-01-02", str(a.get("next_earnings")))
    check("calendar hit beats the news scrape",
          b["next_earnings"] == "2099-05-05", str(b.get("next_earnings")))
    check("scan row found across lanes (BBB in contrarian)",
          b["last_close"] == 7.7, str(b.get("last_close")))
    check("insider signal + buyer count echoed",
          a["insider_signal"] == "bullish_cluster_buying"
          and a["insider_distinct_buyers"] == 3)
    check("13f + deal count", a["13f_net_activity"] == "net_buying"
          and a["deal_8ks_recent"] == 2)
    check("None values stripped (BBB has no revenue key)",
          "revenue_usd" not in b, str(sorted(b)))
    check("False survives the None-strip (fundamentals_stale on BBB)",
          b.get("fundamentals_stale") is False)


def test_build_digest_default_earnings_next_binds_late():
    print("_build_digest default earnings_next (late-bound to _earnings_next):")
    filings, deep, scan, news, insiders, smart, partners = _digest_fixture()
    real = cg._earnings_next
    try:
        cg._earnings_next = lambda t: "2099-12-31"
        digest = cg._build_digest(["AAA"], filings, deep, scan, news,
                                  insiders, smart, partners)
        check("default path uses module _earnings_next (monkeypatch visible)",
              digest["AAA"]["next_earnings"] == "2099-12-31",
              str(digest["AAA"].get("next_earnings")))
    finally:
        cg._earnings_next = real


def test_market_checkin_payload():
    print("_market_checkin_payload:")
    scan = {"benchmark_trend": "supportive",
            "sector_relative_strength": {"semis": 1.0},
            "by_sector_leaders": {"semis": ["NVDA"]},
            "top_setups": [{"ticker": f"T{i}"} for i in range(20)]}
    discovery = {"status": "ok", "scanned": 950,
                 "top_candidates": [{"ticker": f"D{i}"} for i in range(20)]}
    got = cg._market_checkin_payload(scan, discovery, ["NVDA", "DELL"])
    check("top_setups capped at 15", len(got["top_setups"]) == 15)
    check("discovery candidates capped at 15",
          len(got["discovery"]["top_candidates"]) == 15)
    check("discovery status + scanned passed through",
          got["discovery"]["status"] == "ok" and got["discovery"]["scanned"] == 950)
    check("scan fields passed through",
          got["benchmark_trend"] == "supportive"
          and got["by_sector_leaders"] == {"semis": ["NVDA"]})
    check("focus echoed", got["focus_deep_researched"] == ["NVDA", "DELL"])
    check("as_of_et present", isinstance(got.get("as_of_et"), str) and got["as_of_et"])


def test_skipped_block_wording():
    print("_skipped_block (must match the monolith's placeholder verbatim):")
    check("exact dict", cg._skipped_block("light") ==
          {"status": "skipped", "note": "not run at depth=light"})


def test_late_tape_no_fetch_paths():
    print("_gather_late_tape (only the paths that cannot fetch):")
    mn, e8 = {"headlines": []}, {"filers": []}
    got_mn, got_8k = cg._gather_late_tape(mn, e8, {"filings_sweep": True}, "full")
    check("already-fetched blocks pass through untouched",
          got_mn is mn and got_8k is e8)
    got_mn, got_8k = cg._gather_late_tape(mn, None, {"filings_sweep": False}, "light")
    check("no sweep budget -> skipped_<depth>_run stub",
          got_8k == {"status": "skipped_light_run", "filers": []}, str(got_8k))


def test_market_breadth_block_fixture_paths():
    print("_market_breadth_block (fixture paths):")
    scan = {"breadth": {"pct_above_200dma": 41}}
    got = cg._market_breadth_block(scan, None)
    check("universe sample echoed", got["universe"] == {"pct_above_200dma": 41})
    check("no discovery -> broad_unavailable reason",
          "broad" not in got and "weekly (Sunday) depth only" in got["broad_unavailable"],
          str(got.get("broad_unavailable")))
    got2 = cg._market_breadth_block(scan, {"breadth": {"n": 950}})
    check("discovery breadth -> broad set", got2["broad"] == {"n": 950})
    check("sector_rotation_30d key always present", "sector_rotation_30d" in got)


if __name__ == "__main__":
    print("gather_context decomposition shape checks\n")
    for fn in (test_research_functions_share_one_contract,
               test_bundle_top_level_keys_and_order,
               test_reporters_in_universe,
               test_held_and_watch,
               test_company_names_from_filings,
               test_fundamentals_freshness,
               test_build_digest_shapes_and_none_stripping,
               test_build_digest_default_earnings_next_binds_late,
               test_market_checkin_payload,
               test_skipped_block_wording,
               test_late_tape_no_fetch_paths,
               test_market_breadth_block_fixture_paths):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All context_gather shape checks passed.")
