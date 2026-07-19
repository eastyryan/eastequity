"""Offline tests for tools.ownership_flow (pure synthesis, no network).

    python3 tests/test_ownership_flow.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ownership_flow import (  # noqa: E402
    build_ticker_card,
    classify_institutional,
    classify_price_action,
    classify_retail_proxy,
    find_scan_row,
    get_ownership_flow,
    synthesize_alignment,
    tape_sponsorship_lean,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_find_scan_row():
    print("find_scan_row:")
    scan = {
        "top_setups": [{"ticker": "AAA", "last_close": 1}],
        "contrarian_setups": [{"ticker": "BBB", "last_close": 2}],
    }
    check("top lane", find_scan_row(scan, "aaa").get("last_close") == 1)
    check("contrarian lane", find_scan_row(scan, "BBB").get("last_close") == 2)
    check("missing -> empty", find_scan_row(scan, "ZZZ") == {})
    check("none scan", find_scan_row(None, "AAA") == {})


def test_tape_sponsorship():
    print("tape sponsorship:")
    t = tape_sponsorship_lean({"read": "pocket_pivot", "pocket_pivot": True, "cmf_20": 0.1})
    check("pocket pivot constructive", t["lean"] == "constructive_sponsorship")
    t = tape_sponsorship_lean({"read": "absorption_at_highs_distribution"})
    check("distribution lean", t["lean"] == "distribution_or_weak_sponsorship")
    t = tape_sponsorship_lean({"read": "neutral", "cmf_20": -0.2})
    check("neutral lean", t["lean"] == "neutral_or_unknown")
    check("cmf negative flag", t["cmf_positive"] is False)


def test_classify_institutional():
    print("institutional:")
    inst = classify_institutional(
        {"net_activity": "net_buying", "managers_added": 2, "managers_new": 1,
         "notable_increases": [{"manager": "Coatue", "action": "added"}]},
        {"held_pct_institutions": 72.5},
        {"read": "pocket_pivot", "pocket_pivot": True, "cmf_20": 0.15,
         "updown_vol_ratio_25d": 1.4},
    )
    check("accumulating stance", inst["stance"] == "accumulating", inst["stance"])
    check("13f net buying", inst["thirteen_f"]["net_activity"] == "net_buying")
    check("ownership pct kept", inst["ownership"]["held_pct_institutions"] == 72.5)
    check("notable fund named", inst["thirteen_f"]["notable_funds"][0]["manager"] == "Coatue")

    weak = classify_institutional(
        {"net_activity": "net_selling", "managers_trimmed": 2, "managers_exited": 1},
        None,
        {"read": "absorption_at_highs_distribution", "cmf_20": -0.2,
         "updown_vol_ratio_25d": 0.6},
    )
    check("distributing stance", weak["stance"] == "distributing", weak["stance"])

    sparse = classify_institutional(
        {"net_activity": "no_tracked_activity"},
        None,
        {"read": "neutral"},
    )
    check("sparse when quiet", sparse["stance"] == "sparse", sparse["stance"])


def test_classify_retail_proxy():
    print("retail proxy:")
    hot = classify_retail_proxy(
        {
            "status": "ok",
            "put_call_volume_ratio": 0.4,
            "unusual_strikes": [{"strike": 100, "type": "call"}],
            "swing_options_read": {
                "net_tilt": "lean_bullish",
                "bullish_tilts": [{"x": 1}, {"y": 2}],
                "bearish_tilts": [],
            },
        },
        {"rvol": 2.5, "read": "climactic_volume"},
    )
    check("elevated speculation", hot["label"] == "elevated_speculation", hot["label"])
    check("unsponsored heat", hot["unsponsored_volume_heat"] is True)
    check("caveat present", "Proxy only" in (hot.get("caveat") or ""))

    quiet = classify_retail_proxy(
        {"status": "ok", "put_call_volume_ratio": 0.9, "unusual_strikes": [],
         "swing_options_read": {"net_tilt": "mixed_or_neutral",
                               "bullish_tilts": [], "bearish_tilts": []}},
        {"rvol": 0.8, "read": "neutral"},
    )
    check("quiet label", quiet["label"] == "quiet", quiet["label"])


def test_price_action_and_alignment():
    print("price + alignment:")
    pa = classify_price_action({
        "last_close": 100,
        "pct_change_1d": 1.2,
        "momentum_1m_pct": 8.0,
        "momentum_3m_pct": 22.0,
        "rel_strength_1m_pct": 3.5,
        "above_50dma": True,
        "above_200dma": True,
        "pullback_from_20d_high_pct": -4.0,
        "pct_from_52w_high": -6.0,
    })
    check("pullback structure", pa["structure"] == "pullback_in_uptrend", pa["structure"])
    check("price rising", pa["lean"] == "rising", pa["lean"])

    # Fallbacks from closes only.
    closes = [100 + i * 0.5 for i in range(30)]  # steady grind up
    pa2 = classify_price_action({}, closes=closes, highs=closes)
    check("return_5d from bars", isinstance(pa2["return_5d_pct"], float))
    check("bars lean rising", pa2["lean"] == "rising", pa2["lean"])

    syn = synthesize_alignment(
        {"stance": "accumulating", "thirteen_f": {"net_activity": "net_buying"},
         "tape_sponsorship": {"lean": "constructive_sponsorship"}},
        {"label": "quiet"},
        {"lean": "rising", "structure": "advancing_uptrend"},
    )
    check("inst buy + rising", syn["alignment"] == "institutions_buying_price_rising")

    syn = synthesize_alignment(
        {"stance": "accumulating", "thirteen_f": {"net_activity": "net_buying"},
         "tape_sponsorship": {"lean": "constructive_sponsorship"}},
        {"label": "quiet"},
        {"lean": "falling", "structure": "base_or_correction_above_200"},
    )
    check("inst buy + weak price", syn["alignment"] == "institutions_buying_price_weak")

    syn = synthesize_alignment(
        {"stance": "sparse", "thirteen_f": {"net_activity": "no_tracked_activity"},
         "tape_sponsorship": {"lean": "neutral_or_unknown"}},
        {"label": "elevated_speculation"},
        {"lean": "rising", "structure": "extended_near_highs"},
    )
    check("retail hot inst quiet", syn["alignment"] == "retail_hot_institutions_quiet")

    syn = synthesize_alignment(
        {"stance": "distributing", "thirteen_f": {"net_activity": "net_selling"},
         "tape_sponsorship": {"lean": "distribution_or_weak_sponsorship"}},
        {"label": "quiet"},
        {"lean": "falling", "structure": "breakdown_or_downtrend"},
    )
    check("inst sell + weak", syn["alignment"] == "institutions_selling_price_weak")


def test_build_card_and_offline_get():
    print("build_card + offline get_ownership_flow:")
    card = build_ticker_card(
        "DELL",
        sm_row={"net_activity": "net_buying", "managers_added": 1,
                "notable_increases": [{"manager": "Tiger Global", "action": "added"}]},
        options_sig={"status": "ok", "put_call_volume_ratio": 0.9, "unusual_strikes": [],
                     "swing_options_read": {"net_tilt": "mixed_or_neutral",
                                           "bullish_tilts": [], "bearish_tilts": []}},
        scan_row={
            "ticker": "DELL",
            "last_close": 140,
            "momentum_1m_pct": 6.0,
            "above_50dma": True,
            "above_200dma": True,
            "pullback_from_20d_high_pct": -1.0,
            "pct_from_52w_high": -2.0,
            "volume_signal": {
                "read": "pocket_pivot",
                "pocket_pivot": True,
                "cmf_20": 0.12,
                "updown_vol_ratio_25d": 1.3,
                "rvol": 1.8,
            },
        },
        ownership_snap={"held_pct_institutions": 68.0, "source": "test"},
    )
    check("ticker upper", card["ticker"] == "DELL")
    check("has alignment", card["alignment"] == "institutions_buying_price_rising",
          card["alignment"])
    check("read for swing non-empty", bool(card.get("read_for_swing")))
    check("three legs present",
          all(k in card for k in ("institutional", "retail_proxy", "price_action")))

    # fetch_missing=False keeps this fully offline.
    out = get_ownership_flow(
        ["DELL", "ZZZ"],
        smart_money={"by_ticker": {
            "DELL": {"net_activity": "net_selling", "managers_trimmed": 2},
        }},
        options_signals={"tickers": {
            "DELL": {"status": "ok", "put_call_volume_ratio": 1.6,
                     "unusual_strikes": [{"s": 1}],
                     "swing_options_read": {
                         "net_tilt": "lean_bearish",
                         "bullish_tilts": [],
                         "bearish_tilts": [{"a": 1}, {"b": 2}],
                     }},
        }},
        scan={"top_setups": [{
            "ticker": "DELL",
            "last_close": 100,
            "momentum_1m_pct": -8.0,
            "above_50dma": False,
            "above_200dma": False,
            "volume_signal": {"read": "absorption_at_highs_distribution",
                             "cmf_20": -0.15, "updown_vol_ratio_25d": 0.5, "rvol": 1.1},
        }]},
        fetch_missing=False,
    )
    check("status ok", out["status"] == "ok")
    check("both tickers", set(out["by_ticker"]) == {"DELL", "ZZZ"})
    dell = out["by_ticker"]["DELL"]
    check("dell alignment inst sell weak",
          dell["alignment"] == "institutions_selling_price_weak",
          dell["alignment"])
    # ZZZ has almost no inputs -> sparse or mixed is fine
    z = out["by_ticker"]["ZZZ"]
    check("zzz has alignment key", "alignment" in z, str(z.keys()))


def main():
    test_find_scan_row()
    test_tape_sponsorship()
    test_classify_institutional()
    test_classify_retail_proxy()
    test_price_action_and_alignment()
    test_build_card_and_offline_get()
    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("All ownership_flow tests passed.")


if __name__ == "__main__":
    main()
