"""Offline tests for tools/momentum_health.py — the momentum-factor unwind gauge.

Everything here is pure/offline: network fetch is monkeypatched out and the
cache file is pointed at tmp_path, so the suite never touches yfinance or the
committed data/cache/momentum_etfs.json.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.momentum_health as mh


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_rows(n=20, leader_1m=5.0, leader_3m=40.0, rest_1m=1.0, rest_3m=5.0,
              n_leaders=3):
    """Synthetic scan rows: `n_leaders` clear 3-month leaders + the pack."""
    rows = []
    for i in range(n_leaders):
        rows.append({"ticker": f"LDR{i}", "rel_strength_3m_pct": leader_3m - i,
                     "rel_strength_1m_pct": leader_1m})
    for i in range(n - n_leaders):
        rows.append({"ticker": f"PCK{i}", "rel_strength_3m_pct": rest_3m,
                     "rel_strength_1m_pct": rest_1m})
    return rows


def flat_closes(price=100.0, n=40):
    return [price] * n


def closes_with_drop(pre=100.0, post=None, n=40, drop_sessions=2):
    """Flat at `pre`, dropping to `post` for the last `drop_sessions` bars —
    a fresh, sharp break (drawdown from the 20d high = post/pre - 1)."""
    return [pre] * (n - drop_sessions) + [post] * drop_sessions


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No network, isolated cache file, for every test in this module."""
    monkeypatch.setattr(mh, "_fetch_etf_closes", lambda: {})
    monkeypatch.setattr(mh, "CACHE_FILE", tmp_path / "momentum_etfs.json")


def write_cache(path, closes, age_days=0.0):
    as_of = datetime.now(timezone.utc) - timedelta(days=age_days)
    path.write_text(json.dumps({"as_of": as_of.isoformat(), "closes": closes}))


# --------------------------------------------------------------------------- #
# Internal layer: leader-decile math
# --------------------------------------------------------------------------- #
def test_internal_leaders_lagging_hard_is_unwind():
    rows = make_rows(n=20, leader_1m=-8.0)   # 3m leaders now badly lagging 1m
    layer = mh._internal_layer(rows)
    assert layer is not None
    assert layer["status"] == "unwind"
    assert layer["n_rows"] == 20
    # decile of 20 rows floors to 2, but MIN_LEADERS=3 wins
    assert layer["n_leaders"] == 3
    assert layer["leaders"] == ["LDR0", "LDR1", "LDR2"]
    assert layer["avg_leader_1m_rs_pct"] == -8.0


def test_internal_leaders_still_leading_is_healthy():
    layer = mh._internal_layer(make_rows(n=20, leader_1m=6.0))
    assert layer["status"] == "healthy"


def test_internal_picks_top_decile_by_3m_not_row_order():
    # Leaders appended LAST; pack has better 1m — ranking must be by 3m RS.
    rows = make_rows(n=30, leader_1m=-9.0, rest_1m=4.0)
    rows.reverse()
    layer = mh._internal_layer(rows)
    assert set(layer["leaders"]) == {"LDR0", "LDR1", "LDR2"}
    assert layer["status"] == "unwind"


def test_internal_decile_scales_with_row_count():
    # 40 usable rows -> 4 leaders (40 // 10), not the MIN_LEADERS floor.
    rows = make_rows(n=40, leader_1m=-8.0, n_leaders=4)
    layer = mh._internal_layer(rows)
    assert layer["n_leaders"] == 4
    assert layer["status"] == "unwind"


def test_internal_threshold_boundaries():
    # exactly -5.0 -> unwind (inclusive)
    assert mh._internal_layer(make_rows(leader_1m=-5.0))["status"] == "unwind"
    # just above -5 -> softening
    assert mh._internal_layer(make_rows(leader_1m=-4.9))["status"] == "softening"
    # exactly -2.0 -> softening (inclusive)
    assert mh._internal_layer(make_rows(leader_1m=-2.0))["status"] == "softening"
    # just above -2 -> healthy
    assert mh._internal_layer(make_rows(leader_1m=-1.9))["status"] == "healthy"


def test_internal_too_few_rows_returns_none():
    assert mh._internal_layer(make_rows(n=mh.MIN_INTERNAL_ROWS - 1)) is None
    assert mh._internal_layer([]) is None
    assert mh._internal_layer(None) is None


