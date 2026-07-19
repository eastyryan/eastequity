"""Ownership flow composite — institutional vs retail proxies vs price action.

One free research card per focus ticker that the brain can read side-by-side:

  INSTITUTIONAL
    - elite-fund 13F QoQ activity (from smart_money_13f, ~45d lag)
    - held-by-institutions % / short interest when yfinance info is available
    - tape sponsorship via volume_analysis (pocket pivot, CMF, breakout vol, …)

  RETAIL PROXY (NOT true retail buy/sell prints)
    - options put/call volume tilt + unusual strikes (no aggressor side)
    - elevated volume without institutional sponsorship patterns

  PRICE ACTION
    - short/medium returns, relative strength (when scan provides it),
      structure (advancing / base / pullback / breakdown / extended)

Honest limits (also stamped into every payload):
  - Free data has no true retail buy/sell imbalance or dark-pool side.
  - Options volume does not identify the aggressor.
  - 13F is conviction/positioning, never timing.
  - Volume patterns are sponsorship proxies, not fund identity.

CLI: python -m tools.ownership_flow NVDA DELL
"""

from __future__ import annotations

import json
from typing import Any

from tools.volume_analysis import analyze_volume

NOTE = (
    "FREE COMPOSITE — not true retail prints. Institutional side mixes lagging "
    "13F elite-fund activity (~45d) with daily volume sponsorship proxies. "
    "Retail side is a PROXY from options heat + unsponsored volume; free chains "
    "have no aggressor side. Price action is the third leg. Use alignment as "
    "corroboration / risk context for a 3–90 day swing, never as a standalone thesis."
)

# Volume reads that usually imply real sponsorship (inst-like footprint on the tape).
_TAPE_BULLISH = frozenset({
    "pocket_pivot",
    "confirmed_breakout",
    "absorption_at_lows_accumulation",
    "no_supply_pullback",
    "selling_climax_reversal_watch",
})
_TAPE_BEARISH = frozenset({
    "absorption_at_highs_distribution",
    "buying_climax_watch",
    "unconfirmed_breakout_suspect",
    "climactic_advance",
})


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no network)
# ---------------------------------------------------------------------------

def find_scan_row(scan: dict | None, ticker: str) -> dict:
    """Locate a scanner row for ticker across the published lanes."""
    if not isinstance(scan, dict):
        return {}
    tu = ticker.upper()
    lanes = (
        scan.get("top_setups") or [],
        scan.get("contrarian_setups") or [],
        scan.get("deep_value_200w") or [],
        scan.get("supplier_pullbacks") or [],
    )
    for lane in lanes:
        for r in lane:
            if isinstance(r, dict) and (r.get("ticker") or "").upper() == tu:
                return r
    return {}


def tape_sponsorship_lean(volume_signal: dict | None) -> dict:
    """Map a volume_analysis read into institutional-tape language."""
    vs = volume_signal if isinstance(volume_signal, dict) else {}
    read = vs.get("read") or "insufficient_data"
    if read in _TAPE_BULLISH or vs.get("pocket_pivot") or vs.get("breakout_volume_confirmed") is True:
        lean = "constructive_sponsorship"
    elif read in _TAPE_BEARISH or vs.get("breakout_volume_confirmed") is False:
        lean = "distribution_or_weak_sponsorship"
    elif read in (None, "insufficient_data", "neutral"):
        lean = "neutral_or_unknown"
    else:
        # climactic_volume / churn / selling_climax etc. — context-dependent
        lean = "mixed_or_event"
    cmf = vs.get("cmf_20")
    updown = vs.get("updown_vol_ratio_25d")
    return {
        "volume_read": read,
        "lean": lean,
        "pocket_pivot": bool(vs.get("pocket_pivot")),
        "breakout_volume_confirmed": vs.get("breakout_volume_confirmed"),
        "cmf_20": cmf,
        "updown_vol_ratio_25d": updown,
        "obv_divergence": vs.get("obv_divergence"),
        "rvol": vs.get("rvol"),
        "cmf_positive": (cmf > 0) if isinstance(cmf, (int, float)) else None,
        "up_day_volume_dominates": (updown > 1.0) if isinstance(updown, (int, float)) else None,
    }


