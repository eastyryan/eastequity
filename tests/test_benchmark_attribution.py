"""Benchmark attribution — the numbers that separate skill from AI beta.

Before this, the entire performance surface was absolute P&L, and win_rate_pct was
not cosmetic: calibration_gate flags a sector "losing" on it and then demands an
exception paragraph to trade it. In a bull tape no bucket ever trips; in a drawdown
they all trip at once.

These tests pin the math AND the honesty guards — a ratio quoted off two weeks of
data would be worse than no ratio at all.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import benchmark as bm  # noqa: E402


def series(pairs):
    """[(date, equity, bench)] -> equity_history rows."""
    return [{"date": d, "equity": e, "cash": 0.0, "benchmark_close": b}
            for d, e, b in pairs]


# --------------------------------------------------------------------------- #
# Return series construction
# --------------------------------------------------------------------------- #
def test_pct_return_guards():
    assert bm.pct_return(100, 110) == pytest.approx(0.10)
    assert bm.pct_return(0, 110) is None       # division guard
    assert bm.pct_return(None, 110) is None
    assert bm.pct_return(100, None) is None
    assert bm.pct_return("x", 110) is None


def test_daily_returns_skip_rows_without_a_benchmark_close():
    """equity_history.json contains WEEKEND rows and rows where benchmark_close is
    an ABSENT KEY. Counting them adds artificial zero-return days, which deflates
    volatility and inflates every ratio derived from it."""
    hist = [
        {"date": "2026-07-09", "equity": 10000.0, "benchmark_close": 500.0},
        {"date": "2026-07-10", "equity": 10100.0, "benchmark_close": 505.0},
        {"date": "2026-07-11", "equity": 10100.0},          # weekend: key absent
        {"date": "2026-07-12", "equity": 10100.0},          # weekend
        {"date": "2026-07-13", "equity": 10200.0, "benchmark_close": 510.0},
    ]
    rets = bm.daily_returns(hist)
    dates = [d for d, _ in rets]
    assert "2026-07-11" not in dates and "2026-07-12" not in dates
    assert len(rets) == 2


def test_daily_returns_are_sorted_and_pairwise():
    hist = series([("2026-07-03", 100.0, 10.0), ("2026-07-01", 90.0, 9.0),
                   ("2026-07-02", 99.0, 9.9)])
    rets = bm.daily_returns(hist)
    assert [d for d, _ in rets] == ["2026-07-02", "2026-07-03"]
    assert rets[0][1] == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# Ratios refuse to speak on thin samples
# --------------------------------------------------------------------------- #
def test_ratios_are_none_below_the_observation_floor():
    """THE honesty guard. A Sharpe off a fortnight is a number without information,
    and it would render on the dashboard identically to a real one."""
    short = [(f"2026-07-{i:02d}", 0.01) for i in range(1, 20)]
    assert len(short) < bm.MIN_OBS
    assert bm.sharpe(short) is None
    assert bm.sortino(short) is None


def test_sharpe_computes_once_the_sample_is_sufficient():
    rets = [(f"d{i}", 0.001 if i % 2 else 0.003) for i in range(bm.MIN_OBS + 20)]
    s = bm.sharpe(rets)
    assert s is not None and s > 0


def test_sortino_ignores_upside_volatility():
    """A book whose volatility is mostly UPSIDE should score better on Sortino than
    on Sharpe — Sharpe punishes both tails, Sortino only the one that hurts."""
    # Big upside jumps, small consistent downside: same mean, very different shape.
    mixed = [(f"d{i}", 0.05 if i % 5 == 0 else -0.002)
             for i in range(bm.MIN_OBS + 20)]
    assert bm.sortino(mixed) > bm.sharpe(mixed)


def test_sortino_is_none_with_no_downside_at_all():
    """Zero downside deviation makes the ratio undefined — None, never infinity."""
    up_only = [(f"d{i}", 0.001 if i % 5 else 0.05) for i in range(bm.MIN_OBS + 20)]
    assert bm.sortino(up_only) is None


def test_zero_volatility_returns_none_not_infinity():
    flat = [(f"d{i}", 0.0) for i in range(bm.MIN_OBS + 10)]
    assert bm.sharpe(flat) is None
    assert bm.sortino(flat) is None


# --------------------------------------------------------------------------- #
# Beta and alpha
# --------------------------------------------------------------------------- #
def test_beta_of_a_book_that_tracks_the_benchmark_is_one():
    book = [(f"d{i}", 0.01 if i % 3 else -0.02) for i in range(bm.MIN_OBS + 20)]
    assert bm.beta(book, book) == pytest.approx(1.0, abs=0.01)


def test_beta_of_a_double_levered_book_is_two():
    bench = [(f"d{i}", 0.01 if i % 3 else -0.02) for i in range(bm.MIN_OBS + 20)]
    book = [(d, r * 2) for d, r in bench]
    assert bm.beta(book, bench) == pytest.approx(2.0, abs=0.01)


def test_beta_aligns_on_dates_not_position():
    """Zipping unaligned series pairs a Monday book return with a Tuesday benchmark
    return — silently wrong, and the two series DO differ whenever a row lacks a
    benchmark_close."""
    bench = [(f"d{i:03d}", 0.01) for i in range(bm.MIN_OBS + 20)]
    book = [(d, r) for d, r in bench if d != "d005"]     # one missing day
    assert bm.beta(book, bench) is not None


def test_beta_is_none_on_a_thin_sample():
    short = [(f"d{i}", 0.01) for i in range(10)]
    assert bm.beta(short, short) is None


def test_jensens_alpha_is_zero_for_a_pure_beta_book():
    """A book that IS the benchmark earned no alpha — the exact claim this whole
    module exists to test."""
    bench = [(f"d{i}", 0.004 if i % 4 else -0.006) for i in range(bm.MIN_OBS + 40)]
    assert bm.jensens_alpha(bench, bench) == pytest.approx(0.0, abs=0.01)


def test_jensens_alpha_positive_when_the_book_adds_return_beyond_beta():
    bench = [(f"d{i}", 0.004 if i % 4 else -0.006) for i in range(bm.MIN_OBS + 40)]
    book = [(d, r + 0.001) for d, r in bench]           # +10bps/day of pure alpha
    assert bm.jensens_alpha(book, bench) > 0


# --------------------------------------------------------------------------- #
# Matched-window trade attribution
# --------------------------------------------------------------------------- #
CLOSES = {"2026-07-01": 500.0, "2026-07-02": 505.0, "2026-07-06": 510.0,
          "2026-07-07": 515.0}


def test_matched_window_uses_the_trades_own_dates():
    r = bm.matched_window_return("2026-07-01", "2026-07-07", CLOSES)
    assert r == pytest.approx(515.0 / 500.0 - 1)


def test_matched_window_falls_back_to_the_prior_close_on_a_holiday():
    """A trade can open on a day the benchmark has no bar."""
    r = bm.matched_window_return("2026-07-04", "2026-07-07", CLOSES)
    assert r == pytest.approx(515.0 / 505.0 - 1)   # 07-02 is the last close on/before


def test_same_day_round_trip_is_none_not_zero():
    """Day-granular dates give a same-day trade no measurable window. Zero would
    read as 'matched the benchmark', which is a different claim from 'unknown'."""
    assert bm.matched_window_return("2026-07-01", "2026-07-01", CLOSES) is None


def test_missing_dates_are_none():
    assert bm.matched_window_return(None, "2026-07-07", CLOSES) is None
    assert bm.matched_window_return("2026-07-01", None, CLOSES) is None
    assert bm.matched_window_return("2026-07-01", "2026-07-07", {}) is None


def test_attach_stamps_excess_and_leaves_unknowables_none(monkeypatch):
    monkeypatch.setattr(bm, "benchmark_closes", lambda *a, **k: CLOSES)
    closed = [
        {"ticker": "AAA", "entry_price": 100.0, "exit_price": 110.0,
         "opened_at": "2026-07-01", "closed_at": "2026-07-07"},
        {"ticker": "BBB", "entry_price": 50.0, "exit_price": 49.0,
         "opened_at": "2026-07-01", "closed_at": "2026-07-01"},   # same day
    ]
    bm.attach_benchmark_to_closed(closed)

    a, b = closed
    assert a["trade_return_pct"] == pytest.approx(10.0)
    assert a["benchmark_return_pct"] == pytest.approx(3.0, abs=0.01)
    assert a["excess_return_pct"] == pytest.approx(7.0, abs=0.01)
    assert b["excess_return_pct"] is None, "unknowable must not become 0"


def test_a_winning_trade_can_still_lose_to_the_benchmark(monkeypatch):
    """The whole point: +8% while SPY did +9% is a loss of alpha, and absolute
    win_rate_pct would have scored it a win."""
    monkeypatch.setattr(bm, "benchmark_closes",
                        lambda *a, **k: {"2026-07-01": 100.0, "2026-07-07": 109.0})
    closed = [{"ticker": "AAA", "entry_price": 100.0, "exit_price": 108.0,
               "opened_at": "2026-07-01", "closed_at": "2026-07-07"}]
    bm.attach_benchmark_to_closed(closed)
    assert closed[0]["trade_return_pct"] > 0
    assert closed[0]["excess_return_pct"] < 0

    rollup = bm.summarize_excess(closed)
    assert rollup["beat_benchmark_rate_pct"] == 0.0


def test_summarize_excess_reports_unscored_count():
    closed = [{"excess_return_pct": 5.0}, {"excess_return_pct": -3.0},
              {"excess_return_pct": None}]
    s = bm.summarize_excess(closed)
    assert s["n_scored"] == 2 and s["n_unscored"] == 1
    assert s["mean_excess_return_pct"] == pytest.approx(1.0)
    assert s["beat_benchmark_rate_pct"] == 50.0


def test_summarize_excess_handles_nothing_scored():
    assert bm.summarize_excess([])["n_scored"] == 0
    assert bm.summarize_excess([{"excess_return_pct": None}])["n_scored"] == 0


# --------------------------------------------------------------------------- #
# The rollup carries its own sample size
# --------------------------------------------------------------------------- #
def test_risk_metrics_disclose_sample_size():
    """'Sharpe 2.1' off 14 days and off 3 years must not render identically."""
    hist = series([(f"2026-07-{i:02d}", 10000.0 + i * 10, 500.0 + i)
                   for i in range(1, 8)])
    m = bm.book_risk_metrics(hist)
    assert m["n_observations"] == 6
    assert m["sufficient_sample"] is False
    assert m["sharpe"] is None and m["beta_vs_benchmark"] is None
    assert str(bm.MIN_OBS) in m["note"]
