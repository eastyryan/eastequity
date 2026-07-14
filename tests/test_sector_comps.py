"""Offline unit tests for build_sector_comps in tools.universe_scanner - the
peer-relative valuation layer (sector median fwd P/E, premium/discount %,
growth-vs-sector delta, cheapness rank, honest coverage counts). No network,
no pandas/numpy.

    python3 tests/test_sector_comps.py

These guard the honesty rules specifically: the >=4-peer gate on medians (a
"sector median" of 3 names is not a sector median), negative/missing estimates
excluded from pools without dropping the ticker's row, and coverage always
disclosing the true denominators.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.universe_scanner import (  # noqa: E402
    build_sector_comps, _median, MIN_SECTOR_PEERS,
)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _row(t, sector, pe=None, growth=None):
    r = {"ticker": t, "sector": sector}
    if pe is not None:
        r["fwd_pe_est"] = pe
    if growth is not None:
        r["fwd_growth_pct"] = growth
    return r


# Two sectors: "chips" is rich (5 valid PEs, 6 growth points, one missing-PE
# name, one negative-PE name); "software" is thin (3 names -> gated medians).
ROWS = [
    _row("AAA", "chips", pe=10.0, growth=30.0),
    _row("BBB", "chips", pe=12.0, growth=20.0),
    _row("CCC", "chips", pe=14.0, growth=16.0),
    _row("DDD", "chips", pe=20.0, growth=10.0),
    _row("EEE", "chips", pe=25.0, growth=8.0),
    _row("FFF", "chips", pe=None, growth=12.0),   # missing EPS -> no own P/E
    _row("NEG", "chips", pe=-8.0),                # negative-EPS "P/E" -> excluded
    _row("GGG", "software", pe=30.0, growth=25.0),
    _row("HHH", "software", pe=35.0, growth=22.0),
    _row("III", "software", pe=40.0, growth=18.0),
]
# chips PE pool: [10, 12, 14, 20, 25]        -> median 14.0, pe_n = 5
# chips growth pool: [30, 20, 16, 10, 8, 12] -> median (12+16)/2 = 14.0, growth_n = 6


def test_median_helper():
    print("_median (pure helper):")
    check("empty -> None", _median([]) is None)
    check("odd count picks middle", _median([3, 1, 2]) == 2)
    check("even count averages middle pair", _median([4, 1, 3, 2]) == 2.5)
    check("single value is its own median", _median([7.0]) == 7.0)


def test_gate_constant():
    print("median gate constant:")
    check("MIN_SECTOR_PEERS is 4", MIN_SECTOR_PEERS == 4)


def test_median_math():
    print("sector medians over ALL same-sector data points:")
    comps = build_sector_comps(ROWS)
    a = comps["AAA"]
    check("chips median fwd P/E = 14.0 (odd pool of 5)",
          a["sector_median_fwd_pe"] == 14.0, str(a["sector_median_fwd_pe"]))
    check("chips median growth = 14.0 (even pool of 6 averages middle pair)",
          a["sector_median_fwd_growth_pct"] == 14.0,
          str(a["sector_median_fwd_growth_pct"]))
    check("sector label carried through", a["sector"] == "chips")
    check("every input row with ticker+sector gets an entry",
          set(comps) == {r["ticker"] for r in ROWS})


def test_premium_pct():
    print("fwd_pe_premium_pct = (own / sector median - 1) * 100:")
    comps = build_sector_comps(ROWS)
    check("AAA at 10x vs 14x = -28.6% (discount is negative)",
          comps["AAA"]["fwd_pe_premium_pct"] == -28.6,
          str(comps["AAA"]["fwd_pe_premium_pct"]))
    check("CCC at the median = 0.0%", comps["CCC"]["fwd_pe_premium_pct"] == 0.0)
    check("EEE at 25x vs 14x = +78.6%",
          comps["EEE"]["fwd_pe_premium_pct"] == 78.6,
          str(comps["EEE"]["fwd_pe_premium_pct"]))


def test_growth_delta():
    print("growth_vs_sector_pp = own growth minus sector median (pp):")
    comps = build_sector_comps(ROWS)
    check("AAA: 30 - 14 = +16.0pp", comps["AAA"]["growth_vs_sector_pp"] == 16.0)
    check("EEE: 8 - 14 = -6.0pp", comps["EEE"]["growth_vs_sector_pp"] == -6.0)
    check("FFF (no P/E) still gets its growth delta: 12 - 14 = -2.0pp",
          comps["FFF"]["growth_vs_sector_pp"] == -2.0,
          str(comps["FFF"]["growth_vs_sector_pp"]))


def test_rank_string():
    print("pe_rank_in_sector ('3/17 cheapest' style, rank 1 = cheapest):")
    comps = build_sector_comps(ROWS)
    check("cheapest name is 1/5", comps["AAA"]["pe_rank_in_sector"] == "1/5 cheapest",
          str(comps["AAA"]["pe_rank_in_sector"]))
    check("middle name is 3/5", comps["CCC"]["pe_rank_in_sector"] == "3/5 cheapest",
          str(comps["CCC"]["pe_rank_in_sector"]))
    check("dearest name is 5/5", comps["EEE"]["pe_rank_in_sector"] == "5/5 cheapest",
          str(comps["EEE"]["pe_rank_in_sector"]))
    check("rank denominator counts only names WITH a valid estimate (5, not 7)",
          comps["AAA"]["pe_rank_in_sector"].endswith("/5 cheapest"))


def test_thin_sector_gate():
    print(f"the >={MIN_SECTOR_PEERS} gate: a 3-name sector gets NO medians:")
    comps = build_sector_comps(ROWS)
    g = comps["GGG"]
    check("median fwd P/E is None", g["sector_median_fwd_pe"] is None)
    check("median growth is None", g["sector_median_fwd_growth_pct"] is None)
    check("premium is None (no median to compare against)",
          g["fwd_pe_premium_pct"] is None)
    check("growth delta is None", g["growth_vs_sector_pp"] is None)
    check("coverage still HONESTLY reports 3/3",
          g["coverage"] == {"pe_n": 3, "growth_n": 3}, str(g["coverage"]))
    check("own fwd_pe_est still reported", g["fwd_pe_est"] == 30.0)
    check("rank still quoted with its honest denominator",
          comps["HHH"]["pe_rank_in_sector"] == "2/3 cheapest",
          str(comps["HHH"]["pe_rank_in_sector"]))
    check("exactly 4 points DOES clear the gate",
          build_sector_comps([_row(t, "s", pe=float(p)) for t, p in
                              zip("abcd", (8, 10, 12, 14))])["a"]
          ["sector_median_fwd_pe"] == 11.0)


def test_missing_eps_handling():
    print("missing estimate: excluded from pool, row still reported:")
    comps = build_sector_comps(ROWS)
    f = comps["FFF"]
    check("own fwd_pe_est None", f["fwd_pe_est"] is None)
    check("premium None", f["fwd_pe_premium_pct"] is None)
    check("no rank without an estimate", f["pe_rank_in_sector"] is None)
    check("sector median still visible to it", f["sector_median_fwd_pe"] == 14.0)
    check("coverage: pe_n=5 (FFF and NEG excluded), growth_n=6",
          f["coverage"] == {"pe_n": 5, "growth_n": 6}, str(f["coverage"]))


def test_negative_eps_excluded():
    print("negative-EPS 'P/E' excluded from pools and own fields:")
    comps = build_sector_comps(ROWS)
    n = comps["NEG"]
    check("own fwd_pe_est None (not -8.0)", n["fwd_pe_est"] is None)
    check("premium None", n["fwd_pe_premium_pct"] is None)
    check("rank None", n["pe_rank_in_sector"] is None)
    check("did not poison the sector median (still 14.0)",
          n["sector_median_fwd_pe"] == 14.0)
    check("pe_n stays 5 with NEG present", n["coverage"]["pe_n"] == 5)


def test_degenerate_inputs():
    print("degenerate inputs fail soft:")
    check("empty rows -> {}", build_sector_comps([]) == {})
    check("row without sector is skipped entirely",
          "X" not in build_sector_comps([{"ticker": "X", "fwd_pe_est": 10.0}]))
    check("row without ticker contributes to pools but gets no entry",
          build_sector_comps([{"sector": "s", "fwd_pe_est": 10.0}]) == {})
    booly = build_sector_comps([_row("B1", "s", pe=True),
                                _row("B2", "s", growth=False)])
    check("bools are not numbers (pe)", booly["B1"]["fwd_pe_est"] is None)
    check("bools are not numbers (growth)", booly["B2"]["fwd_growth_pct"] is None)


if __name__ == "__main__":
    for fn in (test_median_helper, test_gate_constant, test_median_math,
               test_premium_pct, test_growth_delta, test_rank_string,
               test_thin_sector_gate, test_missing_eps_handling,
               test_negative_eps_excluded, test_degenerate_inputs):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All sector-comps checks passed.")
