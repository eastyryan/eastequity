"""Point-in-time universe membership must be reconstructable, and drift must be loud.

data/universe.json is a LIVE POINTER with no history — git spans ~9 days and the
weekly review rewrites it wholesale, computing removals as a set difference from a
model-supplied replacement map. Names that leave vanish without trace, and the
measured removals (INTC, PYPL, MDB, ON, NXPI, MCHP, IREN, APLD, COIN, MU, NBIS) are
the laggard cohort of the AI trade, while additions come from names the radar
surfaced BECAUSE they already moved. That is survivorship bias being manufactured
weekly, invisible to every downstream measurement.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import universe_history as uh  # noqa: E402


@pytest.fixture
def log(tmp_path, monkeypatch):
    f = tmp_path / "universe_history.jsonl"
    monkeypatch.setattr(uh, "HISTORY_FILE", f)
    monkeypatch.setattr(uh, "UNIVERSE_FILE", tmp_path / "universe.json")
    return f


def write_universe(tmp_path, sectors):
    (tmp_path / "universe.json").write_text(json.dumps({"sectors": sectors}))


# --------------------------------------------------------------------------- #
# Reconstruction
# --------------------------------------------------------------------------- #
def test_replays_membership_at_a_past_date(log):
    uh.record_changes(added=["AAA", "BBB"], date="2026-01-01")
    uh.record_changes(added=["CCC"], removed=["AAA"], date="2026-02-01")

    assert uh.universe_as_of("2025-12-31") == set()
    assert uh.universe_as_of("2026-01-15") == {"AAA", "BBB"}
    assert uh.universe_as_of("2026-02-15") == {"BBB", "CCC"}


def test_a_removed_name_is_still_reconstructable_for_its_own_window(log):
    """THE point. INTC left the universe; a study over January must still see it as
    tradeable then, or the pool has had its losers deleted retroactively."""
    uh.record_changes(added=["INTC"], date="2026-01-01")
    uh.record_changes(removed=["INTC"], date="2026-03-01")

    assert "INTC" in uh.universe_as_of("2026-02-01")
    assert "INTC" not in uh.universe_as_of("2026-04-01")


def test_readd_after_removal_resolves_in_order(log):
    uh.record_changes(added=["MU"], date="2026-01-01")
    uh.record_changes(removed=["MU"], date="2026-02-01")
    uh.record_changes(added=["MU"], date="2026-03-01")

    assert uh.universe_as_of("2026-01-15") == {"MU"}
    assert uh.universe_as_of("2026-02-15") == set()
    assert uh.universe_as_of("2026-03-15") == {"MU"}


def test_membership_spans_capture_each_window(log):
    uh.record_changes(added=["NVDA"], date="2026-01-01")
    uh.record_changes(removed=["NVDA"], date="2026-02-01")
    uh.record_changes(added=["NVDA"], date="2026-03-01")

    spans = uh.membership_spans()["NVDA"]
    assert spans == [{"added": "2026-01-01", "removed": "2026-02-01"},
                     {"added": "2026-03-01", "removed": None}]


# --------------------------------------------------------------------------- #
# Append-only integrity
# --------------------------------------------------------------------------- #
def test_the_log_is_append_only(log):
    uh.record_changes(added=["AAA"], date="2026-01-01")
    first = log.read_text()
    uh.record_changes(added=["BBB"], date="2026-02-01")
    assert log.read_text().startswith(first), "existing records must never be rewritten"


def test_a_corrupt_line_is_skipped_not_fatal(log):
    uh.record_changes(added=["AAA"], date="2026-01-01")
    with open(log, "a") as fh:
        fh.write("{not json\n")
    uh.record_changes(added=["BBB"], date="2026-02-01")
    assert uh.universe_as_of("2026-03-01") == {"AAA", "BBB"}


def test_records_are_normalized_and_deduped(log):
    uh.record_changes(added=["aaa", "AAA", " bbb "], date="2026-01-01")
    events = uh.load_history()
    assert sorted(e["ticker"] for e in events) == ["AAA", "BBB"]


def test_empty_change_writes_nothing(log):
    assert uh.record_changes(added=[], removed=[]) == 0
    assert not log.exists() or log.read_text() == ""


# --------------------------------------------------------------------------- #
# Seed + drift
# --------------------------------------------------------------------------- #
def test_seed_records_current_membership_once(log, tmp_path):
    write_universe(tmp_path, {"gpu": ["NVDA", "AMD"], "net": ["ANET"]})
    assert uh.seed_from_current(date="2026-07-18") == 3
    assert uh.seed_from_current(date="2026-07-19") == 0, "seed must be idempotent"
    assert uh.universe_as_of("2026-07-18") == {"NVDA", "AMD", "ANET"}


def test_seed_is_labelled_so_it_is_never_read_as_a_real_entry_date(log, tmp_path):
    """These names were not all added that day; the record must say so."""
    write_universe(tmp_path, {"gpu": ["NVDA"]})
    uh.seed_from_current(date="2026-07-18")
    rec = uh.load_history()[0]
    assert rec["source"] == "seed"
    assert "unrecoverable" in (rec["reason"] or "")


def test_drift_report_flags_a_write_that_bypassed_the_log(log, tmp_path):
    """A bypassed write means point-in-time reconstruction is silently wrong — the
    exact failure this module exists to prevent, so it must be visible."""
    write_universe(tmp_path, {"gpu": ["NVDA", "AMD"]})
    uh.record_changes(added=["NVDA"], date="2026-07-18")

    r = uh.drift_report()
    assert r["ok"] is False
    assert r["unlogged_additions"] == ["AMD"]
    assert "bypassed" in r["detail"]


def test_drift_report_clean_when_consistent(log, tmp_path):
    write_universe(tmp_path, {"gpu": ["NVDA", "AMD"]})
    uh.seed_from_current(date="2026-07-18")
    r = uh.drift_report()
    assert r["ok"] is True and r["n_live"] == r["n_replayed"] == 2


# --------------------------------------------------------------------------- #
# Both writers must log
# --------------------------------------------------------------------------- #
def test_both_universe_writers_record_changes():
    """Removals in the weekly review are an implicit set difference; if that path
    skipped the log, dropped names would leave no trace at all."""
    src = (Path(__file__).resolve().parent.parent / "runlib" / "reviews.py").read_text()
    assert src.count("from tools.universe_history import record_changes") == 2, (
        "both apply_universe_candidates and universe_review must log membership "
        "changes, or the log silently drifts from reality")
