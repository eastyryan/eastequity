"""Box-run poster (2026-10-08) - offline, no network, X is never called.

Failure being fixed: GitHub delivered x-post.yml's cron after 19:00 ET every
day from 09-24, the poster's 16:00-18:59 ET gate returned silently, and the
10-02/10-05/10-06/10-07 trades never posted. Plus: the out-of-band stop draft
was undated (never matched the same-day filter) and its early return reused a
stale 09-18 HPE memo; and "newest wins" was really "highest random hex".
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.et_time as ET
import tools.x_poster as XP

ET_TZ = ZoneInfo("America/New_York")


@pytest.fixture
def calls():
    """Texts the fake X endpoint received."""
    return []


@pytest.fixture
def sandbox(tmp_path, monkeypatch, calls):
    (tmp_path / "state").mkdir()
    (tmp_path / "journal" / "trades").mkdir(parents=True)
    (tmp_path / "journal" / "proposals").mkdir(parents=True)
    monkeypatch.setattr(XP, "ROOT", tmp_path)
    monkeypatch.setattr(XP, "POST_LOG", tmp_path / "journal" / "x_posts.jsonl")
    def fake_post(text, media_path=None):
        calls.append(text)
        return {"status": "posted", "tweet_id": str(1000 + len(calls)),
                "with_media": False}

    monkeypatch.setattr(XP, "post_tweet", fake_post)
    import tools.chart_card as CC
    monkeypatch.setattr(CC, "render_equity_card",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no chart")))
    return tmp_path


def _clock(monkeypatch, y, m, d, hh, mm=0):
    monkeypatch.setattr(ET, "et_now", lambda: datetime(y, m, d, hh, mm, tzinfo=ET_TZ))


def _log(sandbox):
    f = sandbox / "journal" / "x_posts.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


RICH = ("Opened a small test position in Natera $NTRA at about $398. " * 6).strip()

# --------------------------------------------------------------- window gate

def test_outside_window_skips_loudly_and_posts_nothing(sandbox, calls, monkeypatch, capsys):
    _clock(monkeypatch, 2026, 10, 7, 20, 17)          # the 10-07 GitHub run time
    (sandbox / "state" / "x_draft_20261007-fdcbe1_trade.txt").write_text(RICH)
    assert XP.process_drafts() == []
    out = capsys.readouterr().out
    assert '"skipped"' in out and "16:00-18:59" in out
    assert calls == [] and _log(sandbox) == []


def test_force_window_posts_the_same_day_draft(sandbox, calls, monkeypatch):
    _clock(monkeypatch, 2026, 10, 7, 20, 17)
    (sandbox / "state" / "x_draft_20261007-fdcbe1_trade.txt").write_text(RICH)
    res = XP.process_drafts(force_window=True)
    assert res[0]["status"] == "posted" and len(calls) == 1


def test_box_slot_at_1630_posts_once_and_a_second_run_is_a_noop(sandbox, calls, monkeypatch):
    _clock(monkeypatch, 2026, 10, 7, 16, 30)
    (sandbox / "state" / "x_draft_20261007-fdcbe1_trade.txt").write_text(RICH)
    XP.process_drafts()
    _clock(monkeypatch, 2026, 10, 7, 18, 15)          # e.g. a manual GH dispatch
    assert XP.process_drafts() == []
    assert len(calls) == 1


def test_no_same_day_draft_is_a_clean_skip(sandbox, monkeypatch, capsys):
    _clock(monkeypatch, 2026, 10, 8, 16, 30)
    (sandbox / "state" / "x_draft_20261007-fdcbe1_trade.txt").write_text(RICH)
    assert XP.process_drafts() == []
    assert "no same-day trade draft" in capsys.readouterr().out
    assert XP.main(["--force-window"]) == 0


def test_dry_run_never_consumes_the_draft(sandbox, calls, monkeypatch):
    _clock(monkeypatch, 2026, 10, 7, 16, 30)
    (sandbox / "state" / "x_draft_20261007-fdcbe1_trade.txt").write_text(RICH)
    XP.process_drafts(dry_run=True)
    assert _log(sandbox) == [] and calls == []
    XP.process_drafts()
    assert len(calls) == 1


# ------------------------------------------------------------ duplicate guard

def test_posted_today_uses_the_et_day_not_utc(sandbox, monkeypatch):
    # 20:30 ET on 10-07 is 00:30 UTC on 10-08: still 10-07's daily post.
    XP._append_log({"ts": "2026-10-08T00:30:00+00:00", "draft": "x", "kind": "trade",
                    "status": "posted"})
    assert XP._posted_today("20261007")
    assert not XP._posted_today("20261008")


def test_catchups_do_not_use_up_the_daily_post(sandbox, calls, monkeypatch):
    _clock(monkeypatch, 2026, 10, 8, 9, 0)
    cu = sandbox / "state" / "x_catchup"
    cu.mkdir()
    (cu / "20261002.txt").write_text(RICH)
    rec = XP.process_drafts(force_window=True, draft="state/x_catchup/20261002.txt")
    assert rec[0]["kind"] == "catchup" and rec[0]["draft"] == "x_catchup/20261002.txt"
    assert not XP._posted_today("20261008")
    # and the same catch-up never posts twice
    assert XP.process_drafts(force_window=True,
                             draft="state/x_catchup/20261002.txt") == []
    assert len(calls) == 1


def test_a_post_with_unknown_outcome_is_never_retried(sandbox, monkeypatch):
    _clock(monkeypatch, 2026, 10, 7, 16, 30)
    (sandbox / "state" / "x_draft_20261007-fdcbe1_trade.txt").write_text(RICH)
    monkeypatch.setattr(XP, "post_tweet",
                        lambda t, media_path=None: {"status": "unknown", "error": "ReadTimeout"})
    XP.process_drafts()
    assert _log(sandbox)[0]["status"] == "unknown"
    called = []
    monkeypatch.setattr(XP, "post_tweet",
                        lambda t, media_path=None: called.append(t) or {"status": "posted"})
    XP.process_drafts()
    assert called == []


def test_an_api_error_is_logged_but_does_not_consume(sandbox, monkeypatch):
    _clock(monkeypatch, 2026, 10, 7, 16, 30)
    (sandbox / "state" / "x_draft_20261007-fdcbe1_trade.txt").write_text(RICH)
    monkeypatch.setattr(XP, "post_tweet",
                        lambda t, media_path=None: {"status": "error", "code": 402, "body": ""})
    assert XP.main([]) == 2
    assert _log(sandbox)[0]["code"] == 402
    assert "x_draft_20261007-fdcbe1_trade.txt" not in XP._already_posted()


# ---------------------------------------------------------------- newest wins

def _trade(sandbox, day, run_id, ts, ticker="X"):
    f = sandbox / "journal" / "trades" / f"{day}.jsonl"
    with f.open("a") as fh:
        fh.write(json.dumps({"ts": ts, "run_id": run_id,
                             "fill": {"ticker": ticker}}) + "\n")


def test_newest_is_by_fill_time_not_hex(sandbox):
    st = sandbox / "state"
    # 'f0...' sorts after '0a...' but filled EARLIER
    a = st / "x_draft_20261005-f00000_trade.txt"
    b = st / "x_draft_20261005-0a0000_trade.txt"
    a.write_text(RICH + " early")
    b.write_text(RICH + " late")
    _trade(sandbox, "2026-10-05", "20261005-f00000", "2026-10-05T15:00:00+00:00")
    _trade(sandbox, "2026-10-05", "20261005-0a0000", "2026-10-05T19:00:00+00:00")
    assert XP.pick_daily_draft([a, b], "20261005") == b


def test_a_real_memo_beats_a_later_thin_probe(sandbox):
    """10-05 shape: ILMN memo (966 chars) at 11:04, OKTA one-liner at 15:12."""
    st = sandbox / "state"
    rich = st / "x_draft_20261005-0e6781_trade.txt"
    thin = st / "x_draft_20261005-cf6fd3_trade.txt"
    rich.write_text(RICH)
    thin.write_text("3pm paper slot: proposing a half-size $OKTA probe.")
    _trade(sandbox, "2026-10-05", "20261005-0e6781", "2026-10-05T15:04:21+00:00")
    _trade(sandbox, "2026-10-05", "20261005-cf6fd3", "2026-10-05T19:12:36+00:00")
    assert XP.pick_daily_draft([rich, thin], "20261005") == rich


# ----------------------------------------------------------- out-of-band drafts

OOB_ORDER = {"ticker": "ILMN", "action": "SELL_TO_CLOSE", "proposal_id": "out-of-band",
             "out_of_band": True, "forced_exit_reason": "resting_stop_breached",
             "submitted_at": "2026-10-07T13:39:06.709508Z"}
OOB_FILL = {"ticker": "ILMN", "action": "SELL_TO_CLOSE", "fill_price": 261.792,
            "notional_usd": 69.22, "realized_pnl_usd": -4.8,
            "filled_at": "2026-10-07T13:39:06.711648Z"}


def test_out_of_band_draft_is_date_stamped_and_ignores_a_stale_undated_file(sandbox):
    stale = sandbox / "state" / "x_draft_out-of-band_trade.txt"
    stale.write_text("Closed **HPE** $HPE at $53.04 ($53 position).\n")
    path = Path(XP.promote_draft_for_fill("out-of-band", order=OOB_ORDER, fill=OOB_FILL))
    assert path.name == "x_draft_20261007-oob-ILMN_trade.txt"
    text = path.read_text()
    assert "HPE" not in text
    assert "Closed **ILMN** $ILMN at $261.79" in text
    assert "resting stop" in text and "-$4.80" in text and "not advice" in text
    assert stale.read_text().startswith("Closed **HPE**")   # untouched, not reused


def test_out_of_band_draft_is_eligible_for_that_days_post(sandbox, monkeypatch):
    XP.promote_draft_for_fill("out-of-band", order=OOB_ORDER, fill=OOB_FILL)
    _clock(monkeypatch, 2026, 10, 7, 16, 30)
    res = XP.process_drafts()
    assert res and res[0]["draft"] == "x_draft_20261007-oob-ILMN_trade.txt"


def test_ledger_repairs_get_no_public_memo(sandbox):
    for rid in ("ghost-reconcile", "not-at-broker-writeoff"):
        assert XP.promote_draft_for_fill(rid, order={"ticker": "X"},
                                         fill={"ticker": "X", "fill_price": 1.0}) is None
    assert list((sandbox / "state").glob("x_draft_*")) == []


# ------------------------------------------------------------------ wiring

def test_github_x_post_schedule_is_disabled_but_dispatch_remains():
    import re
    wf = (ROOT / ".github" / "workflows" / "x-post.yml").read_text()
    active = "\n".join(l for l in wf.splitlines() if not l.lstrip().startswith("#"))
    assert "workflow_dispatch" in active
    assert not re.search(r"^\s*schedule:", active, re.M)


def test_box_wrapper_exists_and_is_executable():
    w = ROOT / "scripts" / "post_x_daily.sh"
    assert w.exists() and w.stat().st_mode & 0o111
    body = w.read_text()
    assert "tools.x_poster" in body and "[vercel skip]" in body
    assert "push -f" not in body and "--force" not in body.replace("--force-window", "")
