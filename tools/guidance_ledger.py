"""Guidance Ledger — durable record of company guidance vs. delivered results.

The BRAIN (Claude) extracts forward guidance while it reads filing text and
news (deterministic code cannot reliably parse "we expect revenue of $24.5B to
$25.5B" out of an 8-K). This module is the deterministic half: it stores those
extractions durably, grades them against actual reported results from EDGAR
XBRL, and summarizes streaks so future theses can say "3rd consecutive
beat-and-raise" with receipts instead of vibes.

Ledger file: data/guidance_ledger.json
    {
      "DELL": [
        {
          "metric": "revenue",            # "revenue" | "eps" | free-form
          "period": "FY2026Q2",           # "FY####Q#" or "FY####" (full year)
          "guide_low": 24.5,
          "guide_high": 25.5,
          "unit": "usd_billions",         # usd | usd_millions | usd_billions | usd_per_share
          "source": "8-K 2026-07-01",
          "recorded_at": "2026-07-01T...Z",
          "actual": null,                 # filled at grading, in the entry's unit
          "verdict": null,                # "beat" | "met" | "missed"
          "graded_at": null,
          "note": null                    # grading caveats (ambiguous period, etc.)
        }
      ]
    }

Public API (all fail-soft — a broken ledger must never kill a trading run):
    record_guidance(entries)        -> summary dict
    grade_guidance(ticker, actuals) -> list of entries graded this call
    auto_grade(ticker)              -> list of entries graded from EDGAR XBRL
    get_ledger_summary(tickers)     -> per-ticker streaks + pending guidance

CLI: python -m tools.guidance_ledger DELL      (auto_grade + summary)
"""

from __future__ import annotations

import fcntl
import functools
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = ROOT / "data" / "guidance_ledger.json"

VALID_VERDICTS = ("beat", "met", "missed")
_PERIOD_RE = re.compile(r"^FY(\d{4})(?:Q([1-4]))?$")

