"""Second-layer technicals: weekly timeframe, anchored VWAP, sector RS, retention.

FOUR GAPS FOUND 2026-07-19, all cases of the brain being asked to eyeball a PNG
instead of being handed a number:

  1. Everything was DAILY. Weekly bars were downloaded for the 200-week MA and
     then discarded — while a 3-90 day swing lives on the weekly base.
  2. No NUMERIC support. "Support at $345-352" came from the model's eyes;
     nothing auditable or backtestable.
  3. Relative strength only vs SPY, never vs SECTOR. A semi flat while semis are
     -12% is a different name from one merely matching SPY.
  4. ~38 indicators computed for ~184 names, RETAINED for ~35. The other ~149
     kept a price and an ATR — including watchlist names the brain must rule
     drop/hold/buy every single run.

Every helper is pure and must return None rather than a fabricated default when
the bars cannot support the calculation.
"""

from __future__ import annotations

import math

import pytest

from tools.technicals import (
    anchored_vwap, anchored_vwap_block, attach_sector_relative_strength,
    build_indicator_map, compact_indicator_line, find_anchor_index,
    sector_medians, weekly_structure, wilder_rsi,
)


# ---------------------------------------------------------------------------
# 1. weekly timeframe
# ---------------------------------------------------------------------------

def _weekly(n=60, slope=0.8):
    c = [100 + i * slope + 3 * math.sin(i / 3) for i in range(n)]
    return c, [x * 1.02 for x in c], [x * 0.98 for x in c]


def test_weekly_structure_reads_an_uptrend():
    c, h, l = _weekly()
    w = weekly_structure(c, h, l)
    assert w["above_30w_ma"] is True
    assert w["wma_30w_rising"] is True
    assert w["weekly_structure"] == "uptrend"
    assert 0 <= w["weekly_rsi_14"] <= 100


def test_weekly_structure_reads_a_downtrend():
    # Clean monotonic decline: with sine noise the 4-week high/low blocks are not
    # strictly lower and the honest read is "mixed", which the noisy fixture below
    # covers. A structure call must need real lower-highs AND lower-lows.
    c = [100 - i for i in range(60)]
    w = weekly_structure(c, [x * 1.02 for x in c], [x * 0.98 for x in c])
    assert w["above_30w_ma"] is False
    assert w["weekly_structure"] == "downtrend"


def test_choppy_decline_is_mixed_not_forced_into_a_trend():
    c, h, l = _weekly(slope=-0.8)
    w = weekly_structure(c, h, l)
    assert w["above_30w_ma"] is False
    assert w["weekly_structure"] == "mixed"


def test_short_weekly_history_does_not_fabricate_a_30w_ma():
    w = weekly_structure([100.0] * 10)
    assert w["wma_30w"] is None
    assert w["insufficient_for_30w_ma"] is True


def test_no_weekly_bars_is_stated_not_guessed():
    assert weekly_structure([])["status"] == "no_weekly_bars"


def test_weekly_rsi_matches_wilder_bounds():
    assert wilder_rsi([100 + i for i in range(40)]) > 90     # relentless up
    assert wilder_rsi([100 - i for i in range(40)]) < 10     # relentless down
    assert wilder_rsi([100.0] * 5) is None                   # too short


# ---------------------------------------------------------------------------
# 2. anchored VWAP — the first numeric support level this scanner produces
# ---------------------------------------------------------------------------

def _breakout_series():
    closes = [100.0] * 40 + [110.0 + i for i in range(10)]
    return (closes, [c * 1.01 for c in closes], [c * 0.99 for c in closes],
            [1e6] * 40 + [5e6] * 10)

def test_anchor_finds_the_breakout_bar():
    c, h, l, v = _breakout_series()
    idx, reason = find_anchor_index(c, h, v)
    assert idx == 40 and reason == "breakout_above_20d_high"


def test_anchored_vwap_is_the_volume_weighted_price_since_the_anchor():
    c, h, l, v = _breakout_series()
    b = anchored_vwap_block(c, h, l, v)
    assert b["status"] == "ok"
    assert b["sessions_since_anchor"] == 9
    # Bars 40-49 run 110..119 with equal volume -> ~114.5
    assert b["anchored_vwap"] == pytest.approx(114.5, abs=0.5)
    assert b["above_anchored_vwap"] is True
    assert b["pct_vs_anchored_vwap"] > 0


def test_price_below_anchored_vwap_is_reported_as_such():
    closes = [100.0] * 40 + [120.0] + [108.0] * 9      # breakout then fade
    v = [1e6] * 40 + [5e6] + [1e6] * 9
    b = anchored_vwap_block(closes, [c * 1.01 for c in closes],
                            [c * 0.99 for c in closes], v)
    assert b["above_anchored_vwap"] is False
    assert b["pct_vs_anchored_vwap"] < 0


def test_falls_back_to_the_highest_volume_session():
    """No breakout: anchor where institutions most likely repositioned."""
    closes = [100.0] * 60
    v = [1e6] * 30 + [9e6] + [1e6] * 29
    idx, reason = find_anchor_index(closes, [c * 1.01 for c in closes], v)
    assert reason == "highest_volume_session" and idx == 30


