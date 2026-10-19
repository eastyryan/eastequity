"""Commit the working tree to origin/main via the GitHub API (no git push).

The runtime's git transport cannot push (SSH blocked, HTTPS surrogate 401),
so run outputs are committed through the REST Git Database API using the
stored custom.github credential. One commit per call, fast-forward only.

Usage:
    python tools/muse_commit.py "Commit message [vercel skip]"

Commits every modified/new/deleted file that `git status --porcelain` reports
(ignored files like .venv and node_modules are already excluded by git).
Aborts without committing if origin/main moved under us.
"""
import base64
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH_API = Path.home() / "workspace/custom-connectors/custom.legacy-github/bin/gh-api"
PYPATH = "/opt/muse/skills/custom-connector-setup/bin"
OWNER_REPO = "eastyryan/eastequity"
BRANCH = "main"


def gh(method: str, path: str, data: dict | None = None) -> dict:
    cmd = ["env", f"PYTHONPATH={PYPATH}", str(GH_API), method, path]
    if data is not None:
        cmd += ["--data", json.dumps(data)]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"gh-api {method} {path} failed: {r.stdout[:300]} {r.stderr[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def main() -> int:
    message = sys.argv[1] if len(sys.argv) > 1 else "East Equity run [vercel skip]"
    # Current local changes.
    st = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                        text=True, cwd=str(ROOT))
    entries = []
    for line in st.stdout.splitlines():
        code, path = line[:2], line[3:].strip().strip('"')
        if code.strip() in ("M", "A") or "M" in code or "A" in code:
            entries.append(("upsert", path))
        elif "D" in code:
            entries.append(("delete", path))
    # Untracked files show as "??".
    entries = [(a, p) for a, p in entries]
    for line in st.stdout.splitlines():
        if line.startswith("??"):
            p = line[3:].strip().strip('"')
            if Path(ROOT, p).is_file():
                entries.append(("upsert", p))
    if not entries:
        print("muse_commit: nothing to commit")
        return 0

    ref = gh("GET", f"/repos/{OWNER_REPO}/git/ref/heads/{BRANCH}")
    base_sha = ref["object"]["sha"]
    base_commit = gh("GET", f"/repos/{OWNER_REPO}/git/commits/{base_sha}")
    base_tree = base_commit["tree"]["sha"]

    tree_entries = []
    for action, path in entries:
        if action == "delete":
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        raw = Path(ROOT, path).read_bytes()
        try:
            text = raw.decode("utf-8")
            blob = gh("POST", f"/repos/{OWNER_REPO}/git/blobs",
                      {"content": text, "encoding": "utf-8"})
        except UnicodeDecodeError:
            blob = gh("POST", f"/repos/{OWNER_REPO}/git/blobs",
                      {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        tree_entries.append({"path": path, "mode": "100644", "type": "blob",
                             "sha": blob["sha"]})
    tree = gh("POST", f"/repos/{OWNER_REPO}/git/trees",
              {"base_tree": base_tree, "tree": tree_entries})
    commit = gh("POST", f"/repos/{OWNER_REPO}/git/commits",
                {"message": message, "tree": tree["sha"], "parents": [base_sha]})
    try:
        gh("PATCH", f"/repos/{OWNER_REPO}/git/ref/heads/{BRANCH}",
           {"sha": commit["sha"]})
    except RuntimeError as e:
        print(f"muse_commit: ref update failed (origin moved?): {e}")
        print(f"  orphan commit {commit['sha']} created but not landed")
        return 3
    print(f"muse_commit: landed {commit['sha'][:8]} ({len(entries)} files)")
    # Re-sync the local clone to what we just landed.
    subprocess.run(["git", "fetch", "origin", BRANCH, "--depth=1", "--quiet"],
                   cwd=str(ROOT), timeout=120)
    subprocess.run(["git", "reset", "--hard", f"origin/{BRANCH}", "--quiet"],
                   cwd=str(ROOT), timeout=60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
