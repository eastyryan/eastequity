"""Live-price feed + secrets loading fixes of 2026-09-28.

  * tools/live_prices._read_from_branch ran `git fetch --depth=1 origin live-data`
    then `git show origin/live-data:...`. In a SINGLE-BRANCH clone (the box
    checkout: remote.origin.fetch covers refs/heads/main only) that fetch only
    writes FETCH_HEAD, origin/live-data never exists, and the read silently
    returned {} — every time.
  * state/live_prices.json was tracked on main (a stale 09-23 snapshot), so a
    post-run `git reset --hard origin/main` restored it over every in-slot
    refresh, and stop-watch's "fall back to live-data if empty" never fired.
  * tools.envload.load_env returned without loading anything when python-dotenv
    was missing, so the system python3 silently lost the Alpaca keys.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import envload  # noqa: E402
from tools import live_prices  # noqa: E402

GIT = shutil.which("git")


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always",
         *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture
def single_branch_clone(tmp_path):
    """origin (bare) with main + an orphan live-data branch carrying the
    snapshot, cloned SINGLE-BRANCH exactly like the box checkout."""
    if not GIT:
        pytest.skip("git not available")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "README").write_text("main\n")
    _git(work, "add", "README")
    _git(work, "commit", "-q", "-m", "main")
    _git(work, "checkout", "-q", "--orphan", "live-data")
    _git(work, "rm", "-rq", "--cached", ".")
    (work / "README").unlink()
    (work / "state").mkdir()
    snap = {"as_of": "2026-09-28T18:00:00+00:00", "source": "alpaca_iex",
            "prices": {"NOW": 131.07}, "n": 1}
    (work / "state" / "live_prices.json").write_text(json.dumps(snap))
    _git(work, "add", "state/live_prices.json")
    _git(work, "commit", "-q", "-m", "live")
    _git(work, "checkout", "-q", "main")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--single-branch", "--branch", "main",
         f"file://{bare}", str(clone))
    return clone, snap


def test_refspec_names_the_remote_tracking_ref():
    assert live_prices._branch_refspec() == \
        "+refs/heads/live-data:refs/remotes/origin/live-data"


def test_the_old_fetch_never_creates_origin_live_data(single_branch_clone):
    """The bug, reproduced: a bare-branch fetch in a single-branch clone leaves
    origin/live-data missing, so `git show origin/live-data:...` fails."""
    clone, _ = single_branch_clone
    _git(clone, "fetch", "-q", "--depth=1", "origin", "live-data")
    r = subprocess.run(["git", "show", "origin/live-data:state/live_prices.json"],
                       cwd=str(clone), capture_output=True, text=True)
    assert r.returncode != 0


def test_branch_read_works_in_a_single_branch_clone(single_branch_clone,
                                                    monkeypatch):
    clone, snap = single_branch_clone
    monkeypatch.setattr(live_prices, "ROOT", clone)

    blob = live_prices._read_from_branch()

    assert blob == snap
    # and the tracking ref now exists for everything else that reads it
    r = subprocess.run(["git", "rev-parse", "--verify", "-q",
                        "refs/remotes/origin/live-data"],
                       cwd=str(clone), capture_output=True, text=True)
    assert r.returncode == 0


def test_branch_read_without_the_branch_is_empty_not_an_error(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(live_prices, "ROOT", tmp_path)   # not even a repo
    assert live_prices._read_from_branch() == {}


def test_live_prices_json_is_not_tracked_on_main():
    if not GIT or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    r = subprocess.run(["git", "ls-files", "--error-unmatch",
                        "state/live_prices.json"], cwd=str(ROOT),
                       capture_output=True, text=True)
    assert r.returncode != 0, "state/live_prices.json must stay untracked"
    r = subprocess.run(["git", "check-ignore", "-q", "state/live_prices.json"],
                       cwd=str(ROOT))
    assert r.returncode == 0, "state/live_prices.json must be gitignored"


# --------------------------------------------------------------------------- #
# envload without python-dotenv
# --------------------------------------------------------------------------- #
def test_parse_env_file_handles_the_real_shapes(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nEE_T_A=plain\nexport EE_T_B=\"quoted value\"\n"
                 "EE_T_C='single'\nEE_T_D=x # trailing\nnot a line\n"
                 "EE_T_URL=https://paper-api.alpaca.markets\n")
    got = envload._parse_env_file(f)
    assert got == {"EE_T_A": "plain", "EE_T_B": "quoted value",
                   "EE_T_C": "single", "EE_T_D": "x",
                   "EE_T_URL": "https://paper-api.alpaca.markets"}


def test_load_env_without_dotenv_still_loads_keys(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("EE_T_KEY=from-file\nEE_T_KEEP=from-file\n")
    monkeypatch.setenv("EAST_EQUITY_ENV", str(f))
    monkeypatch.setenv("EE_T_KEEP", "ambient")
    # register both for restoration, then clear the one we expect to load
    monkeypatch.setenv("EE_T_KEY", "placeholder")
    monkeypatch.delenv("EE_T_KEY")

    real_import = builtins.__import__

    def no_dotenv(name, *a, **k):
        if name == "dotenv" or name.startswith("dotenv."):
            raise ImportError("python-dotenv not installed (simulated)")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_dotenv)

    assert envload.load_env() == f
    assert os.environ["EE_T_KEY"] == "from-file"
    assert os.environ["EE_T_KEEP"] == "ambient", "never override ambient env"
