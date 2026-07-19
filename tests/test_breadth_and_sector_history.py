"""Breadth must be honest about its sample, and sector history must be durable.

WHY BOTH EXIST (2026-07-19 weekly run)
--------------------------------------
The brain reached that run's sharpest conclusion — "a momentum-factor unwind
concentrated inside the AI stack" — by reading eleven sector momentum averages
and inferring it. That was correct, slow, and unauditable. The bundle carried no
advance/decline, no percentage above the 200-DMA, no new-high/low count, and
`sector_relative_strength` was recomputed and DISCARDED every run, so the finding
lived only in one response's prose. Nothing could later ask "was the AI stack
unwinding on 2026-07-19?"

THE TWO RULES PINNED HERE
  1. A measure that cannot be computed is null WITH A REASON, never zero.
     "0% above their 200-DMA" and "we had no 200-DMA data" are opposite claims.
  2. The sample is always stated. Breadth over this book's 184-name AI-biased
     universe is NOT market breadth, and must never be citable as though it were.
"""

from __future__ import annotations

import json

import pytest

from tools.market_breadth import compute_breadth
from tools import sector_history as SH


# ---------------------------------------------------------------------------
# breadth
# ---------------------------------------------------------------------------

def _row(**kw) -> dict:
    base = {"ticker": "AAA", "above_200dma": True, "above_50dma": True,
            "trend_up_50_over_200": True, "pct_change_1d": 1.0,
            "rel_strength_1m_pct": 5.0, "rel_strength_3m_pct": 5.0,
            "dist_from_52w_high_pct": 3.0, "momentum_1m_pct": 4.0,
            "momentum_3m_pct": 9.0}
    base.update(kw)
    return base


def test_rich_sample_computes_every_measure():
    rows = ([_row(above_200dma=True, pct_change_1d=1.0)] * 6
            + [_row(above_200dma=False, pct_change_1d=-1.0)] * 4)
    b = compute_breadth(rows, sample="universe")
    assert b["pct_above_200dma"] == 60.0
    assert b["advancers"] == 6 and b["decliners"] == 4
    assert b["advance_decline_ratio"] == 1.5
    assert b["net_advancers"] == 2
    assert b["unavailable"] == {}


def test_missing_measure_is_null_with_a_reason_never_zero():
    """RULE 1. The weekly sweep has no 1-day bar and no 200-DMA."""
    thin = [{"ticker": "AAA", "above_50dma": True, "rel_strength_1m_pct": 2.0,
             "dist_from_6m_high_pct": 5.0, "momentum_3m_pct": 10.0}] * 5
    b = compute_breadth(thin, sample="broad")
    assert b["pct_above_200dma"] is None, "absent data must not read as 0%"
    assert "pct_above_200dma" in b["unavailable"]
    assert "advance_decline" in b["unavailable"]
    assert b["pct_above_50dma"] == 100.0          # what IS present still computes


def test_names_without_data_are_excluded_not_counted_as_false():
    """A name with no 200-DMA history is not a name BELOW its 200-DMA."""
    rows = [_row(above_200dma=True), _row(above_200dma=True),
            _row(above_200dma=None)]
    b = compute_breadth(rows, sample="universe")
    assert b["pct_above_200dma"] == 100.0
    assert b["pct_above_200dma_n"] == 2, "the unknown row must leave the denominator"


def test_sample_is_always_stated():
    """RULE 2. Universe breadth is not market breadth."""
    b = compute_breadth([_row()], sample="universe", note="AI-biased hunting ground")
    assert b["sample"] == "universe"
    assert "AI-biased" in b["note"]
    assert b["sample"] in b["read"], "the read must carry its own sample label"


def test_narrow_participation_is_called_narrow():
    rows = ([_row(above_200dma=False, rel_strength_1m_pct=-5.0)] * 8
            + [_row(above_200dma=True, rel_strength_1m_pct=20.0)] * 2)
    read = compute_breadth(rows, sample="universe")["read"]
    assert "narrow" in read
    assert "carried by a minority" in read


def test_broad_participation_is_called_broad():
    rows = [_row(above_200dma=True, rel_strength_1m_pct=6.0)] * 10
    assert "broad" in compute_breadth(rows, sample="universe")["read"]


def test_read_admits_when_it_cannot_conclude():
    b = compute_breadth([{"ticker": "AAA", "momentum_3m_pct": 5.0}], sample="broad")
    assert "insufficient fields" in b["read"]


def test_highs_lows_are_labelled_as_proximity_not_prints():
    """Daily bars cannot see intraday extremes; claiming otherwise is fabrication."""
    b = compute_breadth([_row(dist_from_52w_high_pct=1.0)] * 3, sample="universe")
    assert b["high_low_window"] == "52w"
    assert "NOT literal" in b["high_low_note"]


def test_empty_sample_is_empty_not_an_error():
    b = compute_breadth([], sample="universe")
    assert b["status"] == "empty" and b["n_names"] == 0


