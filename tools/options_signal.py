"""Options-derived signals (read-only) — derivatives market for swing equities.

Free Yahoo chains give IV, volume, and open interest. We derive:

  Geometry
    - expected_move_pct (ATM straddle) — stop/target noise floor
    - atm_iv_pct — event / fear priced in

  Sentiment tilts (NOT trade signals; free data has no aggressor side)
    - put_call_volume_ratio + put_call_oi_ratio
    - iv_skew + skew_read
    - unusual_strikes (vol >> OI) with level context

  Swing learning layer (NEW)
    - swing_options_read: structured bullish_tilts / bearish_tilts / neutral_notes
      with explicit confidence limits for a 3–90 day long-only swing

We never trade options. Long-only equities only.

CLI: python -m tools.options_signal DELL HPE NBIS
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPTIONS_HIST_DIR = ROOT / "data" / "cache" / "options_history"
MAX_HIST_SAMPLES = 90  # calendar samples for IV percentile

AMBIGUITY_NOTE = (
    "Free chain data has NO aggressor side: high call volume is NOT proof of bullish "
    "flow (could be covered-call writing or call spreads). High put volume is NOT proof "
    "of crash bets (could be protective hedges by longs). Use expected_move_pct for "
    "stop/target geometry; use swing_options_read tilts as SECONDARY confirmation or "
    "risk flags alongside price/fundamentals — never as the sole thesis. "
    "You still never trade options."
)


# --- pure interpreters (unit-testable, no network) ---------------------------

def classify_pc_volume(pc_ratio) -> dict:
    """Put/call VOLUME ratio regimes for swing context."""
    if not isinstance(pc_ratio, (int, float)) or pc_ratio <= 0:
        return {"regime": "unknown", "tilt": "neutral",
                "note": "missing put/call volume ratio"}
    # Equity index-ish heuristics adapted to single names (looser).
    if pc_ratio >= 1.5:
        return {
            "regime": "put_heavy",
            "tilt": "bearish_or_hedging",
            "note": (f"P/C vol {pc_ratio:.2f}: elevated put volume — often protective "
                     "hedging by longs OR downside speculation; with rising price can be "
                     "bullish-compatible (collar/hedge). With falling price, risk of more "
                     "deleveraging."),
        }
    if pc_ratio >= 1.0:
        return {
            "regime": "put_lean",
            "tilt": "cautious",
            "note": f"P/C vol {pc_ratio:.2f}: mild put lean — caution, not panic.",
        }
    if pc_ratio <= 0.5:
        return {
            "regime": "call_heavy",
            "tilt": "bullish_or_call_overwriting",
            "note": (f"P/C vol {pc_ratio:.2f}: heavy call volume — speculative upside OR "
                     "covered-call writing into strength. Confirm with price structure; "
                     "do not buy only because calls are busy."),
        }
    if pc_ratio <= 0.8:
        return {
            "regime": "call_lean",
            "tilt": "constructive",
            "note": f"P/C vol {pc_ratio:.2f}: call lean — mild constructive tilt if price confirms.",
        }
    return {
        "regime": "balanced",
        "tilt": "neutral",
        "note": f"P/C vol {pc_ratio:.2f}: roughly balanced volume.",
    }


def classify_skew(skew_pct, skew_read: str | None = None) -> dict:
    """IV skew: OTM put IV vs OTM call IV (percentage points)."""
    if not isinstance(skew_pct, (int, float)):
        return {"regime": "unknown", "tilt": "neutral",
                "note": "skew unavailable"}
    # skew_pct already in percentage points from (put_iv - call_iv)*100
    if skew_pct >= 5:
        return {
            "regime": "steep_put_skew",
            "tilt": "bearish_hedging_demand",
            "note": (f"Put IV {skew_pct:.1f}pp over call IV: market pays up for downside "
                     "protection. Common before earnings or after a run. For a swing long, "
                     "size smaller or demand cleaner structure — fear is priced."),
        }
    if skew_pct >= 3:
        return {
            "regime": "put_skew",
            "tilt": "cautious",
            "note": f"Mild put skew (+{skew_pct:.1f}pp): modest demand for downside protection.",
        }
    if skew_pct <= -3:
        return {
            "regime": "call_skew",
            "tilt": "bullish_speculation",
            "note": (f"Call IV {-skew_pct:.1f}pp over put IV: speculative upside demand "
                     "(or short-put supply). Can mark FOMO; chase risk is real."),
        }
    return {
        "regime": "balanced_skew",
        "tilt": "neutral",
        "note": f"Skew near flat ({skew_pct:.1f}pp).",
    }


def classify_iv(atm_iv_pct, expected_move_pct) -> dict:
    """IV level for event / regime context."""
    if not isinstance(atm_iv_pct, (int, float)):
        return {"regime": "unknown", "note": "IV unavailable"}
    notes = []
    if atm_iv_pct >= 80:
        regime = "very_elevated"
        notes.append(
            f"ATM IV ~{atm_iv_pct:.0f}% is very elevated — event risk or crisis premium. "
            "Expected move may already price the next catalyst; buying equity into this "
            "is paying for volatility unless your edge is post-event drift.")
    elif atm_iv_pct >= 50:
        regime = "elevated"
        notes.append(f"ATM IV ~{atm_iv_pct:.0f}% elevated vs quiet large-caps — event or growth-name premium.")
    elif atm_iv_pct >= 30:
        regime = "moderate"
        notes.append(f"ATM IV ~{atm_iv_pct:.0f}% moderate.")
    else:
        regime = "subdued"
        notes.append(f"ATM IV ~{atm_iv_pct:.0f}% relatively calm — less event premium in options.")
    if isinstance(expected_move_pct, (int, float)):
        notes.append(
            f"Nearest-expiry expected move ±{expected_move_pct:.1f}% — stops inside this "
            "band are noise for that window.")
    return {"regime": regime, "note": " ".join(notes)}


def classify_unusual(unusual: list | None, spot: float | None) -> dict:
    """Map unusual vol>>OI strikes into swing language."""
    if not unusual:
        return {"has_unusual": False, "notes": [], "call_strikes": [], "put_strikes": []}
    call_strikes, put_strikes, notes = [], [], []
    for u in unusual:
        if not isinstance(u, dict):
            continue
        kind = str(u.get("type") or "").lower()
        strike = u.get("strike")
        pct = u.get("pct_from_spot")
        if kind == "call":
            call_strikes.append(u)
            if isinstance(pct, (int, float)) and pct > 15:
                notes.append(
                    f"Far OTM call interest ~{pct:.0f}% above spot "
                    f"(${strike}) — lottery / squeeze narrative, not a base case.")
            elif isinstance(pct, (int, float)) and 0 <= pct <= 10:
                notes.append(
                    f"Near ATM/slightly OTM call heat at ${strike} "
                    f"(+{pct:.0f}%) — upside magnet zone IF flow is buying (unknown).")
        elif kind == "put":
            put_strikes.append(u)
            if isinstance(pct, (int, float)) and pct < -15:
                notes.append(
                    f"Deep OTM put heat ~{pct:.0f}% below spot "
                    f"(${strike}) — tail hedge or crash narrative.")
            elif isinstance(pct, (int, float)) and -10 <= pct <= 0:
                notes.append(
                    f"Near ATM put heat at ${strike} ({pct:.0f}% from spot) — "
                    "support/hedge zone; longs often defend here with puts.")
    return {
        "has_unusual": True,
        "notes": notes[:6],
        "call_strikes": call_strikes,
        "put_strikes": put_strikes,
    }


def build_swing_options_read(sig: dict) -> dict:
    """Compose bullish/bearish/neutral tilts for a long-only swing process.

    Pure function over a single ticker signal dict from _signal_for.
    """
    if not isinstance(sig, dict) or sig.get("status") != "ok":
        return {
            "status": sig.get("status") if isinstance(sig, dict) else "missing",
            "direction_confidence": "none",
            "bullish_tilts": [],
            "bearish_tilts": [],
            "neutral_notes": ["options signal not available"],
            "for_stop_engineering": {},
            "how_to_use": AMBIGUITY_NOTE,
        }

    pc = classify_pc_volume(sig.get("put_call_volume_ratio"))
    sk = classify_skew(sig.get("iv_skew_pct"), sig.get("skew_read"))
    iv = classify_iv(sig.get("atm_iv_pct"), sig.get("expected_move_pct"))
    un = classify_unusual(sig.get("unusual_strikes"), sig.get("spot"))

    bullish, bearish, neutral = [], [], []

    # P/C
    if pc["tilt"] in ("constructive", "bullish_or_call_overwriting"):
        bullish.append({"source": "put_call_volume", **pc})
    elif pc["tilt"] in ("cautious", "bearish_or_hedging"):
        bearish.append({"source": "put_call_volume", **pc})
    else:
        neutral.append({"source": "put_call_volume", **pc})

    # Skew
    if sk["tilt"] == "bullish_speculation":
        bullish.append({"source": "iv_skew", **sk})
    elif sk["tilt"] in ("cautious", "bearish_hedging_demand"):
        bearish.append({"source": "iv_skew", **sk})
    else:
        neutral.append({"source": "iv_skew", **sk})

    # IV regime
    if iv["regime"] in ("very_elevated", "elevated"):
        bearish.append({
            "source": "iv_level",
            "tilt": "event_premium",
            "note": iv["note"] + " Elevated IV raises the cost of being early on a long.",
        })
    else:
        neutral.append({"source": "iv_level", "tilt": "neutral", "note": iv["note"]})

    # Unusual
    for n in un.get("notes") or []:
        low = n.lower()
        if "deep otm put" in low or "tail hedge" in low:
            bearish.append({"source": "unusual_strikes", "tilt": "tail_hedge", "note": n})
        elif "far otm call" in low or "lottery" in low:
            neutral.append({"source": "unusual_strikes", "tilt": "lottery_calls", "note": n})
        elif "near atm put" in low:
            bearish.append({"source": "unusual_strikes", "tilt": "near_support_hedge", "note": n})
        elif "call heat" in low:
            bullish.append({"source": "unusual_strikes", "tilt": "call_heat", "note": n})
        else:
            neutral.append({"source": "unusual_strikes", "tilt": "activity", "note": n})

    # Net tilt score (soft)
    score = len(bullish) - len(bearish)
    if score >= 2:
        net = "lean_bullish"
    elif score <= -2:
        net = "lean_bearish"
    elif score == 1:
        net = "slight_bullish"
    elif score == -1:
        net = "slight_bearish"
    else:
        net = "mixed_or_neutral"

    em = sig.get("expected_move_pct")
    return {
        "status": "ok",
        "net_tilt": net,
        "direction_confidence": "low",  # always low on free data without aggressor
        "direction_confidence_note": (
            "Always LOW on free chains: we cannot see if volume was buy or sell. "
            "Use tilts only with price structure + thesis; never alone."),
        "bullish_tilts": bullish,
        "bearish_tilts": bearish,
        "neutral_notes": neutral,
        "for_stop_engineering": {
            "expected_move_pct": em,
            "atm_iv_pct": sig.get("atm_iv_pct"),
            "expiry": sig.get("expiry"),
            "days_to_expiry": sig.get("days_to_expiry"),
            "rule": "Stop should sit outside max(ATR floor, ~0.5× expected_move). "
                    "Target ideally outside the same noise band for a true swing.",
        },
        "swing_playbook": {
            "if_lean_bullish": (
                "Only add weight if price is reclaiming structure (e.g. above 50-DMA / "
                "prior breakout) AND thesis/fundamentals agree. Call heat without reclaim "
                "is often late FOMO."),
            "if_lean_bearish": (
                "Raise the bar: smaller size, wider invalidation, or wait for post-event "
                "IV crush. Put skew + estimate cuts + insider selling = risk stack."),
            "if_mixed": (
                "Default to price + fundamentals. Options are not voting."),
            "into_earnings": (
                "Elevated IV + large expected_move → prefer trading AFTER the print "
                "(drift) unless the thesis is the binary itself."),
        },
        "how_to_use": AMBIGUITY_NOTE,
    }


# --- history cache (IV rank + OI day-over-day) --------------------------------

def _hist_path(ticker: str) -> Path:
    return OPTIONS_HIST_DIR / f"{str(ticker).upper()}.json"


def load_options_history(ticker: str) -> list[dict]:
    """List of daily samples [{date, atm_iv_pct, call_oi, put_oi, total_oi, ...}]."""
    p = _hist_path(ticker)
    if not p.exists():
        return []
    try:
        blob = json.loads(p.read_text())
        samples = blob.get("samples") if isinstance(blob, dict) else blob
        return list(samples) if isinstance(samples, list) else []
    except Exception:
        return []


def save_options_history(ticker: str, samples: list[dict]) -> None:
    OPTIONS_HIST_DIR.mkdir(parents=True, exist_ok=True)
    # Keep newest MAX_HIST_SAMPLES by date
    cleaned = [s for s in samples if isinstance(s, dict) and s.get("date")]
    cleaned.sort(key=lambda s: str(s.get("date")))
    cleaned = cleaned[-MAX_HIST_SAMPLES:]
    _hist_path(ticker).write_text(json.dumps({
        "ticker": str(ticker).upper(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "samples": cleaned,
    }, indent=2))


def iv_percentile(current_iv, history_ivs: list[float]) -> dict:
    """Percentile of current ATM IV vs stored history (0–100). Pure."""
    if not isinstance(current_iv, (int, float)) or current_iv <= 0:
        return {"status": "unavailable", "note": "no current IV"}
    hist = [float(x) for x in history_ivs
            if isinstance(x, (int, float)) and x > 0]
    if len(hist) < 5:
        return {
            "status": "warming_up",
            "n_samples": len(hist),
            "atm_iv_pct": round(current_iv, 1),
            "note": f"Need ≥5 daily IV samples for percentile (have {len(hist)}). "
                    "Builds automatically as runs accumulate.",
        }
    below = sum(1 for x in hist if x <= current_iv)
    pct = round(100.0 * below / len(hist), 1)
    if pct >= 80:
        regime = "high_vs_own_history"
        tilt = "caution_event_or_stress"
    elif pct <= 20:
        regime = "low_vs_own_history"
        tilt = "calm_relatively"
    else:
        regime = "mid_vs_own_history"
        tilt = "neutral"
    return {
        "status": "ok",
        "n_samples": len(hist),
        "atm_iv_pct": round(current_iv, 1),
        "iv_percentile": pct,
        "iv_min_hist": round(min(hist), 1),
        "iv_max_hist": round(max(hist), 1),
        "regime": regime,
        "tilt": tilt,
        "note": (f"ATM IV is at the {pct:.0f}th percentile of the last {len(hist)} "
                 f"stored samples (range {min(hist):.0f}–{max(hist):.0f}%). "
                 "High = expensive options / event fear; low = calm vs this name's recent past."),
    }


def oi_day_change(current: dict, prior: dict | None) -> dict:
    """Compare total call/put OI vs prior sample. Pure."""
    if not prior or not isinstance(prior, dict):
        return {"status": "no_prior", "note": "No prior OI snapshot yet — next run will compare."}
    def _i(d, k):
        v = d.get(k)
        return int(v) if isinstance(v, (int, float)) else None
    cc, pc = _i(current, "call_open_interest"), _i(current, "put_open_interest")
    pc_prev, pp = _i(prior, "call_open_interest"), _i(prior, "put_open_interest")
    if None in (cc, pc, pc_prev, pp):
        return {"status": "incomplete", "note": "OI fields missing for compare."}
    d_call, d_put = cc - pc_prev, pc - pp
    prior_date = prior.get("date")
    notes = []
    if d_call > 0 and abs(d_call) >= max(500, 0.05 * max(pc_prev, 1)):
        notes.append(f"Call OI +{d_call:,} since {prior_date} (new call open interest).")
    if d_call < 0 and abs(d_call) >= max(500, 0.05 * max(pc_prev, 1)):
        notes.append(f"Call OI {d_call:,} since {prior_date} (call positions closed).")
    if d_put > 0 and abs(d_put) >= max(500, 0.05 * max(pp, 1)):
        notes.append(f"Put OI +{d_put:,} since {prior_date} (new put open interest / hedges).")
    if d_put < 0 and abs(d_put) >= max(500, 0.05 * max(pp, 1)):
        notes.append(f"Put OI {d_put:,} since {prior_date} (puts closed).")
    return {
        "status": "ok",
        "prior_date": prior_date,
        "call_oi": cc, "put_oi": pc,
        "call_oi_change": d_call, "put_oi_change": d_put,
        "total_oi_change": d_call + d_put,
        "notes": notes or ["OI roughly stable vs prior sample."],
        "note": "OI change is positioning change, not aggressor side. Rising put OI can be "
                "new hedges by longs; rising call OI can be new longs or covered writes.",
    }


def term_structure_read(front_iv, back_iv, front_days, back_days) -> dict:
    """Front vs back ATM IV. Pure."""
    if not all(isinstance(x, (int, float)) and x > 0 for x in (front_iv, back_iv)):
        return {"status": "unavailable", "note": "Need two expiries with ATM IV."}
    spread = round(front_iv - back_iv, 1)
    if spread >= 8:
        shape = "steep_backwardation"
        note = (f"Front IV {front_iv:.0f}% >> back {back_iv:.0f}% (spread {spread:.0f}pp) — "
                "near-term event premium (earnings/binary). Prefer post-event entries for swings.")
        tilt = "event_front"
    elif spread >= 3:
        shape = "mild_backwardation"
        note = f"Front richer than back by {spread:.0f}pp — some near-term event pricing."
        tilt = "event_front"
    elif spread <= -5:
        shape = "contango"
        note = (f"Back IV higher than front (spread {spread:.0f}pp) — chronic/longer uncertainty "
                "or quiet front month.")
        tilt = "longer_term_uncertainty"
    else:
        shape = "flat"
        note = f"Term structure roughly flat (front−back {spread:.0f}pp)."
        tilt = "neutral"
    return {
        "status": "ok",
        "front_atm_iv_pct": round(front_iv, 1),
        "back_atm_iv_pct": round(back_iv, 1),
        "front_days": front_days,
        "back_days": back_days,
        "front_minus_back_pp": spread,
        "shape": shape,
        "tilt": tilt,
        "note": note,
    }


def _atm_iv_for_expiry(tk, spot: float, expiry: str) -> dict:
    """ATM IV + straddle expected move for one expiry."""
    chain = tk.option_chain(expiry)
    calls, puts = chain.calls, chain.puts

    def mid(row) -> float:
        b, a = float(row.get("bid") or 0), float(row.get("ask") or 0)
        return (b + a) / 2 if b and a else float(row.get("lastPrice") or 0)

    atm_call = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]].iloc[0]
    atm_put = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]].iloc[0]
    straddle = mid(atm_call) + mid(atm_put)
    ivs = [float(atm_call.get("impliedVolatility") or 0),
           float(atm_put.get("impliedVolatility") or 0)]
    atm_iv = (sum(ivs) / len(ivs) * 100) if any(ivs) else None
    call_vol = int(calls["volume"].fillna(0).sum())
    put_vol = int(puts["volume"].fillna(0).sum())
    call_oi = int(calls["openInterest"].fillna(0).sum()) if "openInterest" in calls else 0
    put_oi = int(puts["openInterest"].fillna(0).sum()) if "openInterest" in puts else 0

    def otm_iv(df, filt):
        sub = df[filt(df["strike"])]
        sub = sub[sub["impliedVolatility"].notna()]
        return float(sub["impliedVolatility"].mean()) if len(sub) else None

    put_iv = otm_iv(puts, lambda s: (s >= spot * 0.85) & (s <= spot * 0.95))
    call_iv = otm_iv(calls, lambda s: (s >= spot * 1.05) & (s <= spot * 1.15))
    unusual = []
    for df, kind in ((calls, "call"), (puts, "put")):
        hot = df[(df["volume"].fillna(0) > 500)
                 & (df["volume"].fillna(0) > 3 * df["openInterest"].fillna(0).clip(lower=1))]
        for _, r in hot.nlargest(3, "volume").iterrows():
            unusual.append({
                "type": kind, "strike": float(r["strike"]),
                "volume": int(r["volume"]),
                "open_interest": int(r["openInterest"] or 0),
                "pct_from_spot": round((float(r["strike"]) / spot - 1) * 100, 1),
            })
    return {
        "atm_iv_pct": round(atm_iv, 1) if atm_iv else None,
        "expected_move_pct": round(straddle / spot * 100, 2) if straddle and spot else None,
        "call_volume": call_vol, "put_volume": put_vol,
        "call_open_interest": call_oi, "put_open_interest": put_oi,
        "put_iv": put_iv, "call_iv": call_iv,
        "unusual_strikes": unusual,
    }


# --- network path ------------------------------------------------------------

def _signal_for(ticker: str) -> dict:
    import yfinance as yf
    tk = yf.Ticker(ticker)
    spot = float(tk.history(period="2d")["Close"].iloc[-1])
    expiries = list(tk.options or [])
    today = datetime.now().date()
    # Front: nearest expiry ≥7d; back: next with ≥21d more tenor if possible
    dated = []
    for e in expiries:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
            days = (d - today).days
            if days >= 7:
                dated.append((days, e))
        except ValueError:
            continue
    dated.sort()
    if not dated:
        return {"status": "no_chain"}
    front_days, expiry = dated[0]
    back = next((x for x in dated if x[0] >= front_days + 21), dated[1] if len(dated) > 1 else None)

    front = _atm_iv_for_expiry(tk, spot, expiry)
    back_pack = None
    if back:
        try:
            back_pack = _atm_iv_for_expiry(tk, spot, back[1])
            back_pack["expiry"] = back[1]
            back_pack["days_to_expiry"] = back[0]
        except Exception:
            back_pack = None

    put_iv, call_iv = front.get("put_iv"), front.get("call_iv")
    skew_pct = (round((put_iv - call_iv) * 100, 1) if put_iv and call_iv else None)
    skew_read = (
        "puts_bid_over_calls (downside protection in demand)"
        if put_iv and call_iv and put_iv - call_iv > 0.03 else
        "calls_bid_over_puts (speculative upside demand)"
        if put_iv and call_iv and call_iv - put_iv > 0.03 else "balanced")

    call_vol, put_vol = front["call_volume"], front["put_volume"]
    call_oi, put_oi = front["call_open_interest"], front["put_open_interest"]
    atm_iv = front.get("atm_iv_pct")

    # History: IV percentile + OI change (before appending today)
    hist = load_options_history(ticker)
    prior = hist[-1] if hist else None
    # If prior is same calendar day, use previous sample for OI delta
    today_s = today.isoformat()
    if prior and prior.get("date") == today_s and len(hist) >= 2:
        prior = hist[-2]
    elif prior and prior.get("date") == today_s:
        prior = None
    hist_ivs = [s.get("atm_iv_pct") for s in hist if s.get("date") != today_s]
    iv_rank = iv_percentile(atm_iv, hist_ivs)
    oi_chg = oi_day_change(
        {"call_open_interest": call_oi, "put_open_interest": put_oi},
        prior,
    )
    term = term_structure_read(
        atm_iv,
        (back_pack or {}).get("atm_iv_pct"),
        front_days,
        (back_pack or {}).get("days_to_expiry"),
    )
    if back_pack:
        term["back_expiry"] = back_pack.get("expiry")

    # Append / upsert today's sample
    sample = {
        "date": today_s,
        "atm_iv_pct": atm_iv,
        "expected_move_pct": front.get("expected_move_pct"),
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "expiry": expiry,
        "spot": round(spot, 2),
    }
    hist = [s for s in hist if s.get("date") != today_s]
    hist.append(sample)
    try:
        save_options_history(ticker, hist)
    except Exception:
        pass

    sig = {
        "status": "ok", "expiry": expiry, "days_to_expiry": front_days,
        "spot": round(spot, 2),
        "expected_move_pct": front.get("expected_move_pct"),
        "atm_iv_pct": atm_iv,
        "put_call_volume_ratio": round(put_vol / call_vol, 2) if call_vol else None,
        "put_call_oi_ratio": round(put_oi / call_oi, 2) if call_oi else None,
        "call_volume": call_vol, "put_volume": put_vol,
        "call_open_interest": call_oi, "put_open_interest": put_oi,
        "iv_skew_pct": skew_pct,
        "skew_read": skew_read,
        "unusual_strikes": front.get("unusual_strikes") or [],
        "iv_rank": iv_rank,
        "term_structure": term,
        "oi_change": oi_chg,
    }
    # Enrich swing read with new fields as tilts
    read = build_swing_options_read(sig)
    if iv_rank.get("status") == "ok":
        if iv_rank.get("tilt") == "caution_event_or_stress":
            read["bearish_tilts"].append({
                "source": "iv_rank", "tilt": "high_iv_rank",
                "note": iv_rank.get("note"),
            })
        elif iv_rank.get("tilt") == "calm_relatively":
            read["neutral_notes"].append({
                "source": "iv_rank", "tilt": "low_iv_rank",
                "note": iv_rank.get("note"),
            })
        else:
            read["neutral_notes"].append({
                "source": "iv_rank", "tilt": "mid_iv_rank",
                "note": iv_rank.get("note"),
            })
    if term.get("status") == "ok" and term.get("tilt") == "event_front":
        read["bearish_tilts"].append({
            "source": "term_structure", "tilt": term.get("shape"),
            "note": term.get("note"),
        })
        read["swing_playbook"]["term_structure"] = term.get("note")
    for n in (oi_chg.get("notes") or []):
        read["neutral_notes"].append({"source": "oi_change", "tilt": "positioning", "note": n})
    # Recompute net tilt after enrichment
    score = len(read["bullish_tilts"]) - len(read["bearish_tilts"])
    if score >= 2:
        read["net_tilt"] = "lean_bullish"
    elif score <= -2:
        read["net_tilt"] = "lean_bearish"
    elif score == 1:
        read["net_tilt"] = "slight_bullish"
    elif score == -1:
        read["net_tilt"] = "slight_bearish"
    else:
        read["net_tilt"] = "mixed_or_neutral"
    sig["swing_options_read"] = read
    return sig


def get_options_signals(tickers: list[str]) -> dict:
    out = {
        "status": "ok",
        "note": AMBIGUITY_NOTE,
        "learning": {
            "purpose": "Teach the brain how free options data informs a 3–90 day LONG equity swing.",
            "always_true": [
                "No aggressor side → never claim 'smart money bought calls' from volume alone.",
                "Expected move + ATR set stop/target geometry (hard via stop_engineering).",
                "P/C ratio, skew, unusual strikes, IV rank, term structure, OI change are TILTS.",
                "Elevated IV / steep backwardation into events → prefer post-event entries.",
                "IV percentile needs a few runs of history (cache warms up automatically).",
            ],
            "bullish_compatible_patterns": [
                "Call lean (P/C vol low) + call skew + price reclaiming structure",
                "Put-heavy volume WHILE price holds/rises (possible hedge of long stock)",
                "Low IV rank (calm) + constructive price structure",
                "Unusual near-OTM calls only as secondary to a real catalyst + structure",
            ],
            "bearish_or_caution_patterns": [
                "Steep put skew + falling estimates + weak price structure",
                "High IV rank / very elevated IV + large expected move into earnings",
                "Front-month backwardation (front IV >> back) = event tax",
                "Deep OTM put heat + discretionary insider selling",
                "Call lottery far OTM after a parabolic run (FOMO, late)",
                "Sharp rise in put OI with price breakdown",
            ],
        },
        "tickers": {},
    }
    for t in tickers:
        try:
            out["tickers"][t.upper()] = _signal_for(t)
        except Exception as e:
            out["tickers"][t.upper()] = {"status": "error", "reason": str(e)[:150]}
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(get_options_signals(sys.argv[1:] or ["DELL"]), indent=1))