def test_internal_ignores_junk_rows_and_duplicates():
    rows = make_rows(n=20, leader_1m=-8.0)
    rows += [{"ticker": "LDR0", "rel_strength_3m_pct": 40.0,      # dup leader
              "rel_strength_1m_pct": -8.0},
             {"ticker": "NAN", "rel_strength_3m_pct": float("nan"),
              "rel_strength_1m_pct": 1.0},
             {"ticker": "NULL", "rel_strength_3m_pct": None,
              "rel_strength_1m_pct": 1.0},
             {"ticker": "BOOL", "rel_strength_3m_pct": True,
              "rel_strength_1m_pct": 1.0},
             "not-a-dict", None]
    layer = mh._internal_layer(rows)
    assert layer["n_rows"] == 20                     # junk + dup not counted
    assert layer["status"] == "unwind"


# --------------------------------------------------------------------------- #
# External layer: ETF drawdown-vs-SPY math (pure, synthetic closes)
# --------------------------------------------------------------------------- #
def test_etf_deep_dd_while_spy_flat_is_unwind():
    layer = mh._etf_layer({
        "SPY": flat_closes(),
        "MTUM": closes_with_drop(100.0, 91.0),       # -9% off 20d high, SPY 0
        "SPMO": flat_closes(),
    })
    assert layer["status"] == "unwind"
    assert layer["etfs"]["MTUM"]["status"] == "unwind"
    assert layer["etfs"]["MTUM"]["drawdown_20d_pct"] == -9.0
    assert layer["etfs"]["SPMO"]["status"] == "healthy"
    assert layer["spy"]["drawdown_20d_pct"] == 0.0


def test_etf_moderate_dd_is_softening():
    layer = mh._etf_layer({
        "SPY": flat_closes(),
        "MTUM": closes_with_drop(100.0, 94.5),       # -5.5%, gap -5.5
    })
    assert layer["status"] == "softening"


def test_etf_boundaries_exact():
    # dd exactly -8 with SPY flat -> unwind (inclusive)
    layer = mh._etf_layer({"SPY": flat_closes(),
                           "MTUM": closes_with_drop(100.0, 92.0)})
    assert layer["status"] == "unwind"
    # dd exactly -5 -> softening (inclusive)
    layer = mh._etf_layer({"SPY": flat_closes(),
                           "MTUM": closes_with_drop(100.0, 95.0)})
    assert layer["status"] == "softening"
    # dd -4.9 -> healthy
    layer = mh._etf_layer({"SPY": flat_closes(),
                           "MTUM": closes_with_drop(100.0, 95.1)})
    assert layer["status"] == "healthy"


def test_etf_spy_falling_too_is_not_an_unwind():
    # Both down ~9%: a market selloff, not a factor unwind (gap ~0 > -4).
    layer = mh._etf_layer({
        "SPY": closes_with_drop(100.0, 91.0),
        "MTUM": closes_with_drop(100.0, 91.0),
    })
    assert layer["status"] == "healthy"
    assert layer["etfs"]["MTUM"]["dd_gap_vs_spy_pct"] == 0.0


def test_etf_gap_boundary_softening_when_spy_partially_down():
    # MTUM -9, SPY -7 -> gap -2: deep dd but not 4pts deeper -> softening.
    layer = mh._etf_layer({
        "SPY": closes_with_drop(100.0, 93.0),
        "MTUM": closes_with_drop(100.0, 91.0),
    })
    assert layer["etfs"]["MTUM"]["status"] == "softening"


def test_etf_uses_worse_of_mtum_spmo():
    layer = mh._etf_layer({
        "SPY": flat_closes(),
        "MTUM": flat_closes(),                        # healthy
        "SPMO": closes_with_drop(100.0, 90.0),        # unwind
    })
    assert layer["status"] == "unwind"


def test_etf_layer_needs_spy_and_enough_history():
    assert mh._etf_layer({"MTUM": closes_with_drop(100.0, 90.0)}) is None
    assert mh._etf_layer({"SPY": flat_closes(n=10),
                          "MTUM": flat_closes(n=10)}) is None
    assert mh._etf_layer({}) is None
    assert mh._etf_layer(None) is None
    # SPY present but no ETF data at all -> None
    assert mh._etf_layer({"SPY": flat_closes()}) is None


def test_etf_10d_excess_return_reported():
    layer = mh._etf_layer({
        "SPY": flat_closes(),
        "MTUM": closes_with_drop(100.0, 91.0),
    })
    row = layer["etfs"]["MTUM"]
    assert row["ret_10d_pct"] == -9.0
    assert row["excess_ret_10d_vs_spy_pct"] == -9.0


