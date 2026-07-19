"""Four execution defects found by auditing the code rather than the tests.

Each of these was reachable in normal operation and silent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_entry_price_max_reaches_the_broker_payload():
    """alpaca_broker's live last-trade guard reads order["entry_price_max"].

    brain_io only ever nested that value inside `plan`, so the guard evaluated None
    and no-opped on EVERY buy since it was written. The only ceiling actually
    enforced was against the bundle price, which the overlay allows to be 30 minutes
    old — so a name gapping between snapshot and submission filled above the stated
    maximum entry.
    """
    import inspect
    from runlib import brain_io
    src = inspect.getsource(brain_io.execute)
    call = src[src.index("broker.place_order({"):]
    call = call[:call.index("})")]
    assert '"entry_price_max"' in call, "the live entry ceiling is unreachable again"


def test_stop_watch_loads_a_real_atr_map():
    """It read data/universe_scan.json, which has never existed, behind a bare
    `except: pass` — so the chandelier trail never ratcheted on a tick and the
    simulation's stop gap-through model was disabled on every tick-driven exit."""
    from stop_watch import _load_atr_map
    assert not (Path(__file__).resolve().parent.parent / "data" / "universe_scan.json").exists()
    atr = _load_atr_map()
    assert isinstance(atr, dict)
    if atr:
        k, v = next(iter(atr.items()))
        assert isinstance(k, str) and isinstance(v, (int, float))


def test_split_dedupe_records_the_event_key_not_a_field_name():
    """`key` was rebound by two inner loops, so seen.add(key) added the literal
    string "trailing_stop". That silently disabled the within-run split guard whose
    absence produced the documented quantity 10 -> 160 double-apply."""
    import inspect
    from execution import corporate_actions as ca
    src = inspect.getsource(ca.apply_corporate_actions)
    split = src[src.index('_event_key("split"'):]
    body = split[:split.index("summary[\"splits_applied\"]")]
    for line in body.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("for key in ("), \
            f"`key` is shadowed again inside the split branch: {stripped}"


def test_a_full_close_is_refused_rather_than_silently_partial(monkeypatch):
    """The severe one. A resting stop HOLDS its shares; Alpaca's cancel is async.
    Sizing the sell before the cancel propagated turned a full close into a ~6%
    partial that was journaled as a completed forced exit while the position stayed
    open."""
    from execution import alpaca_broker as ab
    # Position of 5.34 shares with 5 still held by a working stop.
    monkeypatch.setattr(ab, "_req",
                        lambda m, p, **k: (200, {"qty": "5.34", "qty_available": "0.34"}))
    assert ab._sell_qty("DELL", None) is None, "sized a full close against held shares"


def test_a_partial_close_is_still_allowed(monkeypatch):
    """The mirror — an explicit sell_fraction must still work."""
    from execution import alpaca_broker as ab
    monkeypatch.setattr(ab, "_req",
                        lambda m, p, **k: (200, {"qty": "10", "qty_available": "10"}))
    assert ab._sell_qty("DELL", 0.5) == 5.0


def test_a_clean_full_close_still_works(monkeypatch):
    """Without this, 'refuse everything' passes the test above and no exit ever fills."""
    from execution import alpaca_broker as ab
    monkeypatch.setattr(ab, "_req",
                        lambda m, p, **k: (200, {"qty": "5.34", "qty_available": "5.34"}))
    assert ab._sell_qty("DELL", None) == 5.34