def test_zero_decliners_does_not_divide_by_zero():
    b = compute_breadth([_row(pct_change_1d=2.0)] * 4, sample="universe")
    assert b["advance_decline_ratio"] is None and b["advancers"] == 4


# ---------------------------------------------------------------------------
# sector history
# ---------------------------------------------------------------------------

@pytest.fixture
def hist(tmp_path, monkeypatch):
    monkeypatch.setattr(SH, "HISTORY_PATH", tmp_path / "sector_momentum_history.jsonl")
    return SH


def _srs(**kw) -> list:
    out = []
    for sector, m1 in kw.items():
        out.append({"sector": sector, "avg_momentum_1m_pct": m1,
                    "avg_momentum_3m_pct": 20.0, "names": 5})
    return out


def test_snapshot_is_persisted(hist):
    res = hist.record_sector_momentum(_srs(gpu=5.0, cyber=10.0), as_of="2026-07-19")
    assert res["written"] == 2
    assert len(hist.load_history()) == 2


def test_one_row_per_sector_per_day(hist):
    """Seven runs a day must not weight one Tuesday seven times in a later study."""
    hist.record_sector_momentum(_srs(gpu=5.0), as_of="2026-07-19")
    res = hist.record_sector_momentum(_srs(gpu=9.9), as_of="2026-07-19")
    assert res["written"] == 0 and res["skipped_existing"] == 1
    assert len(hist.load_history()) == 1


def test_trend_needs_more_than_one_observation(hist):
    """One observation is a LEVEL, not a trend."""
    hist.record_sector_momentum(_srs(gpu=5.0), as_of="2026-07-19")
    t = hist.sector_trends()
    assert t["sectors"] == {}
    assert "gpu" in t["insufficient_history"]


def test_rotation_is_visible_across_days(hist):
    """THE POINT: the AI-unwind observation becomes queryable instead of prose."""
    hist.record_sector_momentum(_srs(gpu=30.0, cyber=5.0), as_of="2026-07-01")
    hist.record_sector_momentum(_srs(gpu=-10.0, cyber=21.0), as_of="2026-07-19")
    t = hist.sector_trends()
    assert t["sectors"]["gpu"]["momentum_1m_change_pp"] == -40.0
    assert t["sectors"]["cyber"]["momentum_1m_change_pp"] == 16.0
    assert t["accelerating"][0]["sector"] == "cyber"
    assert t["decelerating"][0]["sector"] == "gpu"


def test_deltas_are_computed_on_read_not_stored(hist):
    """A stored delta rots the moment history is backfilled or corrected."""
    hist.record_sector_momentum(_srs(gpu=10.0), as_of="2026-07-19")
    raw = hist.HISTORY_PATH.read_text()
    assert "change_pp" not in raw and "delta" not in raw


def test_corrupt_line_is_skipped_not_fatal(hist):
    hist.record_sector_momentum(_srs(gpu=5.0), as_of="2026-07-19")
    with hist.HISTORY_PATH.open("a") as f:
        f.write("{ this is not json\n")
    hist.record_sector_momentum(_srs(gpu=6.0), as_of="2026-07-20")
    assert len(hist.load_history()) == 2


def test_write_failure_never_raises(hist, monkeypatch):
    """A history write must not be able to take down a trading run."""
    monkeypatch.setattr(hist, "HISTORY_PATH", hist.ROOT / "no" / "such" / "dir" / "x.jsonl")
    monkeypatch.setattr(hist.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("readonly")))
    res = hist.record_sector_momentum(_srs(gpu=5.0), as_of="2026-07-19")
    assert res["status"].startswith("error")


def test_empty_history_says_so(hist):
    assert hist.sector_trends()["status"] == "no_history"


# ---------------------------------------------------------------------------
# both must actually reach the brain
# ---------------------------------------------------------------------------

def test_breadth_is_wired_and_inside_the_read_window():
    """momentum_health was gathered, used by the validator, and stripped from the
    pack because it was in neither key list. Breadth must not repeat that."""
    from runlib.context_tiers import (ALWAYS_KEYS, DECISION_CONTEXT,
                                      READ_WINDOW_LINES, key_start_lines,
                                      slim_context_for_brain)
    assert "market_breadth" in ALWAYS_KEYS
    assert "market_breadth" in DECISION_CONTEXT, (
        "breadth is a step-1 regime input and must ride with the regime read")
    pack = slim_context_for_brain({
        "market_breadth": {"universe": {"pct_above_200dma": 41.0}},
        "reasoning_process": {}, "portfolio": {},
    })
    assert pack["market_breadth"]["universe"]["pct_above_200dma"] == 41.0
    assert key_start_lines(pack)["market_breadth"] <= READ_WINDOW_LINES


def test_scanner_and_discovery_both_emit_breadth():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    scanner = (root / "tools" / "universe_scanner.py").read_text()
    disc = (root / "tools" / "discovery_screen.py").read_text()
    assert "compute_breadth" in scanner and '"breadth": breadth' in scanner
    assert "compute_breadth" in disc
    assert "record_sector_momentum" in scanner, (
        "the sector snapshot is not persisted, so rotation stays un-queryable")