# --------------------------------------------------------------------------- #
# Cache staleness
# --------------------------------------------------------------------------- #
def test_cache_closes_fresh_vs_stale():
    now = datetime.now(timezone.utc)
    closes = {"SPY": flat_closes(), "MTUM": flat_closes()}
    fresh = {"as_of": (now - timedelta(days=2)).isoformat(), "closes": closes}
    stale = {"as_of": (now - timedelta(days=6)).isoformat(), "closes": closes}
    assert mh._cache_closes(fresh, now=now) == closes
    assert mh._cache_closes(stale, now=now) is None
    assert mh._cache_closes({"closes": closes}, now=now) is None   # no as_of
    assert mh._cache_closes({"as_of": now.isoformat()}, now=now) is None
    assert mh._cache_closes({}, now=now) is None
    assert mh._cache_closes(None, now=now) is None


def test_fetch_failure_falls_back_to_fresh_cache(monkeypatch):
    write_cache(mh.CACHE_FILE, {"SPY": flat_closes(),
                                "MTUM": closes_with_drop(100.0, 90.0)},
                age_days=1.0)
    out = mh.get_momentum_health(None)
    assert out["status"] == "unwind"
    assert out["momentum_unwind"] is True
    assert out["signals"]["external_etfs"]["source"] == "cache"


def test_stale_cache_is_ignored_entirely():
    write_cache(mh.CACHE_FILE, {"SPY": flat_closes(),
                                "MTUM": closes_with_drop(100.0, 90.0)},
                age_days=6.0)
    out = mh.get_momentum_health(None)
    assert out["status"] == "unknown"          # unwind-shaped data, but stale
    assert out["momentum_unwind"] is False


def test_successful_fetch_writes_cache(monkeypatch):
    fetched = {"SPY": flat_closes(), "MTUM": flat_closes(),
               "SPMO": flat_closes()}
    monkeypatch.setattr(mh, "_fetch_etf_closes", lambda: fetched)
    out = mh.get_momentum_health(None)
    assert out["status"] == "healthy"
    assert out["signals"]["external_etfs"]["source"] == "live"
    blob = json.loads(mh.CACHE_FILE.read_text())
    assert blob["closes"] == fetched
    assert "as_of" in blob


# --------------------------------------------------------------------------- #
# Combination + contract shape
# --------------------------------------------------------------------------- #
def test_unknown_fail_open_when_no_data_at_all():
    out = mh.get_momentum_health(None)
    assert out["status"] == "unknown"
    assert out["momentum_unwind"] is False
    assert out["signals"] == {"internal_leaders": None, "external_etfs": None}
    assert isinstance(out["note"], str) and out["note"]


def test_contract_shape_and_types():
    out = mh.get_momentum_health(make_rows(n=20, leader_1m=-8.0))
    assert set(out.keys()) == {"status", "momentum_unwind", "signals",
                               "as_of", "note"}
    assert out["status"] in ("healthy", "softening", "unwind", "unknown")
    assert isinstance(out["momentum_unwind"], bool)
    assert isinstance(out["signals"], dict)
    assert isinstance(out["note"], str)
    # as_of parses as an aware UTC timestamp
    parsed = datetime.fromisoformat(out["as_of"])
    assert parsed.tzinfo is not None


def test_momentum_unwind_true_only_for_unwind_status():
    assert mh.get_momentum_health(make_rows(leader_1m=-8.0))["momentum_unwind"] is True
    for onem in (-4.0, 3.0):
        out = mh.get_momentum_health(make_rows(leader_1m=onem))
        assert out["momentum_unwind"] is False
    assert mh.get_momentum_health(None)["momentum_unwind"] is False


def test_either_layer_unwind_wins():
    # internal healthy, external (via cache) unwind -> unwind
    write_cache(mh.CACHE_FILE, {"SPY": flat_closes(),
                                "SPMO": closes_with_drop(100.0, 90.0)},
                age_days=0.5)
    out = mh.get_momentum_health(make_rows(leader_1m=5.0))
    assert out["signals"]["internal_leaders"]["status"] == "healthy"
    assert out["status"] == "unwind"


def test_softening_when_either_layer_softens():
    out = mh.get_momentum_health(make_rows(leader_1m=-3.0))
    assert out["status"] == "softening"
    assert out["momentum_unwind"] is False


def test_healthy_when_data_present_and_calm():
    out = mh.get_momentum_health(make_rows(leader_1m=4.0))
    assert out["status"] == "healthy"


def test_never_raises_on_garbage_input():
    for garbage in ([{"ticker": None}], [42], {"not": "a list"}, "rows", 3.14):
        out = mh.get_momentum_health(garbage)
        assert out["status"] == "unknown"
        assert out["momentum_unwind"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
