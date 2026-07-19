"""Offline unit tests for the pure scoring/filter helpers in tools.discovery_screen
(synthetic price lists; no network, no pandas — the module imports yfinance only
inside run_discovery). Also sanity-checks the static candidate pool file.

    python3 tests/test_discovery_screen.py

These guard the discovery funnel's hard rules: the documented score weights, the
liquidity DROP (not flag), the no-lookahead partial-bar guard, and honest None on
short histories instead of fabricated windows.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.discovery_screen import (  # noqa: E402
    _blend_score, _chunks, _dist_from_high_pct, _last_bar_is_partial,
    _passes_liquidity, _uptrend_flag,
    CHUNK_SIZE, MIN_BARS, UPTREND_SESSIONS,
    W_MOM_1M, W_MOM_3M, W_RS_1M, W_RS_3M,
    SESS_1M, SESS_3M, SESS_6M, MIN_ADV_USD, CANDIDATES_PATH,
)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_constants():
    print("constants / documented weights:")
    check("session windows match the scanner (21/63/126)",
          (SESS_1M, SESS_3M, SESS_6M) == (21, 63, 126))
    check("MIN_BARS admits a true 3m momentum read", MIN_BARS == SESS_3M + 1)
    check("score weights sum to 1.0",
          abs((W_MOM_3M + W_RS_3M + W_MOM_1M + W_RS_1M) - 1.0) < 1e-12)
    check("3m legs dominate 1m legs", W_MOM_3M > W_MOM_1M and W_RS_3M > W_RS_1M)
    check("liquidity floor is $20M", MIN_ADV_USD == 20_000_000)
    check("uptrend flag uses the 50-session SMA", UPTREND_SESSIONS == 50)


def test_blend_score():
    print("blend score (momentum/RS blend, fractions in, ~percent out):")
    # Hand-computed: 0.35*0.20 + 0.35*0.10 + 0.15*0.05 + 0.15*0.02 = 0.1155 -> 11.55
    s = _blend_score(0.20, 0.05, 0.10, 0.02)
    check("hand-computed blend", s is not None and abs(s - 11.55) < 1e-9, f"got {s}")
    check("3m momentum missing -> None (not scoreable)",
          _blend_score(None, 0.05, 0.10, 0.02) is None)
    # Benchmark down: momentum-only fallback 0.7/0.3 (documented degraded mode).
    s2 = _blend_score(0.20, 0.10, None, None)
    check("no benchmark -> momentum-only 0.7/0.3 fallback",
          s2 is not None and abs(s2 - 17.0) < 1e-9, f"got {s2}")
    # Missing 1m legs contribute zero, not a crash.
    s3 = _blend_score(0.20, None, 0.10, None)
    check("missing 1m legs contribute 0",
          s3 is not None and abs(s3 - (0.35 * 0.20 + 0.35 * 0.10) * 100) < 1e-9, f"got {s3}")
    # Ordering sanity: stronger momentum + RS must outrank weaker.
    strong = _blend_score(0.30, 0.10, 0.20, 0.05)
    weak = _blend_score(0.05, 0.01, -0.02, -0.01)
    check("stronger momentum/RS scores higher", strong > weak, f"{strong} vs {weak}")
    check("negative momentum -> negative score", _blend_score(-0.20, -0.10, -0.15, -0.05) < 0)


def test_dist_from_high():
    print("distance from trailing 6m high:")
    highs = [100.0] * 130
    check("at the high -> 0", abs(_dist_from_high_pct(100.0, highs)) < 1e-9)
    d = _dist_from_high_pct(80.0, highs)
    check("20% below the high -> -20", d is not None and abs(d - (-20.0)) < 1e-9, f"got {d}")
    # Only the trailing SESS_6M bars count: an ancient spike outside the window
    # must NOT be the reference high.
    highs2 = [500.0] + [100.0] * 126
    d2 = _dist_from_high_pct(90.0, highs2, SESS_6M)
    check("spike outside trailing 126 ignored",
          d2 is not None and abs(d2 - (-10.0)) < 1e-9, f"got {d2}")
    check("empty highs -> None", _dist_from_high_pct(100.0, []) is None)
    check("missing last -> None", _dist_from_high_pct(None, highs) is None)
    check("all-None highs -> None", _dist_from_high_pct(100.0, [None, None]) is None)
    check("zero high -> None (no div-by-zero)", _dist_from_high_pct(1.0, [0.0]) is None)


def test_uptrend_flag():
    print("uptrend flag (last > 50-session SMA):")
    rising = [float(i) for i in range(1, 61)]  # 60 rising closes
    check("rising series -> True", _uptrend_flag(rising) is True)
    falling = [float(i) for i in range(60, 0, -1)]
    check("falling series -> False", _uptrend_flag(falling) is False)
    check("short history -> None (no fabricated trend)",
          _uptrend_flag(rising[:49]) is None)
    check("empty -> None", _uptrend_flag([]) is None)
    check("None last close -> None", _uptrend_flag([1.0] * 59 + [None]) is None)


def test_liquidity_gate():
    print("liquidity gate (drop below $20M ADV — a hard rule, not a flag):")
    check("$100M passes", _passes_liquidity(100_000_000) is True)
    check("exactly $20M passes", _passes_liquidity(20_000_000) is True)
    check("$19.99M dropped", _passes_liquidity(19_990_000) is False)
    check("None dropped (no ADV = no admission)", _passes_liquidity(None) is False)
    check("garbage dropped", _passes_liquidity("big") is False)


def test_partial_bar_guard():
    print("no-lookahead guard (drop the still-forming session bar):")
    check("today's bar before the close -> partial",
          _last_bar_is_partial("2026-07-13", "2026-07-13", 13 * 60) is True)
    check("today's bar after 16:00 ET -> completed",
          _last_bar_is_partial("2026-07-13", "2026-07-13", 16 * 60) is False)
    check("yesterday's bar -> completed",
          _last_bar_is_partial("2026-07-10", "2026-07-13", 13 * 60) is False)
    check("empty bar date -> not partial (fail-soft)",
          _last_bar_is_partial("", "2026-07-13", 13 * 60) is False)


def test_chunks():
    print("chunking (batched downloads, fail-soft per chunk):")
    check("splits evenly", _chunks(list(range(10)), 5) == [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]])
    ch = _chunks(list(range(7)), 3)
    check("remainder chunk kept", ch == [[0, 1, 2], [3, 4, 5], [6]])
    check("empty list -> no chunks", _chunks([], 5) == [])
    check("CHUNK_SIZE is ~150", CHUNK_SIZE == 150)


def test_candidates_file():
    print("discovery_candidates.json (static pool, offline sanity):")
    doc = json.loads(CANDIDATES_PATH.read_text())
    tickers = doc.get("tickers", [])
    # WAS 450-600, which pinned the BROKEN state as correct. tools/discovery_pool.py
    # merges Wikipedia SPX (503) + Russell 1000 (1016) + NDX (101) + the EM ADR seed
    # (80) into ~1130 names — but nothing ever called it, so the pool sat at a
    # 587-name hand-maintained fallback with no Russell 1000 in it, and this
    # assertion certified that as healthy. The weekly sweep was screening ~405
    # non-universe names instead of ~950; the ~545 missing ones were the mid-cap
    # tail, which is exactly where rotation out of mega-cap AI shows up first.
    # The floor is the point: an upper bound here would re-create the same trap.
    check("pool >= 800 tickers (SPX + R1000 + NDX + EM ADR)",
          len(tickers) >= 800, str(len(tickers)))
    check("all unique", len(tickers) == len(set(tickers)))
    check("all uppercase US-listing format",
          all(re.fullmatch(r"[A-Z][A-Z0-9\-]{0,5}", t) for t in tickers))
    banned = {"TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXU", "UPRO", "TSLL", "NVDL"}
    check("no leveraged/inverse products", not banned & set(tickers))
    check("as_of + note present", bool(doc.get("as_of")) and bool(doc.get("note")))
    # The pool must include universe members (runtime exclusion keeps the file
    # stable as the universe evolves) AND genuine non-universe discovery fodder.
    check("universe members included (runtime-excluded by design)",
          "NVDA" in tickers and "MSFT" in tickers)
    check("non-universe names present", "CMI" in tickers and "ODFL" in tickers)
    universe = {t.upper() for ts in json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "universe.json").read_text()
    )["sectors"].values() for t in ts}
    outside = [t for t in tickers if t not in universe]
    check("pool extends well beyond the universe (300+ non-members)",
          len(outside) >= 300, str(len(outside)))


if __name__ == "__main__":
    for fn in (test_constants, test_blend_score, test_dist_from_high,
               test_uptrend_flag, test_liquidity_gate, test_partial_bar_guard,
               test_chunks, test_candidates_file):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All discovery-screen helper checks passed.")


def test_pool_rebuild_is_wired_and_cannot_narrow_coverage():
    """The pool must maintain itself, and a partial fetch must never shrink it.

    THE INCIDENT (2026-07-19). tools/discovery_pool.py could build a ~1130-name
    pool from SPX + Russell 1000 + NDX + EM ADR. NOTHING CALLED IT — manual-only,
    no cron, no workflow — so the file sat at a 587-name fallback and the weekly
    sweep screened ~405 non-universe names instead of ~950.

    Rebuilt on STALENESS from inside the screen rather than on a schedule,
    because the operator's launchd jobs are not loaded and a scheduler-dependent
    refresh is just another thing that silently never runs.
    """
    import tools.discovery_screen as D
    src = (Path(__file__).resolve().parent.parent
           / "tools" / "discovery_screen.py").read_text()
    check("rebuild wired into the screen", "build_pool" in src and "write_pool" in src)
    check("staleness-triggered", "POOL_MAX_AGE_DAYS" in src)
    check("age disclosed in output", "pool_provenance" in src)
    # A partial build must be REJECTED, not written — one unreachable source
    # cannot be allowed to quietly narrow the sweep.
    check("partial build rejected", "POOL_MIN_TICKERS" in src
          and "rebuild_rejected" in src)
    check("floor is meaningful", D.POOL_MIN_TICKERS >= 800, str(D.POOL_MIN_TICKERS))
