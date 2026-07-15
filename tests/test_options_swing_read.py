"""Offline tests for options swing interpretation helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.options_signal import (
    build_swing_options_read,
    classify_iv,
    classify_pc_volume,
    classify_skew,
    classify_unusual,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def test_pc_and_skew():
    print("P/C and skew regimes:")
    check("put heavy", classify_pc_volume(2.0)["regime"] == "put_heavy")
    check("call heavy", classify_pc_volume(0.4)["regime"] == "call_heavy")
    check("put skew", classify_skew(9.5)["tilt"] == "bearish_hedging_demand")
    check("call skew", classify_skew(-4.0)["tilt"] == "bullish_speculation")
    check("elevated IV", classify_iv(142.0, 18.0)["regime"] == "very_elevated")


def test_swing_read_nbis_like():
    print("NBIS-like options snapshot:")
    sig = {
        "status": "ok",
        "expected_move_pct": 18.74,
        "atm_iv_pct": 141.9,
        "put_call_volume_ratio": 2.03,
        "iv_skew_pct": 9.5,
        "skew_read": "puts_bid_over_calls",
        "unusual_strikes": [
            {"type": "call", "strike": 230.0, "volume": 1154, "open_interest": 369, "pct_from_spot": 18.5},
            {"type": "put", "strike": 155.0, "volume": 735, "open_interest": 201, "pct_from_spot": -20.1},
        ],
        "spot": 194.0,
        "expiry": "2026-07-24",
        "days_to_expiry": 10,
    }
    r = build_swing_options_read(sig)
    check("status ok", r["status"] == "ok")
    check("confidence always low", r["direction_confidence"] == "low")
    check("has bearish tilts (put heavy + skew + IV)", len(r["bearish_tilts"]) >= 2)
    check("net not strongly bullish", r["net_tilt"] in (
        "lean_bearish", "slight_bearish", "mixed_or_neutral"))
    check("stop engineering present", r["for_stop_engineering"].get("expected_move_pct") == 18.74)
    check("playbook keys", "into_earnings" in (r.get("swing_playbook") or {}))


def test_unusual():
    print("unusual strikes:")
    u = classify_unusual(
        [{"type": "call", "strike": 100, "pct_from_spot": 5.0, "volume": 1, "open_interest": 1}],
        95.0)
    check("has unusual", u["has_unusual"] is True)
    check("notes", len(u["notes"]) >= 1)


if __name__ == "__main__":
    test_pc_and_skew()
    test_swing_read_nbis_like()
    test_unusual()
    if FAILURES:
        print(f"FAILED {FAILURES}")
        sys.exit(1)
    print("All options swing-read tests passed.")