def classify_institutional(
    sm_row: dict | None,
    ownership_snap: dict | None = None,
    volume_signal: dict | None = None,
) -> dict:
    """Institutional block: 13F activity + ownership snapshot + tape sponsorship."""
    sm = sm_row if isinstance(sm_row, dict) else {}
    own = ownership_snap if isinstance(ownership_snap, dict) else {}
    tape = tape_sponsorship_lean(volume_signal)

    net = sm.get("net_activity")
    if net not in ("net_buying", "net_selling", "mixed", "no_tracked_activity", None):
        net = None

    # Soft institutional score for synthesis (-2 .. +2-ish), not shown as a grade.
    score = 0
    if net == "net_buying":
        score += 2
    elif net == "net_selling":
        score -= 2
    elif net == "mixed":
        score += 0

    if tape["lean"] == "constructive_sponsorship":
        score += 1
    elif tape["lean"] == "distribution_or_weak_sponsorship":
        score -= 1
    if tape.get("cmf_positive") is True:
        score += 1
    elif tape.get("cmf_positive") is False:
        score -= 1
    if tape.get("up_day_volume_dominates") is True:
        score += 1
    elif tape.get("up_day_volume_dominates") is False:
        score -= 1

    if score >= 2:
        stance = "accumulating"
    elif score <= -2:
        stance = "distributing"
    elif score == 0 and net in (None, "no_tracked_activity") and tape["lean"] == "neutral_or_unknown":
        stance = "sparse"
    else:
        stance = "mixed"

    notable_inc = sm.get("notable_increases") or []
    notable_dec = sm.get("notable_decreases") or []
    funds = []
    for m in notable_inc[:3]:
        if isinstance(m, dict) and m.get("manager"):
            funds.append({"manager": m["manager"], "action": m.get("action"), "side": "buy"})
    for m in notable_dec[:3]:
        if isinstance(m, dict) and m.get("manager"):
            funds.append({"manager": m["manager"], "action": m.get("action"), "side": "sell"})

    return {
        "stance": stance,
        "score_hint": score,
        "thirteen_f": {
            "net_activity": net or "unknown",
            "managers_new": sm.get("managers_new"),
            "managers_added": sm.get("managers_added"),
            "managers_trimmed": sm.get("managers_trimmed"),
            "managers_exited": sm.get("managers_exited"),
            "net_share_delta": sm.get("net_share_delta"),
            "net_value_delta_usd": sm.get("net_value_delta_usd"),
            "notable_funds": funds,
            "lag_note": "13F ~45 days stale — conviction only, never timing",
        },
        "ownership": {
            "held_pct_institutions": own.get("held_pct_institutions"),
            "held_pct_insiders": own.get("held_pct_insiders"),
            "short_interest": own.get("short_interest"),
            "source": own.get("source"),
        },
        "tape_sponsorship": tape,
    }


def classify_retail_proxy(options_sig: dict | None, volume_signal: dict | None = None) -> dict:
    """Retail-LEANING proxy from free options heat + unsponsored tape volume.

    Explicitly NOT true retail buy/sell. Labels stay cautious.
    """
    opt = options_sig if isinstance(options_sig, dict) else {}
    vs = volume_signal if isinstance(volume_signal, dict) else {}
    rvol = vs.get("rvol")
    read = vs.get("read") or "neutral"
    swing = opt.get("swing_options_read") if isinstance(opt.get("swing_options_read"), dict) else {}
    # Some payloads nest swing_options_read; others put fields on the signal itself.
    if not swing and opt.get("status") == "ok":
        try:
            from tools.options_signal import build_swing_options_read
            swing = build_swing_options_read(opt)
        except Exception:
            swing = {}

    pc_ratio = opt.get("put_call_volume_ratio")
    unusual = opt.get("unusual_strikes") or []
    has_unusual = bool(unusual) or bool((opt.get("unusual") or {}).get("has_unusual"))
    # Prefer precomputed classifier if present on signal.
    un_block = opt.get("unusual") if isinstance(opt.get("unusual"), dict) else {}
    if un_block.get("has_unusual"):
        has_unusual = True

    net_tilt = swing.get("net_tilt") or "unknown"
    n_bull = len(swing.get("bullish_tilts") or [])
    n_bear = len(swing.get("bearish_tilts") or [])

    # Unsponsored high volume = more retail/speculative heat than sponsorship.
    unsponsored_heat = bool(
        isinstance(rvol, (int, float)) and rvol >= 2.0
        and read not in _TAPE_BULLISH
        and not vs.get("pocket_pivot")
        and vs.get("breakout_volume_confirmed") is not True
    )

    heat = 0
    if has_unusual:
        heat += 1
    if isinstance(pc_ratio, (int, float)) and (pc_ratio <= 0.55 or pc_ratio >= 1.4):
        heat += 1
    if unsponsored_heat:
        heat += 1
    if n_bull + n_bear >= 2:
        heat += 1

    if heat >= 2 and (n_bull > n_bear or (isinstance(pc_ratio, (int, float)) and pc_ratio <= 0.55)):
        label = "elevated_speculation"
    elif heat >= 2 and (n_bear > n_bull or (isinstance(pc_ratio, (int, float)) and pc_ratio >= 1.4)):
        label = "elevated_hedging_or_fear"
    elif heat >= 2:
        label = "elevated_activity_mixed"
    elif heat == 1:
        label = "mild_activity"
    else:
        label = "quiet"

    return {
        "label": label,
        "heat_score": heat,
        "caveat": (
            "Proxy only — free options have no buy/sell side; elevated call volume "
            "can be covered-call writing. Not a retail flow feed."
        ),
        "options": {
            "status": opt.get("status"),
            "put_call_volume_ratio": pc_ratio,
            "put_call_oi_ratio": opt.get("put_call_oi_ratio"),
            "net_tilt": net_tilt,
            "has_unusual_strikes": has_unusual,
            "n_unusual_strikes": len(unusual) if isinstance(unusual, list) else None,
            "atm_iv_pct": opt.get("atm_iv_pct"),
            "expected_move_pct": opt.get("expected_move_pct"),
        },
        "unsponsored_volume_heat": unsponsored_heat,
        "rvol": rvol,
    }


