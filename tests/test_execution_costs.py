"""Offline tests for realistic fill costing + stop gap-through in the simulated
broker (execution/simulated_broker.py) and the dividend attribution wired in by
corporate_actions. Pure Python, no network / yfinance / pandas — run with:

    python3 tests/test_execution_costs.py

The sim broker is JSON over a state file, so we point STATE_FILE at a temp file
and inject the execution_costs config by patching _execution_costs(). We assert
the exact cash / fees / fill_price / realized-P&L math for BUY, SELL, forced
stop-gap exits (with and without ATR), and dividend attribution (no double
count), plus the fail-soft fallback to today's behaviour when the config block
is missing.
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import simulated_broker as sb

_REAL_EXEC_COSTS = sb._execution_costs  # capture before any test patches it

FAILURES = []


def check(label, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def approx(a, b, tol=1e-4):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def fresh_state(cash, positions=None):
    """Write a temp portfolio state and point the broker at it."""
    import os
    fd, path = tempfile.mkstemp(suffix=".json", prefix="port_")
    os.close(fd)
    state = {
        "cash_usd": cash,
        "total_equity_usd": cash,
        "positions": positions or [],
        "pending_orders": {},
        "history": [],
    }
    Path(path).write_text(json.dumps(state, default=str))
    sb.STATE_FILE = Path(path)
    return Path(path)


def set_costs(**overrides):
    """Patch _execution_costs() to return a controlled cost dict."""
    costs = dict(sb._COST_DEFAULTS)
    costs.update(overrides)
    sb._execution_costs = lambda: costs
    return costs


# ---------------------------------------------------------------------------
def test_buy_slippage_and_commission():
    print("BUY: slippage + commission into cash and cost basis:")
    fresh_state(5000.0)
    set_costs(slippage_bps=10, commission_per_share_usd=0.005, commission_min_usd=1.0)
    order = sb.place_order({"ticker": "TEST", "action": "BUY", "quantity": 10,
                            "reference_price": 100.0, "proposal_id": "t"})
    fill = sb.readback(order["order_id"])
    # slip 10bps -> fill 100.1; commission max(1.0, 0.05)=1.0; cash out 1002.0
    check("fill_price = ref*(1+slip)", approx(fill["fill_price"], 100.1))
    check("commission = max(min, per_share*qty)", approx(fill["fees_usd"]["commission"], 1.0))
    check("total_cost = notional + commission", approx(fill["total_cost_usd"], 1002.0))
    check("fees_usd echoes slippage_bps", approx(fill["fees_usd"]["slippage_bps"], 10))
    state = sb._load()
    check("cash out = notional + commission", approx(state["cash_usd"], 5000.0 - 1002.0))
    pos = state["positions"][0]
    check("avg_cost includes buy commission", approx(pos["avg_cost"], 100.2))  # (1001+1)/10
    check("buy_commission recorded on position", approx(pos["buy_commission_usd"], 1.0))
    check("market_value = trade notional (no commission)", approx(pos["market_value_usd"], 1001.0))


def test_sell_regulatory_fees_and_pnl():
    print("SELL: SEC fee + FINRA TAF + commission netted from proceeds & P&L:")
    fresh_state(0.0, positions=[{
        "ticker": "TEST", "quantity": 10, "avg_cost": 100.2,
        "market_value_usd": 1000.0, "opened_at": "2026-01-01T00:00:00+00:00",
    }])
    set_costs(slippage_bps=10, commission_per_share_usd=0.005, commission_min_usd=1.0,
              sec_sell_fee_rate=0.0000278, finra_taf_per_share_usd=0.000166,
              finra_taf_max_usd=8.30)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 100.0, "proposal_id": "t"})
    fill = sb.readback(order["order_id"])
    # fill 99.9; gross 999.0; comm 1.0; sec 0.0000278*999=0.0278; taf 0.000166*10=0.0017
    check("fill_price = ref*(1-slip)", approx(fill["fill_price"], 99.9))
    check("commission", approx(fill["fees_usd"]["commission"], 1.0))
    check("sec_fee on sell notional", approx(fill["fees_usd"]["sec_fee"], 0.0278))
    check("taf per share (capped)", approx(fill["fees_usd"]["taf"], 0.0017))
    # proceeds = 999.0 - 1.0 - 0.0278 - 0.0017 = 997.9705 -> 997.97
    check("proceeds net of all sell fees", approx(fill["notional_usd"], 999.0))
    state = sb._load()
    check("cash in = net proceeds", approx(state["cash_usd"], 997.97))
    # realized = proceeds - qty*avg_cost = 997.97 - 1002.0 = -4.03 (nets buy+sell costs)
    check("realized_pnl nets ALL costs", approx(fill["realized_pnl_usd"], -4.03))
    check("no dividends -> total == price P&L", approx(fill["total_realized_pnl_usd"], -4.03))


def test_taf_cap_applies():
    print("SELL: FINRA TAF is capped:")
    fresh_state(0.0, positions=[{
        "ticker": "TEST", "quantity": 1_000_000, "avg_cost": 1.0,
        "market_value_usd": 1_000_000.0, "opened_at": "2026-01-01T00:00:00+00:00",
    }])
    set_costs(slippage_bps=0, finra_taf_per_share_usd=0.000166, finra_taf_max_usd=8.30)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 1.0, "proposal_id": "t"})
    fill = sb.readback(order["order_id"])
    # 0.000166 * 1e6 = 166 -> capped at 8.30
    check("taf capped at finra_taf_max_usd", approx(fill["fees_usd"]["taf"], 8.30))


def test_stop_gap_with_atr():
    print("FORCED STOP EXIT: fills BELOW the stop by fraction*ATR (gap-through):")
    fresh_state(0.0, positions=[{
        "ticker": "TEST", "quantity": 10, "avg_cost": 100.0,
        "market_value_usd": 940.0, "opened_at": "2026-01-01T00:00:00+00:00",
    }])
    set_costs(slippage_bps=10, model_stop_gaps=True, stop_gap_atr_fraction=0.5)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 94.0, "proposal_id": "t",
                            "forced_exit_reason": "stop_loss_breached",
                            "stop_loss": 95.0, "atr_pct": 8.0})
    fill = sb.readback(order["order_id"])
    # gapped = 95 - 0.5*(8/100)*94 = 95 - 3.76 = 91.24 ; *0.999 = 91.14876
    check("gap fill = (stop - frac*ATR*ref)*(1-slip)", approx(fill["fill_price"], 91.1488))
    check("gap_modeled flag set", fill.get("gap_modeled") is True)
    check("gap_usd = stop - fill", approx(fill["gap_usd"], 95.0 - 91.1488))
    # An ordinary sell at ref would fill at 94*0.999 = 93.906 -> gap-through is worse
    check("gap fill is BELOW an ordinary sell fill", fill["fill_price"] < 93.906)


def test_stop_gap_fallback_no_atr():
    print("FORCED STOP EXIT without ATR: degrades to 2x slippage penalty:")
    fresh_state(0.0, positions=[{
        "ticker": "TEST", "quantity": 10, "avg_cost": 100.0,
        "market_value_usd": 940.0, "opened_at": "2026-01-01T00:00:00+00:00",
    }])
    set_costs(slippage_bps=10, model_stop_gaps=True, stop_gap_atr_fraction=0.5)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 94.0, "proposal_id": "t",
                            "forced_exit_reason": "stop_loss_breached",
                            "stop_loss": 95.0})  # no atr_pct
    fill = sb.readback(order["order_id"])
    # 94 * (1 - 2*0.001) = 94*0.998 = 93.812
    check("fallback fill = ref*(1-2*slip)", approx(fill["fill_price"], 93.812))
    check("gap_modeled flag set on fallback", fill.get("gap_modeled") is True)
    check("fallback fill worse than plain sell", fill["fill_price"] < 93.906)


def test_no_gap_for_non_stop_exit():
    print("Non-stop forced exit (horizon) does NOT gap through:")
    fresh_state(0.0, positions=[{
        "ticker": "TEST", "quantity": 10, "avg_cost": 100.0,
        "market_value_usd": 940.0, "opened_at": "2026-01-01T00:00:00+00:00",
    }])
    set_costs(slippage_bps=10, model_stop_gaps=True, stop_gap_atr_fraction=0.5)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 94.0, "proposal_id": "t",
                            "forced_exit_reason": "horizon_expired",
                            "stop_loss": 95.0, "atr_pct": 8.0})
    fill = sb.readback(order["order_id"])
    check("horizon exit fills at ordinary ref*(1-slip)", approx(fill["fill_price"], 93.906))
    check("horizon exit is not gap-modeled", not fill.get("gap_modeled"))


def test_dividend_attribution_no_double_count():
    print("SELL: carried dividends attributed to trade WITHOUT double-counting cash:")
    fresh_state(1000.0, positions=[{
        "ticker": "TEST", "quantity": 10, "avg_cost": 100.2,
        "market_value_usd": 1000.0, "opened_at": "2026-01-01T00:00:00+00:00",
        "dividends_received_usd": 15.0,  # already credited to cash earlier
    }])
    set_costs(slippage_bps=10, commission_per_share_usd=0.005, commission_min_usd=1.0,
              sec_sell_fee_rate=0.0000278, finra_taf_per_share_usd=0.000166,
              finra_taf_max_usd=8.30)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 100.0, "proposal_id": "t"})
    fill = sb.readback(order["order_id"])
    # proceeds 997.97 as above; price P&L -4.03; total = -4.03 + 15.0 = 10.97
    check("dividends echoed on fill", approx(fill["dividends_received_usd"], 15.0))
    check("price-only realized unchanged by dividends", approx(fill["realized_pnl_usd"], -4.03))
    check("total_realized = price P&L + dividends", approx(fill["total_realized_pnl_usd"], 10.97))
    state = sb._load()
    # Cash rises by PROCEEDS ONLY (997.97) — dividends were already in cash, not re-added
    check("cash rises by proceeds only (no dividend re-credit)",
          approx(state["cash_usd"], 1000.0 + 997.97))


def test_vol_scaled_slippage_exceeds_flat_bps():
    print("VOL-SCALED SLIPPAGE: high-ATR name costs more each way than flat bps:")
    # BUY, entry gap OFF so we isolate slippage. atr 7% at 0.05 -> 0.35% each way.
    fresh_state(5000.0)
    set_costs(slippage_bps=10, slippage_atr_fraction=0.05, entry_gap_atr_fraction=0.0)
    order = sb.place_order({"ticker": "TEST", "action": "BUY", "quantity": 10,
                            "reference_price": 100.0, "proposal_id": "t", "atr_pct": 7.0})
    fill = sb.readback(order["order_id"])
    # eff_slip = max(0.001, 0.05*0.07)=0.0035 -> fill 100*(1.0035)=100.35
    check("BUY fill uses vol-scaled slip (100.35 > flat 100.10)", approx(fill["fill_price"], 100.35))
    check("fill exceeds flat-bps fill", fill["fill_price"] > 100.1)
    check("effective_slippage_pct reported (0.35 > flat 0.10)",
          approx(fill["fees_usd"]["effective_slippage_pct"], 0.35))
    check("effective_slippage_pct beats flat slippage_bps/100", fill["fees_usd"]["effective_slippage_pct"] > 0.1)
    # And symmetrically on an ordinary SELL: high ATR fills LOWER than flat 99.90.
    fresh_state(0.0, positions=[{
        "ticker": "TEST", "quantity": 10, "avg_cost": 100.0,
        "market_value_usd": 1000.0, "opened_at": "2026-01-01T00:00:00+00:00"}])
    set_costs(slippage_bps=10, slippage_atr_fraction=0.05)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 100.0, "proposal_id": "t", "atr_pct": 7.0})
    sell = sb.readback(order["order_id"])
    # eff_slip 0.0035 -> 100*(1-0.0035)=99.65 < flat 99.90
    check("SELL fill uses vol-scaled slip (99.65 < flat 99.90)", approx(sell["fill_price"], 99.65))
    check("SELL fill is below flat-bps fill", sell["fill_price"] < 99.9)
    check("SELL echoes effective_slippage_pct", approx(sell["fees_usd"]["effective_slippage_pct"], 0.35))


def test_entry_gap_pushes_buy_above_ref_plus_slip():
    print("ENTRY GAP: BUY fills ABOVE ref+slip by entry_gap_atr_fraction*ATR:")
    fresh_state(5000.0)
    set_costs(slippage_bps=10, slippage_atr_fraction=0.05, entry_gap_atr_fraction=0.1)
    order = sb.place_order({"ticker": "TEST", "action": "BUY", "quantity": 10,
                            "reference_price": 100.0, "proposal_id": "t", "atr_pct": 7.0})
    fill = sb.readback(order["order_id"])
    # eff_slip 0.0035; entry_gap 0.1*0.07=0.007 -> fill 100*(1+0.0035+0.007)=101.05
    ref_plus_slip = round(100.0 * (1 + 0.0035), 4)  # 100.35
    check("fill = ref*(1+slip+gap) = 101.05", approx(fill["fill_price"], 101.05))
    check("entry gap pushes fill ABOVE ref+slip", fill["fill_price"] > ref_plus_slip)
    check("entry_gap_usd = per-share gap = 0.7", approx(fill["entry_gap_usd"], 0.70))
    check("effective_slippage_pct still reported", approx(fill["fees_usd"]["effective_slippage_pct"], 0.35))
    # Cash/cost basis include the gap ONCE (no double charge): cost = 10*101.05 = 1010.50
    check("cash out folds gap in once (1010.50)", approx(fill["total_cost_usd"], 1010.50))
    state = sb._load()
    check("avg_cost reflects gapped fill (101.05)", approx(state["positions"][0]["avg_cost"], 101.05))


def test_atr_none_reproduces_old_behaviour():
    print("ATR None: vol-scaling + entry gap collapse to today's flat-bps fill:")
    fresh_state(5000.0)
    # Config HAS vol/gap fractions set, but the order carries no atr_pct.
    set_costs(slippage_bps=10, slippage_atr_fraction=0.05, entry_gap_atr_fraction=0.1)
    order = sb.place_order({"ticker": "TEST", "action": "BUY", "quantity": 10,
                            "reference_price": 100.0, "proposal_id": "t"})  # no atr_pct
    fill = sb.readback(order["order_id"])
    check("BUY fill = flat ref*(1+slip) = 100.10", approx(fill["fill_price"], 100.1))
    check("entry_gap_usd = 0 when atr absent", approx(fill["entry_gap_usd"], 0.0))
    check("effective_slippage_pct = flat 0.10", approx(fill["fees_usd"]["effective_slippage_pct"], 0.1))
    # Explicit atr_pct=None behaves the same as absent.
    fresh_state(5000.0)
    set_costs(slippage_bps=10, slippage_atr_fraction=0.05, entry_gap_atr_fraction=0.1)
    order = sb.place_order({"ticker": "TEST", "action": "BUY", "quantity": 10,
                            "reference_price": 100.0, "proposal_id": "t", "atr_pct": None})
    fill = sb.readback(order["order_id"])
    check("explicit atr_pct=None also flat 100.10", approx(fill["fill_price"], 100.1))


def test_vol_scaled_slippage_on_stop_gap_exit():
    print("STOP GAP-THROUGH also uses vol-scaled slippage (symmetric everywhere):")
    fresh_state(0.0, positions=[{
        "ticker": "TEST", "quantity": 10, "avg_cost": 100.0,
        "market_value_usd": 940.0, "opened_at": "2026-01-01T00:00:00+00:00"}])
    set_costs(slippage_bps=10, slippage_atr_fraction=0.05,
              model_stop_gaps=True, stop_gap_atr_fraction=0.5)
    order = sb.place_order({"ticker": "TEST", "action": "SELL_TO_CLOSE",
                            "reference_price": 94.0, "proposal_id": "t",
                            "forced_exit_reason": "stop_loss_breached",
                            "stop_loss": 95.0, "atr_pct": 8.0})
    fill = sb.readback(order["order_id"])
    # eff_slip = max(0.001, 0.05*0.08=0.004)=0.004; gapped 95-0.5*0.08*94=91.24
    # fill = 91.24*(1-0.004)=90.87504 -> 90.875 ; WORSE than flat-slip 91.1488
    check("gap fill uses vol-scaled slip (90.875)", approx(fill["fill_price"], 90.875))
    check("vol-scaled gap fill below flat-slip gap fill (91.1488)", fill["fill_price"] < 91.1488)
    check("gap_modeled still flagged", fill.get("gap_modeled") is True)
    check("effective_slippage_pct = 0.40 on the exit", approx(fill["fees_usd"]["effective_slippage_pct"], 0.4))


def test_config_fallback_when_block_missing():
    print("Fail-soft: missing execution_costs block -> 10bps slippage, no fees:")
    # Point ROOT at a temp dir whose config has NO execution_costs block and
    # exercise the REAL reader (set_costs patched _execution_costs in prior tests).
    d = Path(tempfile.mkdtemp())
    (d / "autonomy_config.json").write_text(json.dumps({"position_sizing": {}}))
    saved_root, saved_fn = sb.ROOT, sb._execution_costs
    try:
        sb.ROOT = d
        sb._execution_costs = _REAL_EXEC_COSTS
        costs = sb._execution_costs()
        check("slippage falls back to 10 bps", approx(costs["slippage_bps"], 10.0))
        check("commission defaults to 0", approx(costs["commission_per_share_usd"], 0.0))
        check("sec fee defaults to 0", approx(costs["sec_sell_fee_rate"], 0.0))
        check("stop gaps default OFF", costs["model_stop_gaps"] is False)
        check("slippage_atr_fraction defaults to 0 (vol-scaling off)",
              approx(costs["slippage_atr_fraction"], 0.0))
        check("entry_gap_atr_fraction defaults to 0 (no entry gap)",
              approx(costs["entry_gap_atr_fraction"], 0.0))
    finally:
        sb.ROOT, sb._execution_costs = saved_root, saved_fn


if __name__ == "__main__":
    for fn in (test_buy_slippage_and_commission, test_sell_regulatory_fees_and_pnl,
               test_taf_cap_applies, test_stop_gap_with_atr, test_stop_gap_fallback_no_atr,
               test_no_gap_for_non_stop_exit, test_dividend_attribution_no_double_count,
               test_vol_scaled_slippage_exceeds_flat_bps,
               test_entry_gap_pushes_buy_above_ref_plus_slip,
               test_atr_none_reproduces_old_behaviour,
               test_vol_scaled_slippage_on_stop_gap_exit,
               test_config_fallback_when_block_missing):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All execution-cost checks passed.")
