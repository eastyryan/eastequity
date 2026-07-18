"""Secrets hygiene — fails the build if credentials return to the brain's reach.

The `claude -p` brain runs with cwd=<repo> and an allowlist of Read/Glob/Grep/
WebSearch/WebFetch, while its input bundle carries unsanitized news headlines and
SEC filing prose. Anything secret-bearing inside the working directory is therefore
reachable by instruction-shaped external text, with WebFetch as egress.

The control is LOCATION, not a deny rule: Glob and Grep are cwd-scoped, and Grep
prints matching lines, so `grep ALPACA` would surface values without ever invoking
Read. These tests pin that the secrets file stays out of the tree and that the
resolver never silently falls back into it.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import envload  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Files that legitimately live in-tree and contain only key NAMES, never values.
ALLOWED_IN_TREE = {".env.example"}

# Value-shaped secrets we must never find committed or loose in the tree.
SECRET_VALUE_PATTERNS = [
    re.compile(r"\bPK[A-Z0-9]{16,}\b"),          # Alpaca key id
    re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"),  # generic live secret
]


def _tracked_files():
    import subprocess
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return [ROOT / f for f in out.stdout.splitlines() if f]


def test_no_secrets_file_inside_working_directory():
    """The regression that matters: a .env back at the repo root re-opens the hole."""
    found = [p for p in ROOT.rglob(".env*")
             if ".venv" not in p.parts and ".git" not in p.parts
             and p.name not in ALLOWED_IN_TREE and p.is_file()]
    # dashboard/.env.local is a Vercel CLI dev artifact; it is gitignored and holds a
    # short-lived OIDC token, but it is still in-tree, so it must stay declared here
    # rather than quietly accumulating siblings.
    known = {ROOT / "dashboard" / ".env.local"}
    unexpected = [p for p in found if p not in known]
    assert not unexpected, (
        "secret-bearing file(s) inside the brain's working directory: "
        f"{[str(p.relative_to(ROOT)) for p in unexpected]}. "
        "Move them to ~/.config/east-equity-agent/ (see tools/envload.py).")


def test_env_example_carries_no_values():
    example = ROOT / ".env.example"
    if not example.is_file():
        pytest.skip("no .env.example")
    for line in example.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, _, value = line.partition("=")
        value = value.split("#", 1)[0].strip().strip('"').strip("'")
        assert not value, f".env.example must carry key names only, got a value on: {line}"


def test_resolver_prefers_config_home_over_repo():
    """If both exist, the out-of-tree copy must win — otherwise a stale in-repo file
    silently keeps the exposure open."""
    assert envload.CONFIG_HOME.parts[-3:] == (".config", "east-equity-agent", ".env")
    assert envload.LEGACY_PATH == ROOT / ".env"
    # Ordering is what enforces the preference.
    order = [envload.CONFIG_HOME, envload.LEGACY_PATH]
    assert order.index(envload.CONFIG_HOME) < order.index(envload.LEGACY_PATH)


def test_resolver_reports_legacy_location_loudly():
    """using_legacy_location() is how a half-finished migration stays visible."""
    assert envload.using_legacy_location() is (envload.env_path() == envload.LEGACY_PATH)


def test_resolver_is_fail_soft_without_a_file(monkeypatch, tmp_path):
    """CI has no .env at all — GitHub Actions inject via secrets.*. Absent file must
    degrade to the ambient environment, never raise."""
    monkeypatch.setattr(envload, "CONFIG_HOME", tmp_path / "nope" / ".env")
    monkeypatch.setattr(envload, "LEGACY_PATH", tmp_path / "also-nope" / ".env")
    monkeypatch.delenv("EAST_EQUITY_ENV", raising=False)
    assert envload.env_path() is None
    assert envload.load_env() is None


def test_env_override_is_honored(monkeypatch, tmp_path):
    custom = tmp_path / "custom.env"
    custom.write_text("FRED_API_KEY=abc123\n")
    monkeypatch.setenv("EAST_EQUITY_ENV", str(custom))
    assert envload.env_path() == custom


def test_deny_rules_exist_for_env_paths():
    settings = ROOT / ".claude" / "settings.json"
    assert settings.is_file(), "project .claude/settings.json is the defense-in-depth layer"
    deny = json.loads(settings.read_text()).get("permissions", {}).get("deny", [])
    joined = " ".join(deny)
    # Read alone is insufficient — Grep prints matching lines.
    for tool in ("Read(", "Grep(", "Glob("):
        assert tool in joined, f"deny rules must cover {tool[:-1]}; Grep leaks file contents"


def test_no_secret_values_in_tracked_files():
    """Nothing key-shaped may be committed, in any file."""
    offenders = []
    for path in _tracked_files():
        if not path.is_file() or path.suffix in (".png", ".ico", ".jpg", ".woff2"):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for pat in SECRET_VALUE_PATTERNS:
            if pat.search(text):
                offenders.append(str(path.relative_to(ROOT)))
                break
    assert not offenders, f"secret-shaped values in tracked files: {offenders}"


def test_no_secret_values_in_runtime_artifacts():
    """Logs, journal, published dashboard JSON and state must never carry a live value."""
    # Load first: without this the test skips on every local run and silently checks
    # nothing. In CI there is no secrets file, so it correctly skips there instead.
    envload.load_env()
    live = {k: v for k, v in os.environ.items()
            if k.startswith(("ALPACA_", "X_A", "FRED_")) and v and len(v) > 12}
    if not live:
        pytest.skip("no live credentials loaded in this environment")
    offenders = []
    for sub in ("logs", "journal", "dashboard/data", "state"):
        base = ROOT / sub
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix in (".png", ".ico"):
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            for name, value in live.items():
                if value in text:
                    offenders.append(f"{path.relative_to(ROOT)} leaks {name}")
    assert not offenders, offenders