def _ret(closes: list[float], sessions: int):
    if not closes or len(closes) <= sessions:
        return None
    a, b = closes[-(sessions + 1)], closes[-1]
    if not a:
        return None
    return round((b / a - 1) * 100, 2)


def classify_price_action(
    scan_row: dict | None = None,
    closes: list[float] | None = None,
    highs: list[float] | None = None,
) -> dict:
    """Price structure + returns. Prefers scan metrics; falls back to bar lists."""
    row = scan_row if isinstance(scan_row, dict) else {}
    closes = list(closes or [])
    highs = list(highs or [])

    last = row.get("last_close")
    if last is None and closes:
        last = closes[-1]

    ret_5d = row.get("pct_change_5d")
    if ret_5d is None and closes:
        ret_5d = _ret(closes, 5)
    # scanner uses pct_change_1d / momentum windows rather than 5d always
    ret_1d = row.get("pct_change_1d")
    ret_1m = row.get("momentum_1m_pct")
    ret_3m = row.get("momentum_3m_pct")
    if ret_1m is None and closes:
        ret_1m = _ret(closes, 21)
    if ret_3m is None and closes:
        ret_3m = _ret(closes, 63)
    if ret_5d is None and closes:
        ret_5d = _ret(closes, 5)

    rs_1m = row.get("rel_strength_1m_pct")
    rs_3m = row.get("rel_strength_3m_pct")
    above_50 = row.get("above_50dma")
    above_200 = row.get("above_200dma")
    pullback_20 = row.get("pullback_from_20d_high_pct")
    pct_52w = row.get("pct_from_52w_high")
    if pct_52w is None and closes and highs and len(highs) >= 20:
        hi = max(highs[-min(len(highs), 252):])
        if hi:
            pct_52w = round((closes[-1] / hi - 1) * 100, 1)

    # Structure label (simple, swing-relevant).
    structure = "unknown"
    if above_50 is True and above_200 is True:
        if isinstance(pullback_20, (int, float)) and pullback_20 <= -2:
            structure = "pullback_in_uptrend"
        elif isinstance(pct_52w, (int, float)) and pct_52w >= -3:
            structure = "extended_near_highs"
        else:
            structure = "advancing_uptrend"
    elif above_50 is True and above_200 is False:
        structure = "intermediate_up_long_term_mixed"
    elif above_50 is False and above_200 is True:
        structure = "base_or_correction_above_200"
    elif above_50 is False and above_200 is False:
        structure = "breakdown_or_downtrend"
    elif closes and len(closes) >= 20:
        # Fallback without DMAs: recent direction vs 20d range position.
        r20 = _ret(closes, 20) or 0
        hi20 = max(closes[-20:])
        lo20 = min(closes[-20:])
        pos = (closes[-1] - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
        if r20 >= 5 and pos >= 0.7:
            structure = "advancing_uptrend"
        elif r20 <= -5 and pos <= 0.3:
            structure = "breakdown_or_downtrend"
        elif -3 <= r20 <= 3:
            structure = "base_or_correction_above_200" if pos >= 0.4 else "base"
        else:
            structure = "mixed"

    # Directional lean for synthesis.
    lean = "flat"
    probes = [x for x in (ret_5d, ret_1m) if isinstance(x, (int, float))]
    if probes:
        avg = sum(probes) / len(probes)
        if avg >= 2:
            lean = "rising"
        elif avg <= -2:
            lean = "falling"

    return {
        "last_close": last,
        "return_1d_pct": ret_1d,
        "return_5d_pct": ret_5d,
        "return_1m_pct": ret_1m,
        "return_3m_pct": ret_3m,
        "rel_strength_1m_pct": rs_1m,
        "rel_strength_3m_pct": rs_3m,
        "above_50dma": above_50,
        "above_200dma": above_200,
        "pullback_from_20d_high_pct": pullback_20,
        "pct_from_52w_high": pct_52w,
        "structure": structure,
        "lean": lean,
        "rsi_14": row.get("rsi_14"),
        "macd_state": (row.get("macd") or {}).get("state") if isinstance(row.get("macd"), dict) else row.get("macd_state"),
    }


def synthesize_alignment(institutional: dict, retail: dict, price: dict) -> dict:
    """Combine the three legs into one agent-facing alignment label + read."""
    inst_stance = (institutional or {}).get("stance") or "sparse"
    retail_label = (retail or {}).get("label") or "quiet"
    price_lean = (price or {}).get("lean") or "flat"
    structure = (price or {}).get("structure") or "unknown"
    net_13f = ((institutional or {}).get("thirteen_f") or {}).get("net_activity")
    tape_lean = ((institutional or {}).get("tape_sponsorship") or {}).get("lean")

    retail_hot = retail_label in (
        "elevated_speculation", "elevated_hedging_or_fear", "elevated_activity_mixed",
    )
    inst_buy = inst_stance == "accumulating" or net_13f == "net_buying" or tape_lean == "constructive_sponsorship"
    inst_sell = inst_stance == "distributing" or net_13f == "net_selling" or tape_lean == "distribution_or_weak_sponsorship"

    # Priority table — first match wins.
    if inst_buy and price_lean == "rising":
        alignment = "institutions_buying_price_rising"
        read = (
            "Institutional sponsorship (13F and/or tape) aligns with rising price — "
            "trend has real participation; still demand your entry geometry."
        )
    elif inst_buy and price_lean == "falling":
        alignment = "institutions_buying_price_weak"
        read = (
            "Institutions accumulating while price is soft — classic absorption/"
            "accumulation candidate IF structure holds; do not buy a broken trend on 13F alone."
        )
    elif inst_sell and price_lean == "falling":
        alignment = "institutions_selling_price_weak"
        read = (
            "Distribution pressure with weak price — raise the bar hard for new longs; "
            "prefer defense on holdings unless thesis is explicitly contrarian."
        )
    elif inst_sell and price_lean == "rising":
        alignment = "institutions_selling_price_rising"
        read = (
            "Price rising into institutional selling / weak sponsorship — distribution "
            "risk or late melt-up; size smaller and demand clean invalidation."
        )
    elif retail_hot and not inst_buy and inst_stance in ("sparse", "mixed"):
        alignment = "retail_hot_institutions_quiet"
        read = (
            "Speculative/options heat without clear institutional sponsorship — "
            "FOMO risk is elevated; need a stronger fundamental or structure case."
        )
    elif retail_hot and inst_buy:
        alignment = "retail_hot_institutions_buying"
        read = (
            "Both speculative heat and institutional sponsorship present — can work, "
            "but crowded; watch for climax volume and late entry."
        )
    elif retail_hot and inst_sell:
        alignment = "retail_hot_institutions_selling"
        read = (
            "Retail/spec heat against institutional distribution — often a fade zone "
            "for new swing longs unless a discrete catalyst rewrites the tape."
        )
    elif inst_stance == "sparse" and retail_label == "quiet":
        alignment = "sparse_data"
        read = (
            "Little institutional or retail-proxy signal — lean on price structure, "
            "fundamentals, and your own setup criteria."
        )
    else:
        alignment = "mixed"
        read = (
            "Signals conflict or are only partially present — treat ownership flow as "
            "context, not a vote; require price structure + thesis agreement."
        )

    return {
        "alignment": alignment,
        "read_for_swing": read,
        "structure": structure,
        "components": {
            "institutional_stance": inst_stance,
            "retail_proxy_label": retail_label,
            "price_lean": price_lean,
            "thirteen_f_net": net_13f,
            "tape_lean": tape_lean,
        },
    }


def build_ticker_card(
    ticker: str,
    *,
    sm_row: dict | None = None,
    options_sig: dict | None = None,
    scan_row: dict | None = None,
    ownership_snap: dict | None = None,
    ohlcv: dict | None = None,
) -> dict:
    """Pure card builder. ohlcv = {opens,highs,lows,closes,volumes} optional."""
    tu = ticker.upper()
    row = dict(scan_row or {})
    vol_sig = row.get("volume_signal") if isinstance(row.get("volume_signal"), dict) else None

    closes = highs = lows = opens = volumes = None
    if isinstance(ohlcv, dict):
        opens = ohlcv.get("opens")
        highs = ohlcv.get("highs")
        lows = ohlcv.get("lows")
        closes = ohlcv.get("closes")
        volumes = ohlcv.get("volumes")
        if vol_sig is None and closes and volumes and opens and highs and lows:
            try:
                vol_sig = analyze_volume(opens, highs, lows, closes, volumes)
            except Exception:
                vol_sig = {"read": "insufficient_data"}

    # Prefer short interest from scan row when ownership_snap lacks it.
    own = dict(ownership_snap or {})
    if own.get("short_interest") is None and row.get("short_interest"):
        own["short_interest"] = row.get("short_interest")
        own.setdefault("source", "scan_row")

    institutional = classify_institutional(sm_row, own, vol_sig)
    retail = classify_retail_proxy(options_sig, vol_sig)
    price = classify_price_action(row, closes=closes, highs=highs)
    synthesis = synthesize_alignment(institutional, retail, price)

    return {
        "ticker": tu,
        "institutional": institutional,
        "retail_proxy": retail,
        "price_action": price,
        "alignment": synthesis["alignment"],
        "read_for_swing": synthesis["read_for_swing"],
        "synthesis": synthesis,
    }


# ---------------------------------------------------------------------------
# Network / orchestration
# ---------------------------------------------------------------------------

def _ownership_from_info(info: dict) -> dict:
    """Pull institution/insider % + short interest from a yfinance info dict."""
    if not isinstance(info, dict):
        return {}
    def pct(v):
        if isinstance(v, (int, float)):
            # yfinance usually returns fraction 0-1; sometimes already percent.
            return round(v * 100, 2) if v <= 1.5 else round(float(v), 2)
        return None
    from tools.universe_scanner import _short_interest_from_info
    si = _short_interest_from_info(info)
    out = {
        "held_pct_institutions": pct(info.get("heldPercentInstitutions")),
        "held_pct_insiders": pct(info.get("heldPercentInsiders")),
        "short_interest": si,
        "source": "yfinance_info",
    }
    if all(out[k] is None for k in ("held_pct_institutions", "held_pct_insiders")) and not si:
        return {}
    return out


def _fetch_ohlcv(ticker: str, period: str = "6mo") -> dict | None:
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
        if df is None or df.empty:
            return None
        df = df.dropna(subset=["Close"])
        return {
            "opens": [float(x) for x in df["Open"].tolist()],
            "highs": [float(x) for x in df["High"].tolist()],
            "lows": [float(x) for x in df["Low"].tolist()],
            "closes": [float(x) for x in df["Close"].tolist()],
            "volumes": [float(x) for x in df["Volume"].tolist()],
        }
    except Exception:
        return None


def _fetch_ownership_snap(ticker: str) -> dict:
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        return _ownership_from_info(info)
    except Exception:
        return {}


def get_ownership_flow(
    tickers: list[str] | None,
    *,
    smart_money: dict | None = None,
    options_signals: dict | None = None,
    scan: dict | None = None,
    fetch_missing: bool = True,
) -> dict:
    """Build the composite ownership-flow card for each ticker.

    Prefer reusing already-fetched smart_money / options_signals / scan rows
    from context_gather so we do not re-hit EDGAR or options chains.
    """
    tickers = [str(t).upper() for t in (tickers or []) if t]
    if not tickers:
        return {"status": "skipped", "note": NOTE, "by_ticker": {}, "tickers": []}

    sm_by = {}
    if isinstance(smart_money, dict):
        sm_by = smart_money.get("by_ticker") or {}
        if not isinstance(sm_by, dict):
            sm_by = {}

    opt_by = {}
    if isinstance(options_signals, dict):
        opt_by = options_signals.get("tickers") or {}
        if not isinstance(opt_by, dict):
            opt_by = {}

    by_ticker: dict[str, Any] = {}
    errors = 0
    for t in tickers:
        try:
            row = find_scan_row(scan, t)
            sm_row = sm_by.get(t) if isinstance(sm_by.get(t), dict) else None
            opt_sig = opt_by.get(t) if isinstance(opt_by.get(t), dict) else None

            ohlcv = None
            need_bars = not (isinstance(row.get("volume_signal"), dict)
                             and row.get("volume_signal").get("read")
                             and row.get("last_close") is not None
                             and row.get("momentum_1m_pct") is not None)
            if fetch_missing and need_bars:
                ohlcv = _fetch_ohlcv(t)

            own = {}
            if isinstance(row.get("short_interest"), dict):
                own["short_interest"] = row["short_interest"]
                own["source"] = "scan_row"
            # Institutional % only on enrich path sometimes — fetch if missing.
            if fetch_missing and own.get("held_pct_institutions") is None:
                fetched = _fetch_ownership_snap(t)
                if fetched:
                    # Merge: prefer non-null from fetch, keep short interest from either.
                    if own.get("short_interest") and not fetched.get("short_interest"):
                        fetched["short_interest"] = own["short_interest"]
                    own = fetched

            card = build_ticker_card(
                t,
                sm_row=sm_row,
                options_sig=opt_sig,
                scan_row=row,
                ownership_snap=own,
                ohlcv=ohlcv,
            )
            by_ticker[t] = card
        except Exception as e:
            errors += 1
            by_ticker[t] = {"ticker": t, "status": "error", "reason": str(e)[:150]}

    # STATUS CONTRACT (tools/freshness_audit.feed_status). The predicate this
    # replaces was `ok = sum(... if v.get("alignment") or v.get("status") !=
    # "error")` followed by `status = "ok" if ok else ("error" if errors else
    # "ok")` — which could NEVER return "degraded": one surviving card out of
    # twenty reported a fully healthy feed. Since every alignment read that
    # cannot be computed degrades to `sparse_data`, and `sparse_data` explicitly
    # instructs the brain to "ignore this card", a half-dead feed was
    # indistinguishable from a universe of genuinely ambiguous names.
    from tools.freshness_audit import stamp_feed

    ok = sum(1 for v in by_ticker.values() if v.get("status") != "error")
    # A card is only SUBSTANTIVE when it produced a usable alignment vote;
    # sparse_data/mixed are non-votes by their own documented meaning.
    voted = sum(1 for v in by_ticker.values()
                if v.get("alignment") not in (None, "sparse_data", "mixed"))
    failures = {t: str(v.get("reason"))[:120]
                for t, v in by_ticker.items() if v.get("status") == "error"}
    out = {
        "note": NOTE,
        "tickers": tickers,
        "by_ticker": by_ticker,
        "how_to_use": {
            "institutions_buying_price_rising": "Corroboration for momentum entries with real participation.",
            "institutions_buying_price_weak": "Look for absorption / base; wait for reclaim or light-volume test.",
            "institutions_selling_price_weak": "Defensive bias; new longs need an exceptional thesis.",
            "institutions_selling_price_rising": "Distribution risk — late-move caution.",
            "retail_hot_institutions_quiet": "FOMO / lottery risk without sponsorship.",
            "retail_hot_institutions_buying": "Crowded but sponsored — size and climax risk matter.",
            "retail_hot_institutions_selling": "Often a fade zone for new swing longs.",
            "sparse_data": "Ignore this card; use structure + fundamentals.",
            "mixed": "No clean vote — require agreement from other evidence.",
        },
    }
    return stamp_feed(out, len(tickers), ok, len(tickers) - ok, found=voted,
                      failures=dict(list(failures.items())[:10]),
                      cards_with_alignment_vote=voted)


if __name__ == "__main__":
    import sys
    ts = [a for a in sys.argv[1:] if not a.startswith("-")] or ["NVDA", "DELL"]
    # Standalone: pull 13F + options so the CLI is useful without the orchestrator.
    try:
        from tools.smart_money_13f import get_smart_money
        sm = get_smart_money(ts)
    except Exception as e:
        sm = {"status": "error", "reason": str(e), "by_ticker": {}}
    try:
        from tools.options_signal import get_options_signals
        opt = get_options_signals(ts)
    except Exception as e:
        opt = {"status": "error", "reason": str(e), "tickers": {}}
    print(json.dumps(get_ownership_flow(ts, smart_money=sm, options_signals=opt), indent=2))
