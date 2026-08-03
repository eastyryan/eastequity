"""FOUR files describe the same seven slots. They have already drifted twice.

The schedule lives in four places that nothing checked against each other:

  * autonomy_config.json      schedule.slot_depths      (what the cloud trader resolves)
  * runlib/depths.py          DEFAULT_SLOT_DEPTHS       (the fallback when cfg is absent)
  * runlib/analytics.py       expected_slots()          (what the heartbeat grades)
  * scripts/run_cycle.sh      the launchd slot gate     (what the local trader fires on)

Both drifts are in the history and both cost real slots:

  * 2026-07-25: 09:00/10:00 became 08:45/10:30. Anything still grading against the old
    list reports phantom drift — matching the 09:22 runs of that week against an 08:45
    slot manufactures a "+37 minute drift" that never happened.
  * 2026-08-03: 10:30 was promoted from holdings_watchlist to `full` in
    autonomy_config.json and runlib/depths.py, and scripts/run_cycle.sh still carried
    `0845|1030|1200|1400) --depth holdings_watchlist` in its own hardcoded map. The
    local trader would have run a mini-scan at the exact slot that had just been changed
    to put a full universe scan inside the session. run_cycle.sh now uses --auto-depth
    and this pins the one thing it still hardcodes: WHICH ticks fire.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _hhmm(slot: float) -> str:
    return f"{int(slot):02d}{int(round((slot % 1) * 60)):02d}"


def _weekday_slots() -> list[float]:
    from runlib.analytics import expected_slots
    return expected_slots(True)


def test_config_slot_depths_cover_exactly_the_graded_slots():
    cfg = json.loads((ROOT / "autonomy_config.json").read_text())
    keys = {k for k in cfg["schedule"]["slot_depths"]
            if len(k) == 4 and k.isdigit()}
    assert keys == {_hhmm(s) for s in _weekday_slots()}


def test_the_depth_defaults_cover_exactly_the_graded_slots():
    """The fallback map is what a run gets when cfg is missing — a fallback that names
    different slots than the config is a silent depth change under failure."""
    from runlib.depths import DEFAULT_SLOT_DEPTHS
    assert set(DEFAULT_SLOT_DEPTHS) == {_hhmm(s) for s in _weekday_slots()}


def test_the_depth_defaults_agree_with_the_live_config():
    cfg = json.loads((ROOT / "autonomy_config.json").read_text())
    from runlib.depths import DEFAULT_SLOT_DEPTHS
    live = {k: v for k, v in cfg["schedule"]["slot_depths"].items()
            if len(k) == 4 and k.isdigit()}
    assert live == DEFAULT_SLOT_DEPTHS


def test_run_cycle_fires_on_exactly_the_graded_slots():
    src = (ROOT / "scripts" / "run_cycle.sh").read_text()
    gate = re.search(r"^\s*(0600(?:\|\d{4})*)\)\s*;;", src, re.M)
    assert gate, "the weekday slot gate in scripts/run_cycle.sh moved or vanished"
    assert set(gate.group(1).split("|")) == {_hhmm(s) for s in _weekday_slots()}


def test_run_cycle_no_longer_hardcodes_depths():
    """A second copy of the depth map is a second thing to forget."""
    src = (ROOT / "scripts" / "run_cycle.sh").read_text()
    # Comments strip out: the block explaining WHY the map is gone necessarily quotes it.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "--auto-depth" in code
    assert "--depth " not in code, "a second copy of the depth map is back"


def test_every_slot_gap_leaves_room_for_a_recovery():
    """The watchdog can only repair a slot if a recovery launched after the miss is
    detectable can still FINISH before the next slot's window opens. Tighten the
    schedule past that and self-healing silently stops working — which is the state the
    16:00 slot was already in, because slot_report would not call it missed until 17:00
    and 17:30 is only 90 minutes away."""
    import scripts.find_missed_slot as fm
    from runlib.analytics import SLOT_EARLY_TOLERANCE_H

    slots = _weekday_slots()
    need = fm.NO_SHOW_H + fm.RECOVERY_RUN_H + SLOT_EARLY_TOLERANCE_H
    for a, b in zip(slots, slots[1:]):
        assert b - a > need, (
            f"the {_hhmm(a)}->{_hhmm(b)} gap is {(b - a) * 60:.0f} min; a recovery needs "
            f"{need * 60:.0f} min (detect {fm.NO_SHOW_H * 60:.0f} + run "
            f"{fm.RECOVERY_RUN_H * 60:.0f} + early tolerance "
            f"{SLOT_EARLY_TOLERANCE_H * 60:.0f}) or the slot cannot be self-healed")
