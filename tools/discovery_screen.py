"""Discovery screen — weekly broad-market sweep feeding the Sunday universe review.

The curated universe is a cage as well as a filter: names that never enter it can
never be traded. This screen sweeps a static ~600-name candidate pool
(data/discovery_candidates.json: S&P 500 core + Nasdaq 100 + liquid mid-caps,
maintained by hand ~quarterly), EXCLUDES everything already in the universe
(validator.load_universe(), so the pool file never needs editing when the universe
changes), and ranks the rest by a momentum / relative-strength blend so the weekly
universe curation picks from the whole market instead of only what it already knows.

Discipline:
- Trailing COMPLETED bars only (a still-open session's bar is dropped) — no lookahead.
- Liquidity floor: 20-session median dollar volume >= $20M (MIN_ADV_USD, same bar as
  the universe scanner); names below it are DROPPED, not flagged — an illiquid name
  cannot earn a universe slot through this funnel.
- Fail-soft everywhere: a bad chunk download, a bad ticker, or a missing benchmark
  degrades the output (labeled in `status` / counters) instead of crashing the run.
- All per-name math is pure helpers (list-in/scalar-out) unit-tested offline in
  tests/test_discovery_screen.py; shared window helpers are imported from
  tools.universe_scanner so the session-window semantics stay identical.

Output: dict written to data/discovery_screen.json —
  {"status", "as_of", "scanned", "excluded_in_universe", "top_candidates": [...]}.

CLI: python -m tools.discovery_screen [--top N]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Shared pure helpers — identical session-window semantics to the universe scanner.
from tools.universe_scanner import (
    SESS_1M, SESS_3M, SESS_6M, SESS_ADV, MIN_ADV_USD,
    _median_dollar_volume, _rel_strength, _return_over_sessions,
    # Session-completeness semantics are now CANONICAL in universe_scanner (it
    # needed them too - it runs 4x/day on intraday slots and was computing
    # vol_surge on a half-formed bar). Imported rather than duplicated so the
    # weekly screen and the daily scanner can never disagree about what "a
    # completed session" means.
    _chunks, _completed_series, _last_bar_is_partial,
)

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = ROOT / "data" / "discovery_candidates.json"
# Rebuild the pool when the file is older than this. Index membership drifts and
# a hand-maintained list ages silently; 21 days is well inside a quarter.
POOL_MAX_AGE_DAYS = 21
# A rebuild that produces fewer than this is treated as a PARTIAL fetch and
# rejected, so one unreachable source can never quietly narrow the sweep.
# The full merge (SPX + Russell 1000 + NDX + EM ADR) is ~1130.
POOL_MIN_TICKERS = 800
OUTPUT_PATH = ROOT / "data" / "discovery_screen.json"

# ~150 tickers per yf.download call: large enough to keep the sweep to ~4 batches,
# small enough that one failed batch loses a quarter of the pool, not all of it.
CHUNK_SIZE = 150

# Fetch ~7 months of daily bars so the trailing 126-SESSION (6m) windows are
# genuinely full after weekends/holidays; every labeled window is still counted in
# sessions (SESS_*), never "whatever the fetch returned" — same rule as the scanner.
FETCH_PERIOD = "7mo"

# Admission floor: enough bars for a true 3m momentum read (63 sessions -> 64 bars).
# 6m fields degrade to None on shorter histories rather than being mislabeled.
MIN_BARS = SESS_3M + 1

# Score = momentum / relative-strength blend (documented weights, summing to 1.0):
#   0.35 * 3m momentum      — the primary swing-relevant trend window
#   0.35 * 3m rel strength  — vs SPY, same window: market-beaters outrank beta-riders
#   0.15 * 1m momentum      — recency: is the move still alive?
#   0.15 * 1m rel strength
# Inputs are plain fractions; the score is reported as ~percent (x100). When the
# benchmark is unavailable the score degrades to momentum-only (0.7 * 3m + 0.3 * 1m)
# and the run is labeled degraded_no_benchmark — fail-soft, never a crash.
W_MOM_3M, W_RS_3M, W_MOM_1M, W_RS_1M = 0.35, 0.35, 0.15, 0.15
UPTREND_SESSIONS = 50  # simple uptrend flag: last close > 50-session SMA


def _dist_from_high_pct(last, highs, sessions: int = SESS_6M):
    """Percent distance of `last` from the highest of the trailing `sessions` highs
    (<= 0 when at/below the high). None on missing inputs. Pure (list+scalar in,
    scalar out) for offline tests. Uses whatever history exists when shorter than
    `sessions` — callers get MIN_BARS-gated inputs, so the window is never trivial."""
    if last is None or not highs:
        return None
    window = [h for h in highs[-sessions:] if h is not None]
    if not window:
        return None
    hi = max(window)
    if not hi or hi <= 0:
        return None
    return (last / hi - 1) * 100.0


def _uptrend_flag(closes, sessions: int = UPTREND_SESSIONS):
    """Simple uptrend flag: last close above the `sessions`-session SMA. None when
    history is too short (never a fabricated trend read). Pure."""
    if not closes or len(closes) < sessions:
        return None
    window = [c for c in closes[-sessions:] if c is not None]
    if len(window) < sessions or closes[-1] is None:
        return None
    return closes[-1] > sum(window) / len(window)


def _passes_liquidity(adv_usd, floor: float = MIN_ADV_USD) -> bool:
    """Liquidity gate: True only with a real ADV at/above the floor. Unlike the
    universe scanner (which FLAGS illiquidity because held names must survive the
    scan), discovery DROPS illiquid names — they cannot earn a universe slot. Pure."""
    return isinstance(adv_usd, (int, float)) and adv_usd >= floor


def _blend_score(mom_3m, mom_1m, rs_3m, rs_1m):
    """Momentum/RS blend (see weight docs above). Fractions in, ~percent score out.
    - 3m momentum missing -> None (name not scoreable; caller skips it).
    - Rel strength missing (benchmark down) -> momentum-only fallback 0.7/0.3.
    - 1m legs missing -> treated as 0 contribution. Pure."""
    if mom_3m is None:
        return None
    if rs_3m is None:
        s = 0.7 * mom_3m + 0.3 * (mom_1m or 0.0)
    else:
        s = (W_MOM_3M * mom_3m + W_RS_3M * rs_3m
             + W_MOM_1M * (mom_1m or 0.0) + W_RS_1M * (rs_1m or 0.0))
    return round(s * 100.0, 2)





def run_discovery(top_n: int = 25) -> dict:
    """Sweep the candidate pool, exclude universe members, rank non-members by the
    momentum/RS blend, and write the result to data/discovery_screen.json."""
    out: dict = {
        "status": "ok",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "note": ("Weekly broad-market discovery sweep for the Sunday universe review. "
                 "Candidates ranked by a momentum/relative-strength blend "
                 "(0.35*mom_3m + 0.35*rs_3m + 0.15*mom_1m + 0.15*rs_1m, fractions x100); "
                 "universe members excluded at runtime; liquidity floor $20M median "
                 "20d dollar volume (below = dropped); trailing completed bars only. "
                 "These are CANDIDATES for curation research, not trade signals."),
    }

    # --- candidate pool (rebuilt when stale; fail-soft to the file on disk) ---
    #
    # THE INCIDENT (2026-07-19). tools/discovery_pool.py builds this file from
    # Wikipedia SPX (503) + Russell 1000 (1016) + NDX (101) + the EM ADR seed
    # (80) — 1132 names merged. NOTHING EVER CALLED IT. No cron, no workflow, no
    # script referenced it; it was manual-only. So the pool sat at a 587-name
    # hand-maintained fallback whose own note said "S&P 500 core + Nasdaq 100 +
    # notable liquid mid-caps" — no Russell 1000 at all.
    #
    # The weekly sweep therefore screened ~405 non-universe names instead of
    # ~950, and the ~545 invisible ones were the Russell 1000 mid-cap tail —
    # exactly where rotation out of mega-cap AI shows up first. On the week the
    # brain concluded the AI stack was unwinding, this lane was blind to most of
    # where the money was going.
    #
    # Rebuilt HERE rather than on a schedule on purpose: the launchd jobs are not
    # even loaded on the operator's machine, so a scheduler-dependent refresh is
    # just another thing that silently never runs. Staleness-triggered and
    # fail-soft means the pool maintains itself wherever the screen runs, and a
    # failed rebuild degrades to the existing file instead of losing the sweep.
    pool_meta: dict = {}
    try:
        age_days = (datetime.now(timezone.utc).timestamp()
                    - CANDIDATES_PATH.stat().st_mtime) / 86400.0
    except Exception:
        age_days = None
    if age_days is None or age_days > POOL_MAX_AGE_DAYS:
        try:
            from tools.discovery_pool import build_pool, write_pool
            doc_new = build_pool()
            n_new = len(doc_new.get("tickers") or [])
            if n_new < POOL_MIN_TICKERS:
                # A partial build (one source unreachable) must not SHRINK the
                # pool — that is how coverage silently narrows. Keep what we have.
                pool_meta["rebuilt"] = False
                pool_meta["rebuild_rejected"] = (
                    f"built only {n_new} tickers (< {POOL_MIN_TICKERS}); kept the "
                    f"existing pool rather than narrowing coverage")
            else:
                write_pool(doc_new)
                pool_meta["rebuilt"] = True
                pool_meta["rebuild_n"] = n_new
                pool_meta["rebuild_sources"] = (
                    doc_new.get("sources_status") or doc_new.get("stats"))
        except Exception as e:
            pool_meta["rebuilt"] = False
            pool_meta["rebuild_error"] = f"{type(e).__name__}: {str(e)[:120]}"

    try:
        doc = json.loads(CANDIDATES_PATH.read_text())
        pool = sorted({str(t).upper() for t in doc.get("tickers", []) if t})
        if not pool:
            raise ValueError("empty ticker list")
    except Exception as e:
        out.update({"status": f"error_candidates_unreadable:{type(e).__name__}",
                    "scanned": 0, "excluded_in_universe": 0, "top_candidates": []})
        _write_output(out)
        return out
    out["candidate_pool"] = len(pool)
    # Disclose the pool's provenance and age. A static list that quietly ages is
    # indistinguishable in the output from a freshly-built one, and that is how
    # 545 names went missing without anything saying so.
    try:
        pool_meta["age_days"] = (None if age_days is None else round(age_days, 1))
        pool_meta["built_at"] = doc.get("as_of")
        pool_meta["max_age_days"] = POOL_MAX_AGE_DAYS
    except Exception:
        pass
    out["pool_provenance"] = pool_meta

    # --- exclude current universe members (the whole point of the screen) ---
    try:
        import validator
        universe = validator.load_universe()
    except Exception as e:
        universe = set()
        out["status"] = f"degraded_universe_unreadable:{type(e).__name__}"
    scan_list = [t for t in pool if t not in universe]
    out["excluded_in_universe"] = len(pool) - len(scan_list)

    import yfinance as yf
    from tools.fundamental_screen import _eastern_now
    now_et = _eastern_now()

    # --- benchmark (SPY) for relative strength; fail-soft to momentum-only ---
    spy_1m = spy_3m = None
    try:
        spy = yf.download("SPY", period=FETCH_PERIOD, interval="1d",
                          auto_adjust=True, progress=False)
        spy = _completed_series(spy, now_et)
        spy_closes = [float(x) for x in spy["Close"].dropna().values.reshape(-1).tolist()]
        spy_1m = _return_over_sessions(spy_closes, SESS_1M)
        spy_3m = _return_over_sessions(spy_closes, SESS_3M)
    except Exception:
        pass
    if spy_3m is None and out["status"] == "ok":
        out["status"] = "degraded_no_benchmark"

    # --- batched sweep, fail-soft per chunk and per ticker ---
    rows: list[dict] = []
    scanned = insufficient = illiquid = no_data = chunk_errors = 0
    for chunk in _chunks(scan_list, CHUNK_SIZE):
        try:
            data = yf.download(chunk, period=FETCH_PERIOD, interval="1d",
                               group_by="ticker", auto_adjust=True,
                               progress=False, threads=True)
        except Exception:
            chunk_errors += 1
            continue
        for t in chunk:
            try:
                df = _completed_series(data[t].dropna(), now_et)
                n = len(df)
                if n == 0:
                    no_data += 1
                    continue
                if n < MIN_BARS:
                    insufficient += 1
                    continue
                closes = [float(x) for x in df["Close"].tolist()]
                highs = [float(x) for x in df["High"].tolist()]
                vols = [float(x) for x in df["Volume"].tolist()]
                scanned += 1

                adv = _median_dollar_volume(closes, vols, SESS_ADV)
                if not _passes_liquidity(adv):
                    illiquid += 1
                    continue

                mom_1m = _return_over_sessions(closes, SESS_1M)
                mom_3m = _return_over_sessions(closes, SESS_3M)
                rs_1m = _rel_strength(mom_1m, spy_1m)
                rs_3m = _rel_strength(mom_3m, spy_3m)
                score = _blend_score(mom_3m, mom_1m, rs_3m, rs_1m)
                if score is None:
                    continue
                uptrend = _uptrend_flag(closes)
                rows.append({
                    "ticker": t,
                    "score": score,
                    "momentum_3m_pct": round(mom_3m * 100, 1),
                    "momentum_1m_pct": round(mom_1m * 100, 1) if mom_1m is not None else None,
                    "rel_strength_3m_pct": round(rs_3m * 100, 1) if rs_3m is not None else None,
                    "rel_strength_1m_pct": round(rs_1m * 100, 1) if rs_1m is not None else None,
                    "dist_from_6m_high_pct": (round(d, 1) if (d := _dist_from_high_pct(
                        closes[-1], highs, SESS_6M)) is not None else None),
                    "adv_usd": round(adv),
                    "above_50dma": uptrend,
                    "last_close": round(closes[-1], 2),
                    "bars": n,
                })
            except Exception:
                no_data += 1
                continue

    rows.sort(key=lambda r: (r["score"], r["adv_usd"]), reverse=True)
    out.update({
        "scanned": scanned,
        "dropped_illiquid": illiquid,
        "dropped_insufficient_history": insufficient,
        "dropped_no_data": no_data,
        "chunk_errors": chunk_errors,
        "top_candidates": rows[:top_n],
    })
    _write_output(out)
    return out


def _write_output(out: dict) -> None:
    """Persist the sweep for the Sunday universe review. Fail-soft: a write error is
    recorded in the returned dict, never raised. KEEP-LAST-GOOD: a sweep that found
    nothing (blocked/flaky feed) must not replace a real candidate list with an
    empty one wearing a fresh timestamp - the review would then curate from nothing."""
    try:
        if not out.get("top_candidates") and OUTPUT_PATH.exists():
            try:
                prior = json.loads(OUTPUT_PATH.read_text())
                if prior.get("top_candidates"):
                    out["write_skipped"] = ("empty sweep - kept last-good candidate "
                                            "file from " + str(prior.get("as_of")))
                    return
            except Exception:
                pass  # prior file unreadable - writing the fresh (even empty) one is fine
        OUTPUT_PATH.write_text(json.dumps(out, indent=1) + "\n")
    except Exception as e:
        out["write_error"] = type(e).__name__


if __name__ == "__main__":
    import sys
    n = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 25
    result = run_discovery(n)
    print(json.dumps(result, indent=2))