# How the raw XBRL value (USD, or USD/share for EPS) converts into ledger units.
_UNIT_DIVISORS = {
    "usd": 1.0,
    "usd_thousands": 1e3,
    "usd_millions": 1e6,
    "usd_billions": 1e9,
    "usd_per_share": 1.0,
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _load_ledger() -> dict:
    if not LEDGER_FILE.exists():
        return {}
    try:
        data = json.loads(LEDGER_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt ledger must NOT silently become {}: the next save would
        # atomically persist the empty dict and wipe every graded receipt.
        # Preserve the bytes under a .corrupt name so history is recoverable.
        try:
            backup = LEDGER_FILE.with_name(
                LEDGER_FILE.stem
                + f".corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json")
            LEDGER_FILE.rename(backup)
            print(f"  (guidance ledger unreadable - preserved as {backup.name})")
        except OSError:
            pass
        return {}


@contextmanager
def _ledger_lock():
    """Exclusive cross-process lock over ledger read-modify-write cycles.
    Without it, an overlapping manual + scheduled run each rewrite the whole
    file and the last save silently drops the other's recorded guidance."""
    lock_path = LEDGER_FILE.with_name(LEDGER_FILE.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _locked(fn):
    """Run the wrapped read-modify-write function under _ledger_lock."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _ledger_lock():
            return fn(*args, **kwargs)
    return wrapper


def _save_ledger(ledger: dict) -> None:
    """Atomic write: a crash mid-write must never corrupt the ledger."""
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LEDGER_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(ledger, indent=2, default=str))
        os.replace(tmp, LEDGER_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _period_key(period: str) -> tuple:
    """Sort key: (fiscal_year, quarter). Full-year entries sort after Q4."""
    m = _PERIOD_RE.match(period or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else 5)


def _period_estimated_end(period: str, period_ends: dict | None = None) -> date | None:
    """When a fiscal period plausibly ended.

    Prefers the company's ACTUAL period-end from companyfacts (period_ends map,
    label -> date) so shifted fiscal years are handled correctly — the old
    calendar assumption put NVDA's Jan-year-end FY2026Q2 three months wrong and
    produced misleading "past due" notes. For a period not yet in XBRL (future
    guidance), it projects from the same quarter of the nearest prior fiscal
    year (anchored to the real fiscal calendar). Only when neither is available
    does it fall back to the calendar-quarter assumption.
    """
    m = _PERIOD_RE.match(period or "")
    if not m:
        return None
    if period_ends and period in period_ends:
        return period_ends[period]
    year = int(m.group(1))
    q = m.group(2)  # str "1".."4" or None for full-year
    if period_ends:
        for py in range(year - 1, year - 6, -1):
            plabel = f"FY{py}Q{q}" if q else f"FY{py}"
            if plabel in period_ends:
                return period_ends[plabel] + timedelta(days=365 * (year - py))
    # Fallback: calendar assumption (original behavior, fiscal calendar unknown).
    qn = int(q) if q else 4
    month = qn * 3  # Q1->Mar, Q2->Jun, Q3->Sep, Q4/FY->Dec of the labeled year
    if month == 12:
        return date(year, 12, 31)
    import calendar
    return date(year, month, calendar.monthrange(year, month)[1])


def _fiscal_label_ends(units: list) -> dict:
    """Fiscal label -> period END date, using the same earliest-filing-per-period
    mapping as _fiscal_labels (so it inherits its restatement handling). Q4
    inherits the fiscal-year end date."""
    by_period: dict = {}
    for u in units:
        # No frame exclusion: newest quarters often exist ONLY as framed entries
        # (see sec_filings.dedupe_facts). Earliest-filed still wins per period so
        # the fy/fp label comes from the original filer, not a later comparative.
        if u.get("form") not in ("10-Q", "10-K"):
            continue
        key = (u.get("start"), u.get("end"))
        if None in key:
            continue
        prev = by_period.get(key)
        if prev is None or (u.get("filed") or "9999") < (prev.get("filed") or "9999"):
            by_period[key] = u
    ends: dict = {}
    for row in by_period.values():
        fy, fp = row.get("fy"), row.get("fp")
        if fy is None or fp is None:
            continue
        try:
            end = date.fromisoformat(row["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if fp in ("Q1", "Q2", "Q3") and _quarter_span_ok(row):
            ends[f"FY{fy}{fp}"] = end
        elif fp == "FY" and _annual_span_ok(row):
            ends[f"FY{fy}"] = end
            ends.setdefault(f"FY{fy}Q4", end)  # Q4 ends at the fiscal-year end
    return ends


def _period_ends_for_ticker(ticker: str) -> dict:
    """{fiscal_label: date} of actual period ends, via the shared companyfacts
    cache. Built from the first broadly-reported duration tag."""
    from tools.sec_filings import get_company_facts

    facts = get_company_facts(ticker)  # shared TTL cache (see sec_filings)
    # us-gaap wins on tag collisions, but ifrs-full must be present too or
    # foreign filers (ARM/ASML/TSM...) can never resolve periods/actuals.
    _f = facts.get("facts", {})
    gaap = {**_f.get("ifrs-full", {}), **_f.get("us-gaap", {})}
    for tag in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "NetIncomeLoss"):
        units = gaap.get(tag, {}).get("units", {}).get("USD")
        if units:
            ends = _fiscal_label_ends(units)
            if ends:
                return ends
    return {}


# ---------------------------------------------------------------------------
# record_guidance
# ---------------------------------------------------------------------------
@_locked
def record_guidance(entries: list) -> dict:
    """Validate + dedupe + append guidance entries extracted by the brain.

    Dedupe rule: same (ticker, metric, period) REPLACES the existing entry if
    it is still ungraded (the brain saw fresher guidance — a raise/cut). A
    graded entry is a receipt and is never overwritten; the new one is skipped.

    Returns {"recorded": n, "replaced": n, "skipped_graded": n,
             "invalid": [{"entry": ..., "reason": ...}]}.
    """
    summary = {"recorded": 0, "replaced": 0, "skipped_graded": 0, "invalid": []}
    if not entries:
        return summary
    ledger = _load_ledger()
    changed = False

    for raw in entries:
        try:
            if not isinstance(raw, dict):
                raise ValueError("entry is not a dict")
            ticker = str(raw["ticker"]).strip().upper()
            metric = str(raw["metric"]).strip().lower()
            period = str(raw["period"]).strip().upper().replace(" ", "")
            if not re.fullmatch(r"[A-Z.]{1,6}", ticker):
                raise ValueError(f"bad ticker {ticker!r}")
            if not metric:
                raise ValueError("empty metric")
            if not _PERIOD_RE.match(period):
                raise ValueError(f"period {period!r} not FY####[Q#]")
            low = float(raw["guide_low"])
            high = float(raw["guide_high"])
            if low > high:
                low, high = high, low  # forgive swapped bounds
            unit = str(raw.get("unit", "usd")).strip().lower()
            entry = {
                "metric": metric, "period": period,
                "guide_low": low, "guide_high": high, "unit": unit,
                "source": str(raw.get("source", "") or ""),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "actual": None, "verdict": None, "graded_at": None,
                "note": None,
            }
        except (KeyError, ValueError, TypeError) as e:
            summary["invalid"].append({"entry": raw, "reason": str(e)})
            continue

        rows = ledger.setdefault(ticker, [])
        existing_i = next((i for i, r in enumerate(rows)
                           if r.get("metric") == metric and r.get("period") == period), None)
        if existing_i is None:
            rows.append(entry)
            summary["recorded"] += 1
        elif rows[existing_i].get("verdict") is None:
            rows[existing_i] = entry
            summary["replaced"] += 1
        else:
            summary["skipped_graded"] += 1
            continue
        rows.sort(key=lambda r: _period_key(r.get("period", "")))
        changed = True

    if changed:
        _save_ledger(ledger)
    return summary


# ---------------------------------------------------------------------------
# grade_guidance
# ---------------------------------------------------------------------------
def _verdict(actual_in_unit: float, low: float, high: float) -> str:
    if actual_in_unit > high:
        return "beat"
    if actual_in_unit < low:
        return "missed"
    return "met"


def _unit_check(raw_actual: float, low: float, high: float, unit: str) -> tuple:
    """(ok, suggested_unit, factor) magnitude sanity before grading.

    A brain mislabel (unit "usd" while the number means billions) otherwise
    grades every print as a huge beat. We compare the divisor the XBRL actual
    IMPLIES (raw_actual / guide_midpoint) to the entry's stated divisor: a
    consistent unit lands them within the same order of magnitude, so a ratio
    ~1000x off means the unit is wrong. Sign is ignored here (a guided profit
    delivered as a loss is a legitimate miss, not a unit error). Returns
    ok=False with the divisor that best reconciles the two (as a unit name).
    """
    mid = (low + high) / 2.0
    if mid == 0 or raw_actual == 0:
        return True, None, 1.0
    stated = _UNIT_DIVISORS.get(unit, 1.0)
    implied = abs(raw_actual) / abs(mid)
    ratio = implied / stated
    # Real unit mislabels are >=1000x off (usd vs millions vs billions); genuine
    # business outcomes can legitimately land several-fold outside the guide
    # (a low-EPS name guiding $0.15 and printing $0.90 is a 6x BEAT, not a unit
    # error). The old 0.2-5x band refused to grade exactly those blowouts.
    if 0.02 <= ratio <= 50.0:             # far inside any unit-conversion factor
        return True, None, ratio
    factor = ratio if ratio >= 1 else (1.0 / ratio if ratio > 0 else float("inf"))
    suggested, best = None, None
    for u, d in _UNIT_DIVISORS.items():
        if d <= 0:
            continue
        r = implied / d
        if 0.02 <= r <= 50.0:
            score = abs(math.log10(r))
            if best is None or score < best:
                best, suggested = score, u
    return False, suggested, factor


@_locked
def grade_guidance(ticker: str, actuals: dict) -> list:
    """Grade ungraded ledger entries against supplied actuals.

    actuals: {"revenue": {"FY2026Q2": 4.2e9}, "eps": {"FY2026Q2": 1.85}}
    Values are RAW (USD for dollar metrics, per-share for EPS); they are
    converted into each entry's stated unit before comparing to the guide.
    A magnitude sanity check (see _unit_check) leaves an entry UNGRADED with a
    unit_mismatch note rather than emitting a false verdict when the guided
    unit and the XBRL actual disagree by ~1000x.
    Returns the list of entries graded (with a real verdict) by this call.
    """
    ticker = str(ticker).strip().upper()
    ledger = _load_ledger()
    rows = ledger.get(ticker, [])
    graded = []
    changed = False
    for entry in rows:
        if entry.get("verdict") is not None:
            continue
        metric_actuals = (actuals or {}).get(entry["metric"], {})
        if entry["period"] not in metric_actuals:
            continue
        try:
            raw_actual = float(metric_actuals[entry["period"]])
        except (TypeError, ValueError):
            continue
        unit = entry.get("unit", "usd")
        ok, suggested, factor = _unit_check(
            raw_actual, entry["guide_low"], entry["guide_high"], unit)
        if not ok:
            note = ("unit_mismatch: XBRL actual (%.6g) is ~%sx off the guided "
                    "%s-%s %s range - stated unit likely wrong%s; left ungraded "
                    "to avoid a false beat/miss" % (
                        raw_actual, _round_factor(factor),
                        entry["guide_low"], entry["guide_high"], unit,
                        " (looks like %s)" % suggested if suggested else ""))
            if entry.get("note") != note:
                entry["note"] = note
                changed = True
            continue
        divisor = _UNIT_DIVISORS.get(unit, 1.0)
        actual = round(raw_actual / divisor, 4)
        entry["actual"] = actual
        entry["verdict"] = _verdict(actual, entry["guide_low"], entry["guide_high"])
        entry["graded_at"] = datetime.now(timezone.utc).isoformat()
        graded.append(dict(entry, ticker=ticker))
        changed = True
    if changed:
        _save_ledger(ledger)
    return graded


def _round_factor(factor: float) -> str:
    if factor == float("inf"):
        return "inf"
    if factor >= 100:
        return str(int(round(factor / 100.0) * 100))  # 1000, 1000000, ...
    return "%.0f" % factor


# ---------------------------------------------------------------------------
# auto_grade — pull actuals from EDGAR XBRL and grade what has been reported
# ---------------------------------------------------------------------------
def _quarter_span_ok(row: dict) -> bool:
    try:
        days = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
    except (KeyError, ValueError):
        return False
    return 80 <= days <= 100


def _annual_span_ok(row: dict) -> bool:
    try:
        days = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
    except (KeyError, ValueError):
        return False
    return days > 300


def _fiscal_labels(units: list) -> tuple:
    """(labels, annual_by_fy, quarters_by_fy) — fiscal label -> raw XBRL value.

    PERIOD-MAPPING HEURISTIC (best-effort, documented):
    Each XBRL fact carries fy/fp describing the FILING it appeared in, not the
    fact's own period — but the newest period inside any 10-Q/10-K equals the
    filing's own fy/fp. So for each distinct (start, end) period we take the
    row from the EARLIEST filing that reported it (min "filed"): that filing
    is the one whose fy/fp labels this exact period. Quarterly rows (80-100
    day span, fp in Q1-Q3) label "FY{fy}Q{n}"; annual 10-K rows (fp == FY,
    >300 day span) label "FY{fy}". Q4 standalone values are never filed, so
    additive metrics get FY minus Q1+Q2+Q3 when all four exist (marked
    derived); non-additive metrics (EPS) get no Q4. Rows with restated or
    missing fy/fp are dropped — an entry we cannot map stays ungraded with a
    note rather than being graded against the wrong quarter.
    """
    by_period: dict = {}
    for u in units:
        # No frame exclusion (newest quarters are often framed-only); earliest-filed
        # per period keeps the original filer's fy/fp label for correct mapping.
        if u.get("form") not in ("10-Q", "10-K"):
            continue
        key = (u.get("start"), u.get("end"))
        if None in key:
            continue
        prev = by_period.get(key)
        if prev is None or (u.get("filed") or "9999") < (prev.get("filed") or "9999"):
            by_period[key] = u

    labels: dict = {}
    annual: dict = {}
    quarters_by_fy: dict = {}
    for row in by_period.values():
        fy, fp = row.get("fy"), row.get("fp")
        if fy is None or fp is None:
            continue
        if fp in ("Q1", "Q2", "Q3") and _quarter_span_ok(row):
            label = f"FY{fy}{fp}"
            labels[label] = row["val"]
            quarters_by_fy.setdefault(int(fy), {})[fp] = row["val"]
        elif fp == "FY" and _annual_span_ok(row):
            labels[f"FY{fy}"] = row["val"]
            annual[int(fy)] = row["val"]
    return labels, annual, quarters_by_fy


def _actuals_from_edgar(ticker: str) -> dict:
    """{"revenue": {period: raw_usd}, "eps": {period: per_share}} from XBRL."""
    from tools.sec_filings import get_company_facts

    facts = get_company_facts(ticker)  # shared TTL cache (see sec_filings)
    # us-gaap wins on tag collisions, but ifrs-full must be present too or
    # foreign filers (ARM/ASML/TSM...) can never resolve periods/actuals.
    _f = facts.get("facts", {})
    gaap = {**_f.get("ifrs-full", {}), **_f.get("us-gaap", {})}

    out: dict = {}
    # Revenue: same tag preference order as tools.sec_filings.get_filing_brief
    # (kept in sync so ledger actuals match what the brain sees in its bundle):
    # umbrella `Revenues` first (contract tags are subsets - grading a total-revenue
    # guide against a subset actual is a false miss), banks' net-of-interest tag,
    # then the contract variants. The tag whose facts are NEWEST wins; ties go to
    # the earlier (more inclusive) tag - never a first-tag-with-any-data break.
    for label, tags, additive in (
        ("revenue", ["Revenues", "RevenuesNetOfInterestExpense",
                     "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "RevenueFromContractWithCustomerIncludingAssessedTax",
                     "Revenue",  # ifrs-full umbrella tag (foreign filers)
                     "RevenueFromContractsWithCustomers"], True),
        ("eps", ["EarningsPerShareDiluted", "EarningsPerShareBasic"], False),
    ):
        best_units, best_end = None, ""
        for tag in tags:
            unit_map = gaap.get(tag, {}).get("units", {})
            units = unit_map.get("USD") or unit_map.get("USD/shares")
            if not units:
                continue
            newest = max((u.get("end", "") for u in units
                          if u.get("form") in ("10-Q", "10-K")), default="")
            if newest > best_end:
                best_units, best_end = units, newest
        if not best_units:
            continue
        labels, annual, q_by_fy = _fiscal_labels(best_units)
        if additive:  # derive Q4 = FY - (Q1+Q2+Q3) where the full set exists
            for fy, qs in q_by_fy.items():
                if fy in annual and all(k in qs for k in ("Q1", "Q2", "Q3")):
                    labels[f"FY{fy}Q4"] = annual[fy] - sum(qs.values())
        out[label] = labels
    return out


def auto_grade(ticker: str) -> list:
    """Grade any ungraded entries for `ticker` using EDGAR XBRL actuals.

    Fail-soft: network/parse errors return [] with a printed warning rather
    than raising — a dead EDGAR must never break a trading run. Entries whose
    period has plausibly ended but has no mappable actual get an explanatory
    "note" and stay ungraded (Q4 EPS, fiscal-calendar ambiguity, not yet filed).
    """
    ticker = str(ticker).strip().upper()
    ledger = _load_ledger()
    rows = ledger.get(ticker, [])
    if not any(r.get("verdict") is None for r in rows):
        return []
    try:
        actuals = _actuals_from_edgar(ticker)
    except Exception as e:
        print(f"  (guidance auto_grade {ticker}: EDGAR unavailable - {e})")
        return []

    graded = grade_guidance(ticker, actuals)
    graded_keys = {(g["metric"], g["period"]) for g in graded}

    # Actual fiscal-period ends (handles shifted fiscal years) for past-due notes.
    try:
        period_ends = _period_ends_for_ticker(ticker)
    except Exception:
        period_ends = {}

    # Annotate past-due entries we could NOT grade so the brain sees why.
    # (Under the ledger lock: this is a read-modify-write over the whole file.)
    with _ledger_lock():
        ledger = _load_ledger()
        changed = False
        today = datetime.now(timezone.utc).date()
        for entry in ledger.get(ticker, []):
            if entry.get("verdict") is not None:
                continue
            if (entry["metric"], entry["period"]) in graded_keys:
                continue
            est_end = _period_estimated_end(entry["period"], period_ends)
            if est_end and est_end < today and not entry.get("note"):
                if entry["metric"] == "eps" and entry["period"].endswith("Q4"):
                    entry["note"] = "Q4 EPS not standalone in XBRL (non-additive); grade manually"
                else:
                    entry["note"] = (f"period likely ended (~{est_end}) but no matching "
                                     f"XBRL actual yet - not filed or fiscal mapping ambiguous")
                changed = True
        if changed:
            _save_ledger(ledger)
    return graded


# ---------------------------------------------------------------------------
# get_ledger_summary — the context-bundle view
# ---------------------------------------------------------------------------
def get_ledger_summary(tickers: list) -> dict:
    """Per-ticker guidance track record for the brain's context bundle.

    consecutive_beats counts back from the most recently reported graded
    period (ordered by fiscal period, not grading time), PER METRIC
    ({"revenue": n, "eps": m}), so "Nth consecutive revenue beat" claims are
    receipt-backed and never truncated by the other metric's result.
    """
    ledger = _load_ledger()
    out = {}
    for t in [str(x).strip().upper() for x in (tickers or [])]:
        rows = sorted(ledger.get(t, []), key=lambda r: _period_key(r.get("period", "")))
        graded = [r for r in rows if r.get("verdict") in VALID_VERDICTS]
        pending = [r for r in rows if r.get("verdict") is None]
        if not rows:
            continue
        # Streaks are PER METRIC: a mixed revenue+eps walk let an interleaved
        # EPS miss truncate a genuine revenue-beat streak (or vice versa), so
        # "3rd straight revenue beat" could be under- or over-counted.
        streaks: dict = {}
        for metric in {r["metric"] for r in graded}:
            s = 0
            for r in reversed([g for g in graded if g["metric"] == metric]):
                if r["verdict"] == "beat":
                    s += 1
                else:
                    break
            streaks[metric] = s
        out[t] = {
            "consecutive_beats": streaks,
            "total_graded": len(graded),
            "beats": sum(1 for r in graded if r["verdict"] == "beat"),
            "met": sum(1 for r in graded if r["verdict"] == "met"),
            "missed": sum(1 for r in graded if r["verdict"] == "missed"),
            "recent_graded": [
                {k: r.get(k) for k in ("metric", "period", "guide_low", "guide_high",
                                       "unit", "actual", "verdict", "source")}
                for r in graded[-5:]
            ],
            "pending_guidance": [
                {k: r.get(k) for k in ("metric", "period", "guide_low", "guide_high",
                                       "unit", "source", "note")}
                for r in pending
            ],
        }
    if out:
        out["note"] = ("Company guidance you previously extracted vs. delivered "
                       "results, graded deterministically from EDGAR XBRL. Cite "
                       "consecutive_beats/verdicts as receipts; pending_guidance "
                       "are the numbers management is on the hook for next print.")
    return out


if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    print(json.dumps({"auto_graded": auto_grade(tk),
                      "summary": get_ledger_summary([tk])}, indent=2, default=str))
