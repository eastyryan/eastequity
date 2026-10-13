"""2026-10-13 08:45 slot (09:12 ET): the lease rebase's autostash conflicted.

The gather had broker-synced state/portfolio.json, and so had origin (stop-watch).
Only the price marks differed, but the act crashed on conflict markers. The lease
rebase now resolves such conflicts deterministically: the newer broker sync wins,
journals are unioned, and anything else keeps the local copy.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib import preflight  # noqa: E402


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _book(synced, price):
    return json.dumps({"broker_synced_at": synced, "cash_usd": 519.8,
                       "positions": [{"ticker": "NOW", "quantity": 0.791987,
                                      "last_price": price}]}, indent=2) + "\n"


def _repos(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    a, b = tmp_path / "run", tmp_path / "stopwatch"
    for d in (a, b):
        _git(tmp_path, "clone", "-q", str(origin), str(d))
        _git(d, "config", "user.email", "t@t")
        _git(d, "config", "user.name", "t")
    (a / "state").mkdir()
    (a / "journal").mkdir()
    (a / "state" / "portfolio.json").write_text(_book("2026-10-13T12:00:00+00:00", 139.0))
    (a / "journal" / "rej.jsonl").write_text('{"a": 1}\n')
    (a / "universe.json").write_text("v1")
    _git(a, "add", ".")
    _git(a, "commit", "-qm", "seed")
    _git(a, "push", "-q", "origin", "main")
    _git(b, "pull", "-q", "origin", "main")
    return a, b


def _race(tmp_path, monkeypatch, *, local_synced, upstream_synced):
    a, b = _repos(tmp_path)
    # Stop-watch on the other node re-syncs and pushes first.
    (b / "state" / "portfolio.json").write_text(_book(upstream_synced, 140.5))
    (b / "journal" / "rej.jsonl").write_text('{"a": 1}\n{"sw": 2}\n')
    _git(b, "commit", "-qam", "stop watch")
    _git(b, "push", "-q", "origin", "main")
    # The gather re-syncs locally (dirty), and the act commits its lease.
    (a / "state" / "portfolio.json").write_text(_book(local_synced, 140.93))
    (a / "journal" / "rej.jsonl").write_text('{"a": 1}\n{"gather": 3}\n')
    (a / "universe.json").write_text("v2-gather")
    (a / "state" / "RUN_LEASE.json").write_text("{}")
    _git(a, "add", "state/RUN_LEASE.json")
    _git(a, "commit", "-qm", "Acquire run lease")
    monkeypatch.setattr(preflight, "ROOT", a)
    rb = preflight._pull_rebase_autostash()
    return a, rb


def _clean(a):
    assert _git(a, "diff", "--name-only", "--diff-filter=U") == ""
    assert "autostash" not in _git(a, "stash", "list").lower()
    for f in ("state/portfolio.json", "journal/rej.jsonl"):
        assert "<<<<<<<" not in (a / f).read_text()


def test_newer_local_broker_sync_wins(tmp_path, monkeypatch):
    a, rb = _race(tmp_path, monkeypatch,
                  local_synced="2026-10-13T13:12:20+00:00",
                  upstream_synced="2026-10-13T13:05:00+00:00")
    assert rb.returncode == 0, rb.stderr
    _clean(a)
    book = json.loads((a / "state" / "portfolio.json").read_text())
    assert book["broker_synced_at"] == "2026-10-13T13:12:20+00:00"
    assert book["positions"][0]["last_price"] == 140.93
    assert (a / "universe.json").read_text() == "v2-gather"
    lines = (a / "journal" / "rej.jsonl").read_text().splitlines()
    assert lines == ['{"a": 1}', '{"sw": 2}', '{"gather": 3}']
    _git(a, "push", "-q", "origin", "main")          # the lease publishes


def test_newer_upstream_broker_sync_wins(tmp_path, monkeypatch):
    a, rb = _race(tmp_path, monkeypatch,
                  local_synced="2026-10-13T13:00:00+00:00",
                  upstream_synced="2026-10-13T13:10:00+00:00")
    assert rb.returncode == 0, rb.stderr
    _clean(a)
    book = json.loads((a / "state" / "portfolio.json").read_text())
    assert book["broker_synced_at"] == "2026-10-13T13:10:00+00:00"
    assert book["positions"][0]["last_price"] == 140.5


def test_resolver_rules_are_pure():
    r = preflight._resolve_autostash_conflict
    assert r("x.json", b"L", None) == b"L"
    assert r("x.json", None, b"U") == b"U"
    assert r("data/x.json", b"L", b"U") == b"L"
    assert r("j.jsonl", b'{"a":1}\n{"b":2}', b'{"a":1}\n{"c":3}\n') == \
        b'{"a":1}\n{"c":3}\n{"b":2}\n'


def test_no_conflict_leaves_everything_alone(tmp_path, monkeypatch):
    a, b = _repos(tmp_path)
    (b / "universe.json").write_text("v1-upstream-other")
    _git(b, "commit", "-qam", "unrelated")
    _git(b, "push", "-q", "origin", "main")
    (a / "state" / "portfolio.json").write_text(_book("2026-10-13T13:12:20+00:00", 141))
    (a / "state" / "RUN_LEASE.json").write_text("{}")
    _git(a, "add", "state/RUN_LEASE.json")
    _git(a, "commit", "-qm", "Acquire run lease")
    monkeypatch.setattr(preflight, "ROOT", a)
    assert preflight._pull_rebase_autostash().returncode == 0
    assert json.loads((a / "state" / "portfolio.json").read_text())["positions"][0][
        "last_price"] == 141
    assert (a / "universe.json").read_text() == "v1-upstream-other"
