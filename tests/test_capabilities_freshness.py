"""The staleness detector must not itself be fooled by staleness.

THE BUG, TWICE
--------------
runlib.capabilities probes whether each safety guard is wired AND fed, and a DEAD
probe blocks every new BUY. It therefore has to read the freshest bundle. It has
picked the wrong one twice:

  v1  preferred data/cloud_context.json unconditionally, so it probed a bundle
      produced before the wiring it was checking.
  v2  ranked by file mtime. But the relay file is DOWNLOADED long after its data
      was gathered. Measured 2026-07-19:

          data/cloud_context.json   mtime 14:25 local   run_date 06:20 UTC
          state/context_...a0990f   mtime 12:59 local   run_date 16:59 UTC

      mtime ranked the relay first. Its 06:20 content predated the 13:44 UTC commit
      wiring factor_map, so book_risk_bundle_blocks reported DEAD — blocking every
      new BUY — while the genuinely freshest bundle had all three keys present.

A file's write time is not its data's age. These tests pin that distinction so the
bug cannot recur a third time.
"""

from __future__ import annotations

import json
import os

import pytest

from runlib import capabilities


def _bundle(run_date: str, **extra) -> dict:
    return {"run_date": run_date, "as_of_et": run_date[:10], **extra}


def _write(path, bundle: dict, mtime_epoch: float):
    path.write_text(json.dumps(bundle))
    os.utime(path, (mtime_epoch, mtime_epoch))


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(capabilities, "ROOT", tmp_path)
    return tmp_path


def test_newer_content_beats_newer_mtime(fake_root):
    """THE REGRESSION. Reproduces the exact 2026-07-19 file/mtime arrangement."""
    _write(fake_root / "data" / "cloud_context.json",
           _bundle("2026-07-19T06:20:21+00:00", marker="relay-stale-content"),
           mtime_epoch=1_784_485_516)          # 14:25 local — NEWEST file
    _write(fake_root / "state" / "context_20260719-a0990f.json",
           _bundle("2026-07-19T16:59:42+00:00", marker="local-fresh-content",
                   factor_map={"requires_factor_response": True}),
           mtime_epoch=1_784_480_384)          # 12:59 local — older file

    got = capabilities._newest_context()
    assert got["marker"] == "local-fresh-content", (
        "picked the file with the newest mtime but the oldest DATA — this is the "
        "bug that reported factor_map dead and blocked every BUY")
    assert "factor_map" in got


def test_relay_wins_when_its_content_really_is_newest(fake_root):
    """The fix must not simply invert the old preference."""
    _write(fake_root / "data" / "cloud_context.json",
           _bundle("2026-07-19T20:00:00+00:00", marker="relay-fresh"),
           mtime_epoch=1_784_400_000)          # OLDEST file, newest data
    _write(fake_root / "state" / "context_20260719-aaaaaa.json",
           _bundle("2026-07-19T09:00:00+00:00", marker="local-stale"),
           mtime_epoch=1_784_490_000)
    assert capabilities._newest_context()["marker"] == "relay-fresh"


def test_missing_run_date_falls_back_to_mtime(fake_root):
    """A bundle with no parseable run_date must still be rankable, not dropped."""
    _write(fake_root / "state" / "context_20260719-nodate.json",
           {"marker": "no-run-date"}, mtime_epoch=1_784_490_000)
    _write(fake_root / "state" / "context_20260719-dated.json",
           _bundle("2026-07-19T01:00:00+00:00", marker="dated"),
           mtime_epoch=1_784_400_000)
    assert capabilities._newest_context()["marker"] == "no-run-date"


def test_unparseable_run_date_does_not_crash(fake_root):
    _write(fake_root / "state" / "context_20260719-bad.json",
           {"run_date": "not a timestamp", "marker": "bad-date"},
           mtime_epoch=1_784_490_000)
    assert capabilities._newest_context()["marker"] == "bad-date"


def test_corrupt_file_is_skipped_not_fatal(fake_root):
    (fake_root / "state" / "context_20260719-corrupt.json").write_text("{ broken")
    os.utime(fake_root / "state" / "context_20260719-corrupt.json",
             (1_784_490_000, 1_784_490_000))
    _write(fake_root / "state" / "context_20260719-good.json",
           _bundle("2026-07-19T10:00:00+00:00", marker="good"),
           mtime_epoch=1_784_480_000)
    assert capabilities._newest_context()["marker"] == "good"


def test_no_bundles_returns_empty(fake_root):
    assert capabilities._newest_context() == {}


def test_does_not_parse_the_entire_state_directory(fake_root):
    """The prefilter must bound the work — state/ holds ~190 bundles in prod.

    Sound because run_date <= mtime always: a file's data cannot be newer than the
    moment it was written, so the newest-content bundle is always inside the
    newest-mtime window even when its rank within that window is wrong.
    """
    for i in range(40):
        _write(fake_root / "state" / f"context_2026071{i % 9}-{i:04d}.json",
               _bundle("2026-07-1%d T00:00:00+00:00".replace(" ", "") % (i % 9),
                       marker=f"old-{i}"),
               mtime_epoch=1_784_000_000 + i)
    winner = fake_root / "state" / "context_20260719-winner.json"
    _write(winner, _bundle("2026-07-19T23:00:00+00:00", marker="winner"),
           mtime_epoch=1_784_490_000)

    reads = {"n": 0}
    real_read = type(winner).read_text

    def counting_read(self, *a, **k):
        reads["n"] += 1
        return real_read(self, *a, **k)

    import pathlib
    orig = pathlib.Path.read_text
    pathlib.Path.read_text = counting_read
    try:
        got = capabilities._newest_context()
    finally:
        pathlib.Path.read_text = orig

    assert got["marker"] == "winner"
    assert reads["n"] <= 8, (
        f"parsed {reads['n']} bundles; the mtime prefilter should bound this to 8")


def test_bundle_generated_at_prefers_content_over_file(fake_root):
    path = fake_root / "state" / "context_20260719-x.json"
    _write(path, _bundle("2026-07-19T06:00:00+00:00"), mtime_epoch=1_784_490_000)
    from datetime import datetime, timezone
    expected = datetime(2026, 7, 19, 6, 0, tzinfo=timezone.utc).timestamp()
    assert capabilities.bundle_generated_at(json.loads(path.read_text()),
                                            path) == expected