def test_unavailable_says_why():
    b = anchored_vwap_block([100.0] * 5, [101.0] * 5, [99.0] * 5, [1e6] * 5)
    assert b["status"] == "unavailable" and b["reason"] == "insufficient_history"


def test_zero_volume_window_does_not_divide_by_zero():
    c, h, l, _ = _breakout_series()
    assert anchored_vwap(c, h, l, [0.0] * len(c), 40) is None


# ---------------------------------------------------------------------------
# 3. relative strength vs SECTOR
# ---------------------------------------------------------------------------

def _rows():
    return [
        {"ticker": "A", "sector": "semis", "momentum_1m_pct": -5, "momentum_3m_pct": 10},
        {"ticker": "B", "sector": "semis", "momentum_1m_pct": -20, "momentum_3m_pct": 5},
        {"ticker": "C", "sector": "semis", "momentum_1m_pct": -18, "momentum_3m_pct": 4},
        {"ticker": "D", "sector": "solo", "momentum_1m_pct": 3, "momentum_3m_pct": 3},
    ]


def test_median_not_mean_defines_the_sector():
    """One +300% name must not redefine 'keeping up with the group'."""
    rows = _rows() + [{"ticker": "X", "sector": "semis",
                       "momentum_1m_pct": 300, "momentum_3m_pct": 300}]
    med = sector_medians(rows)["semis"]
    vals = [-5, -20, -18, 300]
    assert med["median_momentum_1m_pct"] == -11.5      # median of the four
    assert med["median_momentum_1m_pct"] < 0, (
        f"the mean here is {sum(vals)/len(vals):+.1f}% — a mean would call this "
        f"sector strongly positive on the back of one name")


def test_laggard_in_a_weak_sector_is_identified_as_relatively_strong():
    """THE POINT: A is -5% (bad vs SPY) but its sector median is -18%."""
    rows = _rows()
    attach_sector_relative_strength(rows)
    assert rows[0]["rel_strength_vs_sector_1m_pct"] == 13.0
    assert rows[0]["sector_peers_scored"] == 3


def test_thin_sector_is_skipped_not_compared_to_itself():
    rows = _rows()
    meta = attach_sector_relative_strength(rows)
    assert "rel_strength_vs_sector_1m_pct" not in rows[3]
    assert meta["skipped_thin_sector"] == 1


def test_missing_momentum_does_not_produce_a_number():
    rows = [{"ticker": "A", "sector": "s", "momentum_1m_pct": None},
            {"ticker": "B", "sector": "s", "momentum_1m_pct": 1},
            {"ticker": "C", "sector": "s", "momentum_1m_pct": 2}]
    attach_sector_relative_strength(rows)
    assert "rel_strength_vs_sector_1m_pct" not in rows[0]


# ---------------------------------------------------------------------------
# 4. retention for EVERY scanned name
# ---------------------------------------------------------------------------

def test_every_scanned_name_keeps_a_line():
    """~149 of 184 names had their indicators computed and thrown away."""
    m = build_indicator_map(_rows())
    assert set(m) == {"A", "B", "C", "D"}


def test_compact_line_carries_the_judgement_fields():
    row = {"ticker": "A", "sector": "semis", "last_close": 100.0, "rsi_14": 55,
           "adx_14": 30, "above_200dma": True, "trend_up_50_over_200": True,
           "rel_strength_1m_pct": 4.0, "rel_strength_vs_sector_1m_pct": 12.0,
           "macd": {"state": "above_zero", "hist": 1.0},
           "volume_signal": {"read": "pocket_pivot", "rvol": 2.0},
           "weekly": {"above_30w_ma": True, "weekly_rsi_14": 61.0,
                      "weekly_structure": "uptrend"},
           "anchored_vwap": {"status": "ok", "pct_vs_anchored_vwap": 3.2}}
    line = compact_indicator_line(row)
    for k in ("rsi_14", "adx_14", "above_200dma", "macd_state", "volume_read",
              "above_30w_ma", "weekly_rsi_14", "weekly_structure",
              "pct_vs_anchored_vwap", "rel_strength_vs_sector_1m_pct"):
        assert k in line, f"{k} missing from the retained line"
    assert "rvol" not in line, "the compact line must stay compact"


def test_unavailable_anchored_vwap_is_omitted_not_faked():
    line = compact_indicator_line(
        {"ticker": "A", "anchored_vwap": {"status": "unavailable", "reason": "x"}})
    assert "pct_vs_anchored_vwap" not in line


def test_scanner_emits_all_four():
    """A helper nobody wires in is decoration."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "tools" / "universe_scanner.py").read_text()
    assert "weekly_structure" in src                      # 1
    assert "anchored_vwap_block" in src                   # 2
    assert "attach_sector_relative_strength" in src       # 3
    assert "build_indicator_map" in src                   # 4
    assert '"indicators_by_ticker": indicator_map' in src
