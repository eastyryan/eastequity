"""A stopped-out loss must EARN a process win, not receive one by default.

THE BUG (audited 2026-07-19)
----------------------------
grade_exit_autopsy graded any stop exit `process_win` whenever entry_plan.stop_loss
was merely PRESENT. Its own comment admitted why: "We don't have ATR here". The
only path to process_fail was having no recorded stop — which the validator already
makes impossible. The branch was unfalsifiable: every stopped-out loss was a win.

Live consequence, the real DELL record in journal/exit_autopsies/:

    entry 450.6702 -> stop 408.00 -> filled 389.7542
    stop distance 9.5%, GAPPED THROUGH by 4.5%, realized -13.5% = 1.43x planned
    risk (-1.43R)
    graded: process_win, lesson "risk management worked"

That lesson propagated verbatim into data/concept_memory/DELL.json. Both of this
book's closed trades were losses; both were graded process wins. Any calibration
built on that record was built on a grader that could not fail.

THE RULE THESE TESTS PIN
    An unrunnable check must NOT resolve in the graded party's favour. If ATR is
    unavailable the noise-band test cannot run, and the grade is `mixed` —
    explicitly unverified — never `process_win`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.exit_autopsy import _stop_geometry, grade_exit_autopsy

ROOT = Path(__file__).resolve().parent.parent


def _stop_exit(*, entry=100.0, stop=95.0, fill=94.9, pnl=-50.0) -> dict:
    return {
        "ticker": "TEST", "action": "SELL_TO_CLOSE",
        "avg_cost": entry, "fill_price": fill, "days_held": 5,
        "forced": True, "forced_reason": "stop_loss_breached",
        "realized_pnl_usd": pnl,
        "entry_plan": {"stop_loss": stop, "target_price": 130.0,
                       "holding_horizon_days": 30, "entry_price_max": entry,
                       "thesis_invalidators": {"a": "x", "b": "y", "c": "z"}},
    }


def _process(rec, **kw) -> str:
    return grade_exit_autopsy(rec, **kw)["brain_grade"]["process"]


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def test_geometry_matches_the_real_dell_numbers():
    geom = _stop_geometry(_stop_exit(entry=450.6702, stop=408.0, fill=389.7542))
    assert geom["stop_distance_pct"] == pytest.approx(0.0947, abs=0.001)
    assert geom["gap_through_pct"] == pytest.approx(0.0447, abs=0.001)
    assert geom["realized_loss_pct"] == pytest.approx(-0.1352, abs=0.001)
    assert geom["realized_risk_multiple"] == pytest.approx(1.43, abs=0.02)


def test_geometry_is_safe_on_missing_fields():
    assert _stop_geometry({}) == {}
    assert _stop_geometry({"avg_cost": None, "fill_price": "x"}) == {}


# ---------------------------------------------------------------------------
# the grade must be earned
# ---------------------------------------------------------------------------

def test_stop_inside_the_noise_band_is_a_process_fail():
    """A 2% stop on a 5%-ATR name: an ordinary session took you out."""
    rec = _stop_exit(entry=100.0, stop=98.0, fill=97.9)
    out = grade_exit_autopsy(rec, atr_pct=0.05)
    assert out["brain_grade"]["process"] == "process_fail"
    assert "stop_inside_noise_band" in out["brain_grade"]["tags"]
    assert out["binding"] is True
    assert "noise band" in out["lesson"]


def test_gap_through_is_not_a_win():
    """THE DELL CASE. The stop was honoured; it did not hold the line it was
    sized against, and the loss was 1.43x planned risk."""
    rec = _stop_exit(entry=450.6702, stop=408.0, fill=389.7542, pnl=-135.19)
    out = grade_exit_autopsy(rec, atr_pct=0.04)
    assert out["brain_grade"]["process"] == "mixed", (
        "a 4.5% gap-through costing 1.43x planned risk was graded a process win")
    assert "stop_gapped_through" in out["brain_grade"]["tags"]
    assert out["binding"] is True, "a gap-through lesson must carry forward"
    assert "gapped" in out["lesson"]


def test_unknown_atr_is_not_a_win():
    """THE SHAPE OF THE ORIGINAL BUG: an unrunnable check resolving favourably."""
    rec = _stop_exit(entry=100.0, stop=90.0, fill=89.95)
    out = grade_exit_autopsy(rec, atr_pct=None)
    assert out["brain_grade"]["process"] == "mixed"
    assert "noise_band_unverified" in out["brain_grade"]["tags"]
    assert "could NOT run" in out["lesson"]


def test_a_clean_stop_outside_the_band_still_earns_a_win():
    """The inverse must hold, or the grader just always fails and teaches nothing."""
    rec = _stop_exit(entry=100.0, stop=90.0, fill=89.95)
    out = grade_exit_autopsy(rec, atr_pct=0.04)
    assert out["brain_grade"]["process"] == "process_win"
    assert "stop_outside_noise_band" in out["brain_grade"]["tags"]
    assert out["binding"] is False


def test_missing_recorded_stop_is_still_a_plan_integrity_failure():
    rec = _stop_exit()
    rec["entry_plan"]["stop_loss"] = None
    assert _process(rec, atr_pct=0.04) == "process_fail"


def test_grade_shows_its_working():
    """A grade whose inputs are invisible cannot be argued with."""
    rec = _stop_exit(entry=450.6702, stop=408.0, fill=389.7542, pnl=-135.19)
    geom = grade_exit_autopsy(rec, atr_pct=0.04)["brain_grade"]["stop_geometry"]
    assert geom["gap_through_pct"] > 0
    assert geom["realized_risk_multiple"] > 1


def test_grader_version_is_bumped():
    """v1 is the unfalsifiable one; downstream must be able to tell them apart."""
    out = grade_exit_autopsy(_stop_exit(), atr_pct=0.04)
    assert out["brain_grade"]["graded_by"] == "deterministic_v2_atr"


# ---------------------------------------------------------------------------
# non-stop branches are untouched
# ---------------------------------------------------------------------------

def test_discretionary_loss_is_still_a_process_fail():
    rec = _stop_exit(pnl=-50.0)
    rec["forced"] = False
    rec["forced_reason"] = ""
    assert _process(rec, atr_pct=0.04) == "process_fail"


def test_discretionary_win_is_still_a_process_win():
    rec = _stop_exit(pnl=120.0)
    rec["forced"] = False
    rec["forced_reason"] = ""
    assert _process(rec, atr_pct=0.04) == "process_win"


def test_horizon_exit_with_flat_pnl_is_still_stalled_capital():
    rec = _stop_exit(pnl=10.0)
    rec["forced_reason"] = "horizon_expired"
    rec["days_held"] = 30
    out = grade_exit_autopsy(rec, atr_pct=0.04)
    assert out["brain_grade"]["process"] == "process_fail"
    assert "stalled_capital" in out["brain_grade"]["tags"]


# ---------------------------------------------------------------------------
# against the real journal
# ---------------------------------------------------------------------------

def test_the_real_dell_record_is_no_longer_a_process_win():
    """Regression against the actual file on disk, not a fixture."""
    path = ROOT / "journal" / "exit_autopsies"
    records = []
    for f in sorted(path.glob("*.jsonl")) if path.exists() else []:
        for line in f.read_text().splitlines():
            if line.strip():
                records.append(json.loads(line))
    dell = [r for r in records if r.get("ticker") == "DELL"
            and str(r.get("forced_reason", "")).startswith("stop")]
    if not dell:
        pytest.skip("no real DELL stop-exit record on disk")
    out = grade_exit_autopsy(dell[0], atr_pct=0.04)
    assert out["brain_grade"]["process"] != "process_win", (
        "the -1.43R gap-through is graded a process win again")
