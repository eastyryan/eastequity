"""The ledger must survive a crash mid-write.

state/portfolio.json is the authoritative book — cash, every position, every plan,
the full fill history — and it was written with a bare write_text() on every fill,
every mark-to-market, and every trailing-stop update. A crash or SIGKILL between
truncate and flush left it partial, and _load does a bare json.loads with no schema
check and no fallback: the next run dies on a corrupt book with no backup.

The correct pattern already existed in this repo (tools/guidance_ledger.py,
tools/smart_money_13f.py) and had been applied to a CUSIP cache but not the
portfolio.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution import simulated_broker as sb  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_save_is_atomic_no_partial_file_visible(tmp_path, monkeypatch):
    """A reader must see either the whole old file or the whole new one."""
    state_file = tmp_path / "portfolio.json"
    monkeypatch.setattr(sb, "STATE_FILE", state_file)

    sb._save({"cash_usd": 1.0, "positions": [], "history": [], "pending_orders": {}})
    first = json.loads(state_file.read_text())
    assert first["cash_usd"] == 1.0

    # A much larger second write — the case most likely to tear.
    big = {"cash_usd": 2.0, "positions": [], "pending_orders": {},
           "history": [{"i": i, "pad": "x" * 500} for i in range(2000)]}
    sb._save(big)
    reread = json.loads(state_file.read_text())
    assert reread["cash_usd"] == 2.0
    assert len(reread["history"]) == 2000


def test_no_temp_files_left_behind(tmp_path, monkeypatch):
    state_file = tmp_path / "portfolio.json"
    monkeypatch.setattr(sb, "STATE_FILE", state_file)
    for i in range(5):
        sb._save({"cash_usd": float(i), "positions": [],
                  "history": [], "pending_orders": {}})
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert not leftovers, f"temp files leaked: {leftovers}"


def test_failed_serialization_leaves_the_old_book_intact(tmp_path, monkeypatch):
    """If the new state cannot be serialized, the previous book must survive."""
    state_file = tmp_path / "portfolio.json"
    monkeypatch.setattr(sb, "STATE_FILE", state_file)
    sb._save({"cash_usd": 42.0, "positions": [], "history": [], "pending_orders": {}})

    class Unserializable:
        def __repr__(self):
            raise RuntimeError("boom")

    with pytest.raises(Exception):
        sb._save({"cash_usd": Unserializable()})

    # Old content still parses and is unchanged.
    assert json.loads(state_file.read_text())["cash_usd"] == 42.0
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_previous_book_stays_intact_for_the_whole_write(tmp_path, monkeypatch):
    """The actual atomicity property, tested directly.

    Throughout serialization of the NEW state, the live ledger path must still hold
    the complete OLD content — that is the window in which a crash would destroy it.
    Under the old bare write_text() the file was truncated at the very start of the
    write, so this window did not exist and any crash inside it was fatal.
    """
    state_file = tmp_path / "portfolio.json"
    monkeypatch.setattr(sb, "STATE_FILE", state_file)

    sb._save({"cash_usd": 999.0, "positions": [], "history": [], "pending_orders": {}})
    old_bytes = state_file.read_bytes()

    observed = {}
    real_replace = os.replace

    def spy(src, dst):
        # The instant before the swap: whatever a concurrent reader (or a crashed
        # restart) would find at the ledger path.
        observed["at_replace"] = Path(dst).read_bytes()
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)

    big = {"cash_usd": 111.0, "positions": [], "pending_orders": {},
           "history": [{"i": i, "pad": "y" * 800} for i in range(20000)]}
    sb._save(big)

    assert "at_replace" in observed, (
        "no os.replace occurred — the ledger is being written in place, so a crash "
        "mid-write truncates the authoritative book")
    assert observed["at_replace"] == old_bytes, (
        "the previous book was already modified before the atomic swap")
    assert json.loads(state_file.read_text())["cash_usd"] == 111.0


def test_intents_writer_is_also_atomic():
    """state/order_intents.json is the cloud order queue; clear_intents does a
    read-modify-write over it, so a partial write strands or duplicates real orders."""
    src = (ROOT / "execution" / "alpaca_broker.py").read_text()
    body = src.split("def _save_intents", 1)[1].split("\ndef ", 1)[0]
    assert "os.replace" in body, "order intents must be written atomically"
    assert "write_text" not in body


def test_no_bare_write_text_on_the_portfolio_ledger():
    """Guard against the pattern creeping back into either broker."""
    for rel in ("execution/simulated_broker.py", "execution/alpaca_broker.py"):
        src = (ROOT / rel).read_text()
        assert "STATE_FILE.write_text(" not in src, f"{rel} writes the book non-atomically"
        assert "INTENTS_FILE.write_text(" not in src, f"{rel} writes intents non-atomically"
