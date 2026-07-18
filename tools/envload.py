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


def load_env() -> Path | None:
    """Load the resolved secrets file into os.environ and return the path used.

    Never raises: a missing file, an unreadable file, or an absent python-dotenv all
    degrade to "rely on the ambient environment", which is the correct behavior in CI.
    """
    path = env_path()
    if path is None:
        return None
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
    except Exception:
        return None
    return path


def using_legacy_location() -> bool:
    """True when secrets are still inside the repo working directory — i.e. still
    reachable by the brain's Glob/Grep. Surfaced by tests and the preflight warning
    so a half-finished migration cannot go quiet."""
    return env_path() == LEGACY_PATH
