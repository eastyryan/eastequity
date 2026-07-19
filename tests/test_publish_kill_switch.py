"""A REMOVED kill switch must propagate, not just an engaged one.

THE INCIDENT
------------
redeploy_dashboard() builds the list of paths it hands to `git add -A`. The
kill switch entry was gated on the file existing:

    if (ROOT / "state" / "KILL_SWITCH").exists():
        paths.append("state/KILL_SWITCH")

while the comment four lines below it read:

    # add -A so a REMOVED kill switch (all-clear) also propagates

Those two statements cannot both be true. On the run where an operator REMOVES
the switch, the file does not exist, so the path never entered the list, so
`git add -A` was never called for it and the deletion never staged. Engaging a
halt propagated to every node; lifting one did not.

It fails SAFE — the cloud stays halted — which is why it survived. But it fails
safe in the direction that misleads an operator worst: the local switch is gone,
the publish reports success, and every remote node is still refusing to run. The
operator believes an all-clear shipped. Absent must never read as all-clear,
which is the same rule the context pack enforces on missing data.

WHAT THIS PINS
--------------
The path list is asserted directly, with the kill switch ABSENT — the only state
in which the bug is observable. A test written against an engaged switch passes
on the broken code and proves nothing, so the negative control here is the
engaged case: it must also be present, or the assertion is just "the list is
never empty".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runlib import publish  # noqa: E402


def _added_paths(monkeypatch, root: Path) -> list[str]:
    """Run redeploy_dashboard against a throwaway ROOT; collect `git add` paths."""
    added: list[str] = []

    def fake_run(cmd, *a, **kw):
        if cmd[:3] == ["git", "add", "-A"]:
            added.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "commit"]:
            # Non-zero => "no data changes to publish" => return before pushing.
            # The add loop has already run, which is what this test measures.
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish, "ROOT", root)
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    publish.redeploy_dashboard()
    return added


def test_a_removed_kill_switch_is_still_staged(monkeypatch, tmp_path):
    """THE BUG. With no KILL_SWITCH on disk the path must STILL be staged.

    This is the only state that distinguishes the fix from the bug: the old
    code appended the path only when the file existed, so with it absent the
    deletion could never reach the remote nodes.
    """
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    assert not (tmp_path / "state" / "KILL_SWITCH").exists(), "fixture precondition"

    added = _added_paths(monkeypatch, tmp_path)

    assert "state/KILL_SWITCH" in added, (
        "an all-clear cannot propagate: the kill switch path was not handed to "
        "`git add -A`, so its DELETION never stages and every cloud node stays "
        "halted while the publish reports success")


def test_an_engaged_kill_switch_is_staged_too(monkeypatch, tmp_path):
    """NEGATIVE CONTROL for the test above.

    If the path were staged only when absent, the test above would pass while
    engaging a halt silently stopped propagating — trading the old bug for its
    mirror image. Both states must stage.
    """
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "KILL_SWITCH").write_text("halt")

    added = _added_paths(monkeypatch, tmp_path)

    assert "state/KILL_SWITCH" in added, (
        "engaging a halt no longer propagates — the fix inverted the bug")


def test_the_kill_switch_path_is_not_conditioned_on_the_file_existing():
    """Guard the guard, at the source.

    The two tests above exercise behaviour; this one pins the SHAPE, because the
    regression is a one-line `if` that reads perfectly reasonable in review and
    whose effect is invisible in every state except the one that matters.
    """
    src = (Path(publish.__file__)).read_text()
    marker = 'if (ROOT / "state" / "KILL_SWITCH").exists():'
    assert marker not in src, (
        "the kill switch path is gated on the file existing again — that is the "
        "original bug: the removal case is exactly when the file is absent")


def test_the_ledger_and_order_intents_still_publish(monkeypatch, tmp_path):
    """The kill switch shares its path list with the authoritative ledger and the
    queued-order file whose push TRIGGERS the Actions executor. Editing that list
    must not drop either of them."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    added = _added_paths(monkeypatch, tmp_path)
    for required in ("state/portfolio.json", "state/order_intents.json",
                     "dashboard/data", "journal"):
        assert required in added, f"{required} fell out of the publish path list"
