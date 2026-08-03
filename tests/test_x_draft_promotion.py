"""The BKR 07-28 posting gap — offline, no network.

Two stacked failures let the book's first BUY in 18 days go unannounced:
(1) the poster only posts `x_draft_*_trade.txt`, and the `_trade` suffix was
written only when the RUN carried fills — a cloud run queues its order, so an
executor-filled trade never earned the suffix; (2) the run's plain draft was
written pre-fill, so its text literally says "No new trades today" — promoting
a copy would announce a trade with a memo denying one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.x_poster as XP


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "journal" / "proposals").mkdir(parents=True)
    monkeypatch.setattr(XP, "ROOT", tmp_path)
    return tmp_path


ORDER = {"ticker": "BKR", "action": "BUY", "proposal_id": "20260728-b66630",
         "position_size_usd": 57.93,
         "plan": {"stop_loss": 54.0, "target_price": 70.0,
                  "holding_horizon_days": 45}}
FILL = {"ticker": "BKR", "action": "BUY", "fill_price": 58.198,
        "notional_usd": 57.93, "quantity": 0.995395}


def test_a_pre_fill_no_trade_draft_is_replaced_not_copied(sandbox):
    """The exact BKR shape: the plain draft denies the trade that later filled."""
    (sandbox / "state" / "x_draft_20260728-b66630.txt").write_text(
        "East Equity Agent swing update (Jul 28)\n\nNo new trades today.\n")
    (sandbox / "journal" / "proposals" / "2026-07-28.jsonl").write_text(
        json.dumps({"run_id": "20260728-b66630",
                    "proposal": {"ticker": "BKR",
                                 "thesis": "Record Q2 orders. Guidance raised."}})
        + "\n")
    path = XP.promote_draft_for_fill("20260728-b66630", order=ORDER, fill=FILL)
    text = Path(path).read_text()
    assert "No new trades" not in text
    assert "Opened" in text and "$BKR" in text and "58.20" in text
    assert "stop $54.00" in text and "target $70.00" in text and "45d" in text
    assert "Record Q2 orders." in text            # the brain's own words
    assert "not advice" in text                   # standing disclaimer


def test_a_draft_that_already_announces_the_fill_is_kept_verbatim(sandbox):
    (sandbox / "state" / "x_draft_20260715-e2c95f.txt").write_text(
        "Closed **DELL** $DELL at $389.75 — stop breached.\n")
    path = XP.promote_draft_for_fill("20260715-e2c95f", order={}, fill={})
    assert Path(path).read_text().startswith("Closed **DELL**")


def test_promotion_is_idempotent(sandbox):
    (sandbox / "state" / "x_draft_20260728-b66630_trade.txt").write_text("existing\n")
    path = XP.promote_draft_for_fill("20260728-b66630", order=ORDER, fill=FILL)
    assert Path(path).read_text() == "existing\n"   # never overwritten


def test_no_fill_price_means_no_memo(sandbox):
    """Compose only from facts — a fill without a price cannot be announced."""
    assert XP.promote_draft_for_fill("20260728-b66630", order=ORDER,
                                     fill={"ticker": "BKR"}) is None
    assert XP.promote_draft_for_fill(None) is None


def test_composition_survives_a_missing_proposal_journal(sandbox):
    """Fail-soft: no proposal record -> memo still composes from the fill/plan."""
    path = XP.promote_draft_for_fill("20260728-b66630", order=ORDER, fill=FILL)
    text = Path(path).read_text()
    assert "Opened" in text and "not advice" in text


def test_the_rendered_memo_keeps_exactly_one_cashtag(sandbox):
    path = XP.promote_draft_for_fill("20260728-b66630", order=ORDER, fill=FILL)
    rendered = XP.format_for_x(Path(path).read_text())
    assert rendered.count("$BKR") == 1
