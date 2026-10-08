"""Persisted ATR% map for chandelier trail + stop gap model between gathers.

The scanner writes atr_by_ticker into the in-memory bundle, but light /
evening depths leave it empty and stop_watch historically read a file that
never existed. Persist the last good map to state/atr_map.json whenever a
gather produces one, and read it back when the live bundle is empty so the
trail degrades loudly instead of silently.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATR_FILE = ROOT / "state" / "atr_map.json"


def persist_atr_map(atr_by_ticker: dict | None, *, source: str = "universe_scan",
                    as_of: str | None = None) -> int:
    """Write a non-empty atr map. Returns number of tickers persisted. Fail-soft.

    `as_of` defaults to now; pass the ORIGINAL scan time when re-persisting a
    carried-forward map so a copy never relabels stale figures as fresh."""
    atr = {str(k).upper(): float(v)
           for k, v in (atr_by_ticker or {}).items()
           if v is not None}
    if not atr:
        return 0
    try:
        ATR_FILE.parent.mkdir(parents=True, exist_ok=True)
        ATR_FILE.write_text(json.dumps({
            "as_of": as_of or datetime.now(timezone.utc).isoformat(),
            "source": source,
            "n": len(atr),
            "atr_by_ticker": atr,
        }, indent=2))
    except Exception:
        return 0
    return len(atr)


def load_atr_map() -> dict:
    """{TICKER: atr_pct} from state/atr_map.json, or {}."""
    try:
        blob = json.loads(ATR_FILE.read_text())
        atr = (blob or {}).get("atr_by_ticker") or {}
        return {str(k).upper(): float(v) for k, v in atr.items() if v is not None}
    except Exception:
        return {}


def ensure_atr_on_scan(scan: dict | None) -> dict:
    """If scan.atr_by_ticker is empty, backfill from the persisted map.
    Persist whenever the scan already has values. Returns the atr map in use.
    """
    scan = scan if isinstance(scan, dict) else {}
    atr = scan.get("atr_by_ticker") or {}
    if atr:
        cf = scan.get("atr_carried_forward")
        if isinstance(cf, dict):
            # Carried forward from an earlier scan: re-persist with the ORIGINAL
            # provenance, never as a fresh measurement.
            persist_atr_map(atr, source=str(cf.get("source") or "carried_forward"),
                            as_of=cf.get("as_of"))
        else:
            persist_atr_map(atr, source=str(scan.get("status") or "universe_scan"))
        return {str(k).upper(): v for k, v in atr.items()}
    saved = load_atr_map()
    if saved:
        scan["atr_by_ticker"] = saved
        meta = _persisted_meta()
        if meta:
            scan.setdefault("atr_carried_forward", meta)
        scan.setdefault(
            "atr_map_note",
            "atr_by_ticker backfilled from state/atr_map.json (last full/mini scan); "
            "trail can ratchet but figures may be a session stale",
        )
    return saved


# --------------------------------------------------------------------------- #
# Carry-forward for scans that measured no ATR (2026-10-08)
#
# THE GAP. The ~06:00 and ~06:50 ET pre-market gathers run at depth `light`,
# which builds `universe_scan` with `atr_by_ticker: {}`. gather-data.yml commits
# that bundle as data/cloud_context.json on a FRESH runner, where the gitignored
# state/atr_map.json does not exist, so nothing backfilled it. From ~06:00 until
# the ~09:00 holdings_watchlist gather, every committed bundle had no ATR:
# stop-watch's chandelier trail could not ratchet, the simulated stop gap model
# was off, and test_every_WIRING_capability_is_live_right_now failed every
# morning on a probe doing its job.
#
# THE FIX. When a scan measured nothing, carry the most recent measured map
# forward — from state/atr_map.json or the previously committed bundle,
# whichever is newer — and LABEL it (`atr_carried_forward`: original as_of,
# source, age) so staleness stays visible. Chained carries keep the ORIGINAL
# provenance. Past MAX_CARRY_AGE_DAYS nothing is carried, so a genuinely dead
# scanner still shows up as an empty map instead of hiding behind old numbers.
# --------------------------------------------------------------------------- #
MAX_CARRY_AGE_DAYS = 7


def _parse_ts(ts) -> datetime | None:
    try:
        if not ts:
            return None
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _clean(atr: dict | None) -> dict:
    out = {}
    for k, v in (atr or {}).items():
        try:
            if v is not None:
                out[str(k).upper()] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _persisted_meta() -> dict | None:
    try:
        blob = json.loads(ATR_FILE.read_text())
        return {"as_of": blob.get("as_of"),
                "source": f"state/atr_map.json ({blob.get('source') or 'unknown'})"}
    except Exception:
        return None


def _candidate_from_persisted() -> dict | None:
    try:
        blob = json.loads(ATR_FILE.read_text())
    except Exception:
        return None
    atr = _clean((blob or {}).get("atr_by_ticker"))
    if not atr:
        return None
    return {"atr": atr, "as_of": blob.get("as_of"),
            "source": f"state/atr_map.json ({blob.get('source') or 'unknown'})"}


def _candidate_from_bundle(path: Path | None) -> dict | None:
    try:
        if path is None or not Path(path).exists():
            return None
        ctx = json.loads(Path(path).read_text())
    except Exception:
        return None
    scan = (ctx or {}).get("universe_scan") or {}
    atr = _clean(scan.get("atr_by_ticker"))
    if not atr:
        return None
    prior = scan.get("atr_carried_forward")
    if isinstance(prior, dict) and prior.get("as_of"):
        # Already a carry: keep the ORIGINAL measurement's provenance.
        return {"atr": atr, "as_of": prior.get("as_of"),
                "source": str(prior.get("source") or Path(path).name)}
    as_of = scan.get("scan_fetched_at_utc") or ctx.get("run_date")
    depth = ctx.get("run_depth") or scan.get("status") or "unknown"
    return {"atr": atr, "as_of": as_of,
            "source": f"{Path(path).name} ({depth} scan)"}


def carry_forward_atr(scan: dict | None, *, prior_bundle: Path | None = None,
                      max_age_days: float = MAX_CARRY_AGE_DAYS,
                      now: datetime | None = None) -> dict:
    """Fill an EMPTY scan.atr_by_ticker from the newest prior measured map.

    Never touches a scan that measured ATR itself. Returns the label dict that
    was written to scan["atr_carried_forward"], or {} if nothing was carried.
    Fail-soft; never raises."""
    try:
        if not isinstance(scan, dict) or scan.get("atr_by_ticker"):
            return {}
        now = now or datetime.now(timezone.utc)
        best = None
        for cand in (_candidate_from_persisted(), _candidate_from_bundle(prior_bundle)):
            if not cand:
                continue
            ts = _parse_ts(cand.get("as_of"))
            if ts is None:
                continue  # an undated map cannot prove it is recent enough
            age_h = (now - ts).total_seconds() / 3600.0
            if age_h > max_age_days * 24:
                continue
            if best is None or ts > best["_ts"]:
                best = {**cand, "_ts": ts, "_age_h": age_h}
        if not best:
            return {}
        label = {
            "as_of": best["_ts"].isoformat(),
            "source": best["source"],
            "age_hours": round(max(best["_age_h"], 0.0), 1),
            "n": len(best["atr"]),
            "note": ("this scan measured no ATR; atr_by_ticker is the most recent "
                     "measured map carried forward, NOT a fresh reading"),
        }
        scan["atr_by_ticker"] = best["atr"]
        scan["atr_carried_forward"] = label
        scan["atr_map_note"] = (
            f"atr_by_ticker carried forward from {label['source']} as of "
            f"{label['as_of']} ({label['age_hours']}h old); trail can ratchet "
            f"but figures are not from this run")
        return label
    except Exception:
        return {}
