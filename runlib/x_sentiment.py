"""Pre-market X sentiment note -> soft context for the brain.

A weekday 8:40am ET routine reads ~20 public X accounts
(data/x_sentiment_accounts.json) and writes state/x_sentiment_YYYYMMDD.md plus
state/x_sentiment_latest.md. This module reads the latest note and hands it to
the brain under the context key `x_sentiment`.

SOFT CONTEXT ONLY. The note summarises public posts; it is not verified data,
not a trade signal on its own, and never overrides a validator rule or a hard
limit. It is read fail-soft: a missing, unreadable or empty file yields
status "absent" and never raises (the GitHub Actions gather-data runner does
not have the file at all). A note older than STALE_AFTER_HOURS is still
handed over but labelled "stale" so yesterday's mood is not read as today's.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - tzdata missing
    _ET = timezone.utc

ROOT = Path(__file__).resolve().parent.parent
X_SENTIMENT_PATH = ROOT / "state" / "x_sentiment_latest.md"
STALE_AFTER_HOURS = 20.0
MAX_TEXT_CHARS = 6000
NOTE = "Soft context from public X posts; not a trade signal on its own."


def load_x_sentiment(path: Path | str | None = None, *, now: datetime | None = None,
                     stale_after_hours: float = STALE_AFTER_HOURS,
                     max_chars: int = MAX_TEXT_CHARS) -> dict:
    """Return the `x_sentiment` context block. Never raises."""
    p = Path(path) if path is not None else X_SENTIMENT_PATH
    block: dict = {"status": "absent", "as_of": None, "age_hours": None,
                   "text": None, "note": NOTE, "source_path": _rel(p)}
    try:
        if not p.is_file():
            return block
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return block
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        now_utc = (now or datetime.now(timezone.utc))
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        age_h = max(0.0, (now_utc - mtime).total_seconds() / 3600.0)
        full_chars = len(text)
        if full_chars > max_chars:
            text = (text[:max_chars]
                    + f"\n\n[TRUNCATED for the brain pack: {full_chars - max_chars} "
                      f"more characters in {_rel(p)}.]")
            block["truncated"] = True
            block["full_chars"] = full_chars
        block.update({
            "status": "stale" if age_h > stale_after_hours else "present",
            "as_of": mtime.astimezone(_ET).isoformat(timespec="seconds"),
            "age_hours": round(age_h, 1),
            "text": text,
        })
        return block
    except Exception as e:  # fail-soft by contract
        block["status"] = "absent"
        block["error"] = str(e)[:200]
        return block


def attach_x_sentiment(context: dict, path: Path | str | None = None) -> dict:
    """Set context['x_sentiment'] in place. Never raises; returns the block."""
    try:
        blk = load_x_sentiment(path)
    except Exception as e:  # belt and braces: load_x_sentiment never raises
        blk = {"status": "absent", "as_of": None, "age_hours": None,
               "text": None, "note": NOTE, "error": str(e)[:200]}
    try:
        context["x_sentiment"] = blk
    except Exception:
        pass
    return blk


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except Exception:
        return str(p)
