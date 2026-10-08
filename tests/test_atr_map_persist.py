"""Offline tests for ATR map persistence / backfill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import atr_map


def test_persist_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    n = atr_map.persist_atr_map({"amd": 2.5, "NVDA": 3.1}, source="test")
    assert n == 2
    loaded = atr_map.load_atr_map()
    assert loaded == {"AMD": 2.5, "NVDA": 3.1}


def test_ensure_backfills_empty_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    atr_map.persist_atr_map({"DELL": 4.0})
    scan = {"status": "light", "atr_by_ticker": {}}
    out = atr_map.ensure_atr_on_scan(scan)
    assert out["DELL"] == 4.0
    assert scan["atr_by_ticker"]["DELL"] == 4.0
    assert "backfilled" in scan.get("atr_map_note", "")


def test_ensure_persists_nonempty_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    scan = {"status": "ok", "atr_by_ticker": {"AAPL": 1.2}}
    atr_map.ensure_atr_on_scan(scan)
    blob = json.loads((tmp_path / "atr_map.json").read_text())
    assert blob["atr_by_ticker"]["AAPL"] == 1.2


# --------------------------------------------------------------------------- #
# Carry-forward (2026-10-08). The ~06:00/06:50 ET light gathers committed bundles
# with atr_by_ticker {} on fresh runners (no state/atr_map.json), so from ~06:00
# to the ~09:00 gather stop-watch's trail had no ATR and the WIRING capability
# test failed every morning. Light scans now carry the last measured map
# forward, labelled with its original as-of and source.
# --------------------------------------------------------------------------- #
from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 10, 8, 10, 4, tzinfo=timezone.utc)   # 06:04 ET light gather


def _bundle(path, scan, *, run_date="2026-10-08T04:16:06+00:00",
            depth="holdings_watchlist"):
    path.write_text(json.dumps({"run_date": run_date, "run_depth": depth,
                                "universe_scan": scan}))
    return path


def _light_scan():
    return {"status": "light", "note": "light run - no universe scan this cycle",
            "top_setups": [], "prices": {"NTRA": 398.0}, "atr_by_ticker": {}}


def test_light_scan_carries_prior_bundle_atr_with_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "missing.json")  # fresh runner
    prior = _bundle(tmp_path / "cloud_context.json",
                    {"status": "ok", "atr_by_ticker": {"NTRA": 4.29, "okta": 4.28},
                     "scan_fetched_at_utc": "2026-10-08T04:15:00+00:00"})
    scan = _light_scan()

    label = atr_map.carry_forward_atr(scan, prior_bundle=prior, now=NOW)

    assert scan["atr_by_ticker"] == {"NTRA": 4.29, "OKTA": 4.28}
    cf = scan["atr_carried_forward"]
    assert cf == label
    assert cf["as_of"] == "2026-10-08T04:15:00+00:00", "original scan time, not now"
    assert "cloud_context.json" in cf["source"] and "holdings_watchlist" in cf["source"]
    assert cf["age_hours"] == 5.8 and cf["n"] == 2
    assert "carried forward" in scan["atr_map_note"]


def test_chained_carry_keeps_the_original_measurement_time(tmp_path, monkeypatch):
    """06:50 light run after the 06:04 light run: still labelled with the 00:15
    measurement, never re-dated to the intermediate carry."""
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "missing.json")
    first = _bundle(tmp_path / "a.json",
                    {"status": "ok", "atr_by_ticker": {"NTRA": 4.29},
                     "scan_fetched_at_utc": "2026-10-08T04:15:00+00:00"})
    s1 = _light_scan()
    atr_map.carry_forward_atr(s1, prior_bundle=first, now=NOW)
    second = _bundle(tmp_path / "b.json", s1, run_date=NOW.isoformat(), depth="light")
    s2 = _light_scan()

    atr_map.carry_forward_atr(s2, prior_bundle=second,
                              now=datetime(2026, 10, 8, 10, 50, tzinfo=timezone.utc))

    assert s2["atr_by_ticker"] == {"NTRA": 4.29}
    assert s2["atr_carried_forward"]["as_of"] == "2026-10-08T04:15:00+00:00"
    assert "holdings_watchlist" in s2["atr_carried_forward"]["source"]


def test_newer_of_persisted_map_and_prior_bundle_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    atr_map.persist_atr_map({"NTRA": 9.9}, source="ok",
                            as_of="2026-10-07T20:00:00+00:00")
    prior = _bundle(tmp_path / "cloud_context.json",
                    {"status": "ok", "atr_by_ticker": {"NTRA": 4.29},
                     "scan_fetched_at_utc": "2026-10-08T04:15:00+00:00"})
    scan = _light_scan()
    atr_map.carry_forward_atr(scan, prior_bundle=prior, now=NOW)
    assert scan["atr_by_ticker"] == {"NTRA": 4.29}

    atr_map.persist_atr_map({"NTRA": 5.5}, source="ok",
                            as_of="2026-10-08T09:00:00+00:00")
    scan = _light_scan()
    atr_map.carry_forward_atr(scan, prior_bundle=prior, now=NOW)
    assert scan["atr_by_ticker"] == {"NTRA": 5.5}
    assert scan["atr_carried_forward"]["source"].startswith("state/atr_map.json")


def test_too_old_map_is_not_carried(tmp_path, monkeypatch):
    """A dead scanner must still show up as an EMPTY map, not hide behind old
    numbers forever."""
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "missing.json")
    prior = _bundle(tmp_path / "cloud_context.json",
                    {"status": "ok", "atr_by_ticker": {"NTRA": 4.29},
                     "scan_fetched_at_utc": "2026-09-20T04:15:00+00:00"})
    scan = _light_scan()
    assert atr_map.carry_forward_atr(scan, prior_bundle=prior, now=NOW) == {}
    assert scan["atr_by_ticker"] == {} and "atr_carried_forward" not in scan


def test_undated_or_empty_sources_are_not_carried(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "missing.json")
    undated = tmp_path / "u.json"
    undated.write_text(json.dumps({"universe_scan": {"atr_by_ticker": {"NTRA": 4.0}}}))
    scan = _light_scan()
    assert atr_map.carry_forward_atr(scan, prior_bundle=undated, now=NOW) == {}
    assert atr_map.carry_forward_atr(scan, prior_bundle=tmp_path / "nope.json",
                                     now=NOW) == {}
    assert scan["atr_by_ticker"] == {}


def test_a_scan_that_measured_atr_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "missing.json")
    prior = _bundle(tmp_path / "cloud_context.json",
                    {"status": "ok", "atr_by_ticker": {"NTRA": 4.29},
                     "scan_fetched_at_utc": "2026-10-08T04:15:00+00:00"})
    scan = {"status": "ok", "atr_by_ticker": {"NTRA": 3.0}}
    assert atr_map.carry_forward_atr(scan, prior_bundle=prior, now=NOW) == {}
    assert scan == {"status": "ok", "atr_by_ticker": {"NTRA": 3.0}}


def test_re_persisting_a_carried_map_keeps_its_original_as_of(tmp_path, monkeypatch):
    """brain_io.ensure_atr_on_scan persists whatever map the scan carries; a
    carried map must not be re-stamped as a fresh measurement."""
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "atr_map.json")
    scan = {"status": "light", "atr_by_ticker": {"NTRA": 4.29},
            "atr_carried_forward": {"as_of": "2026-10-08T04:15:00+00:00",
                                    "source": "cloud_context.json (holdings_watchlist scan)"}}
    atr_map.ensure_atr_on_scan(scan)
    blob = json.loads((tmp_path / "atr_map.json").read_text())
    assert blob["as_of"] == "2026-10-08T04:15:00+00:00"
    assert "holdings_watchlist" in blob["source"]


def test_stop_watch_reads_atr_from_a_carried_light_bundle(tmp_path, monkeypatch):
    """End to end on the consumer the capability probe checks: a fresh runner
    (no state/atr_map.json) whose committed bundle is a carried light bundle."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import stop_watch
    monkeypatch.setattr(atr_map, "ATR_FILE", tmp_path / "state" / "atr_map.json")
    monkeypatch.setattr(stop_watch, "ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    prior = _bundle(tmp_path / "prior.json",
                    {"status": "ok", "atr_by_ticker": {"NTRA": 4.29},
                     "scan_fetched_at_utc": "2026-10-08T04:15:00+00:00"})
    scan = _light_scan()
    atr_map.carry_forward_atr(scan, prior_bundle=prior, now=NOW)
    _bundle(tmp_path / "data" / "cloud_context.json", scan,
            run_date=NOW.isoformat(), depth="light")

    assert stop_watch._load_atr_map() == {"NTRA": 4.29}
    blob = json.loads((tmp_path / "state" / "atr_map.json").read_text())
    assert blob["as_of"] == "2026-10-08T04:15:00+00:00"


def test_gather_context_wires_the_carry_forward():
    """A helper nobody calls is decoration (reconciles_with_ledger, 2026-07)."""
    import inspect
    from runlib import context_gather
    src = inspect.getsource(context_gather.gather_context)
    assert "carry_forward_atr(" in src
    assert src.index("carry_forward_atr(") < src.index("_reprice_held_and_mark(")
