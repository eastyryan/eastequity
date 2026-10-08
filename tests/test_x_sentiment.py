"""x_sentiment soft-context block: present, absent, stale, and never raises."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from runlib.context_tiers import (
    BLOCKING_KEYS, DECISION_CONTEXT, READ_WINDOW_LINES, key_start_lines, slim_context_for_brain,
    unregistered_keys,
)
from runlib.x_sentiment import NOTE, attach_x_sentiment, load_x_sentiment


def _write(tmp_path, text, age_hours):
    p = tmp_path / "x_sentiment_latest.md"
    p.write_text(text)
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).timestamp()
    os.utime(p, (ts, ts))
    return p


def test_present(tmp_path):
    p = _write(tmp_path, "# X read\nRisk-off into the open.", age_hours=1)
    blk = load_x_sentiment(p)
    assert blk["status"] == "present"
    assert blk["note"] == NOTE
    assert "Risk-off" in blk["text"]
    assert 0.5 <= blk["age_hours"] <= 1.5
    as_of = datetime.fromisoformat(blk["as_of"])
    assert as_of.utcoffset() is not None, "as_of must carry an explicit offset"


def test_absent_never_raises(tmp_path):
    blk = load_x_sentiment(tmp_path / "missing.md")
    assert blk["status"] == "absent"
    assert blk["text"] is None and blk["as_of"] is None
    assert blk["note"] == NOTE
    # empty file is absent too
    assert load_x_sentiment(_write(tmp_path, "   \n", 0))["status"] == "absent"
    # a directory where the file should be: still absent, still no exception
    d = tmp_path / "adir.md"
    d.mkdir()
    assert load_x_sentiment(d)["status"] == "absent"
    ctx: dict = {}
    attach_x_sentiment(ctx, tmp_path / "missing.md")
    assert ctx["x_sentiment"]["status"] == "absent"


def test_stale(tmp_path):
    p = _write(tmp_path, "old mood", age_hours=26)
    blk = load_x_sentiment(p)
    assert blk["status"] == "stale"
    assert blk["age_hours"] > 20
    assert blk["text"] == "old mood"


def test_text_is_capped(tmp_path):
    p = _write(tmp_path, "x" * 9000, age_hours=0)
    blk = load_x_sentiment(p, max_chars=6000)
    assert blk["truncated"] is True and blk["full_chars"] == 9000
    assert blk["text"].startswith("x" * 6000)
    assert "TRUNCATED" in blk["text"]
    assert len(blk["text"]) < 6200


def test_registered_early_and_not_a_blocking_key(tmp_path):
    assert "x_sentiment" in DECISION_CONTEXT
    assert "x_sentiment" not in BLOCKING_KEYS, "soft context must never be a gate"
    # emitted with the regime read, ahead of the bulkier decision-context blocks
    assert DECISION_CONTEXT.index("x_sentiment") < DECISION_CONTEXT.index("market_breadth")
    full = {"reasoning_process": {}, "portfolio": {}}
    attach_x_sentiment(full, _write(tmp_path, "y" * 7000, age_hours=0))
    assert unregistered_keys(full) == []
    pack = slim_context_for_brain(full)
    assert pack["x_sentiment"]["status"] == "present"
    assert key_start_lines(pack)["x_sentiment"] <= READ_WINDOW_LINES
    assert "x_sentiment" not in pack["_pack_budget"]["unregistered_keys"]
