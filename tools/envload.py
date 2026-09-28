"""Single resolver for where this system's secrets live.

Why this exists: the brain runs as `claude -p` with `cwd=ROOT` and an allowlist of
`Read Glob Grep WebSearch WebFetch`. While `.env` sat at the repo root it was inside
that working directory, holding live broker credentials and write-capable X tokens —
readable by a model whose input includes unsanitized news headlines and SEC filing
prose, with WebFetch available as egress. A deny rule on Read alone is not sufficient:
Grep prints matching lines, so `grep ALPACA` would surface the values without ever
calling Read.

So the file moves OUT of the working directory. `Glob`/`Grep` are scoped to cwd, which
makes relocation a stronger control than any per-tool rule. `.claude/settings.json`
deny rules are kept as defense in depth.

Resolution order (first hit wins):
  1. $EAST_EQUITY_ENV                      - explicit override
  2. ~/.config/east-equity-agent/.env      - the intended home
  3. <repo>/.env                           - legacy fallback, so a half-migrated
                                             checkout still runs

CI/cloud runs need none of this: GitHub Actions inject ALPACA_* via `secrets.*`, and
python-dotenv never overrides an already-set environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG_HOME = Path.home() / ".config" / "east-equity-agent" / ".env"
LEGACY_PATH = ROOT / ".env"


def env_path() -> Path | None:
    """First existing secrets file, or None when running on injected env vars. Pure."""
    override = os.environ.get("EAST_EQUITY_ENV")
    candidates = ([Path(override).expanduser()] if override else []) + [
        CONFIG_HOME, LEGACY_PATH]
    for p in candidates:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser for when python-dotenv is not importable. Pure.

    Handles what these files actually contain: KEY=VALUE lines, blank lines,
    `#` comments, an optional leading `export `, and single/double-quoted values
    (an unquoted value loses a trailing ` # comment`). No interpolation."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            hash_at = val.find(" #")
            if hash_at >= 0:
                val = val[:hash_at].rstrip()
        out[key] = val
    return out


def load_env() -> Path | None:
    """Load the resolved secrets file into os.environ and return the path used.

    Never raises: a missing or unreadable file degrades to "rely on the ambient
    environment", which is the correct behavior in CI. Like python-dotenv, an
    already-set environment variable is never overridden.

    python-dotenv ABSENT no longer means "silently load nothing" (fixed
    2026-09-28): a script run under the system python3 (no venv) used to drop
    the Alpaca keys on the floor and then behave as a keyless node. The file is
    parsed by hand instead.
    """
    path = env_path()
    if path is None:
        return None
    try:
        from dotenv import load_dotenv
    except Exception:
        load_dotenv = None
    try:
        if load_dotenv is not None:
            load_dotenv(path)
        else:
            for k, v in _parse_env_file(path).items():
                os.environ.setdefault(k, v)
    except Exception:
        return None
    return path


def using_legacy_location() -> bool:
    """True when secrets are still inside the repo working directory — i.e. still
    reachable by the brain's Glob/Grep. Surfaced by tests and the preflight warning
    so a half-finished migration cannot go quiet."""
    return env_path() == LEGACY_PATH
