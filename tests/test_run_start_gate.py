"""Only a cloud trading-routine gather marks a run start — not the bundle Action.

The breadcrumb's whole value is the died-vs-missed distinction: a start with no summary
is a death. The hourly GitHub bundle-refresh Action runs the SAME
`--gather-only --auto-depth` command ~7x/day; if it marked starts, those slots would
show phantom "starts" no brain run backs, and a real death would hide in the noise. The
discriminator is GITHUB_ACTIONS, which GitHub always sets and the CCR sandbox never does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator  # noqa: E402


def test_cloud_routine_gather_marks(monkeypatch):
    """--auto-depth, no GITHUB_ACTIONS → the cloud routine → mark."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert orchestrator._should_mark_run_start(True) is True


def test_github_bundle_action_does_not_mark(monkeypatch):
    """--auto-depth WITH GITHUB_ACTIONS → the hourly bundle refresh → do NOT mark."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert orchestrator._should_mark_run_start(True) is False


def test_non_auto_depth_never_marks(monkeypatch):
    """A plain --gather-only (no slot semantics) is not a trading run — never mark."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert orchestrator._should_mark_run_start(False) is False
