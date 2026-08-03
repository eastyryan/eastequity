"""Model-quota rollup: how much of this system's thinking budget goes where.

WHY THIS EXISTS (measured 2026-08-03)
------------------------------------
The raw telemetry was already unreadable by hand and it was also 82% incomplete.
journal/runs held 165 run records against 29 journal/brain_calls records, and the
correlation was perfect: EVERY `brain:*` record belonged to a run on
`MacBook-Pro-2.local`, and not one of the 82 cloud `vm` runs ever recorded the
brain call it was built around. The cause was architectural — in cloud mode the
scheduled routine IS the brain (orchestrator `--gather-only` writes the bundle and
exits, `--act-on` reads a saved response file), so run_claude and all of its
telemetry are simply never reached. journal._reconcile_brain_call now closes that
hole at the one point every completed run passes through.

Closing it is only half the job. Nobody was going to `jq` 30 files a day, so the
numbers stayed invisible even for the calls that WERE recorded — the 07-28 run
that made five risk-desk calls looks identical to a quiet day in a directory
listing. This module is the compact read: per-day and per-call-type, one table.

WHAT THE DOLLAR FIGURES MEAN — read this before quoting one
-----------------------------------------------------------
The brain runs through the `claude -p` CLI authenticated with a SUBSCRIPTION
(OAuth, ~/.claude/.credentials.json). There is no ANTHROPIC_API_KEY anywhere in
this system, so nothing here is billed per token. The CLI still reports
`total_cost_usd` — what the same tokens would have cost on the API — and that is
the only comparable unit available, so this rollup reports it as
`notional_quota_usd`: a measure of QUOTA CONSUMED and of the relative weight of
one call type against another. It is not money charged to anyone. Every label in
the output says so; do not shorten it back to "cost" in a summary.

Measured across the 29 instrumented calls to 2026-07-31, the shape is:
  daily_study     11 calls, ~$1.6 notional each — the largest recurring consumer,
                  and it fires on a schedule whether or not the book trades
  risk_desk       11 calls, ~$1.5 each, only on runs that produce BUY proposals
  brain:*          6 calls, ~$5.0 each — by far the most expensive PER CALL, and
                  the one that was 100% invisible on the cloud node

Usage:
    python -m tools.llm_usage              # last 14 days, per-day + per-call-type
    python -m tools.llm_usage --days 30
    python -m tools.llm_usage --json       # same numbers, machine-readable
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "journal"

# Telemetry only exists from this date; before it, run_claude recorded nothing at
# all. Runs earlier than this are reported as `pre_telemetry` rather than folded
# into the coverage ratio, because counting them as gaps would permanently pin the
# number at a failure that has already been fixed and cannot be backfilled.
TELEMETRY_EPOCH = "2026-07-19"

_TOKEN_FIELDS = ("input_tokens", "output_tokens",
                 "cache_read_input_tokens", "cache_creation_input_tokens")


def _read_jsonl(path: Path) -> list[dict]:
    """Every parseable record in a JSONL file. A torn line from a crash mid-append
    is skipped, never raised — the same guard execute() already applies to the
    trade journal. A reporting tool must not be the thing that fails."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _days(days: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _blank() -> dict:
    return {"calls": 0, "ok": 0, "failed": 0, "retries": 0, "died_mid_call": 0,
            "instrumented": 0, "inferred": 0, "elapsed_s": 0.0,
            "notional_quota_usd": 0.0,
            **{f: 0 for f in _TOKEN_FIELDS}}


def _accumulate(bucket: dict, rec: dict) -> None:
    bucket["calls"] += 1
    if rec.get("ok"):
        bucket["ok"] += 1
    else:
        bucket["failed"] += 1
    # attempt 2 exists only because attempt 1 failed: it is quota spent twice on
    # the same question, which is exactly the kind of waste this rollup should
    # surface rather than bury inside a call count.
    if (rec.get("attempt") or 1) > 1:
        bucket["retries"] += 1
    if rec.get("instrumented") is False:
        bucket["inferred"] += 1
    else:
        bucket["instrumented"] += 1
    try:
        bucket["elapsed_s"] += float(rec.get("elapsed_s") or 0)
    except (TypeError, ValueError):
        pass
    try:
        bucket["notional_quota_usd"] += float(rec.get("total_cost_usd") or 0)
    except (TypeError, ValueError):
        pass
    for f in _TOKEN_FIELDS:
        try:
            bucket[f] += int(rec.get(f) or 0)
        except (TypeError, ValueError):
            pass


def rollup(days: int = 14) -> dict:
    """Per-day and per-call-type model consumption over the last `days` UTC days.

    Two record kinds share journal/brain_calls: `stage: "started"` breadcrumbs
    written before the CLI launches, and outcome records written after it returns.
    Only outcomes are counted as calls. A start whose call_uid never got an
    outcome is a call that BEGAN AND NEVER RETURNED — a sandbox teardown, a
    SIGKILL, a machine that slept — and is reported separately as
    `died_mid_call`, because that is a real invocation whose quota was spent and
    whose absence used to be indistinguishable from never having fired.
    """
    window = _days(days)
    by_day: dict[str, dict] = {}
    by_call: dict[str, dict] = {}
    totals = _blank()

    for day in window:
        records = _read_jsonl(JOURNAL / "brain_calls" / f"{day}.jsonl")
        starts = {r.get("call_uid") for r in records
                  if r.get("stage") == "started" and r.get("call_uid")}
        outcomes = {r.get("call_uid") for r in records if r.get("stage") != "started"}
        orphaned = len(starts - outcomes)

        bucket = by_day.setdefault(day, _blank())
        bucket["died_mid_call"] = orphaned
        totals["died_mid_call"] += orphaned
        for rec in records:
            if rec.get("stage") == "started":
                continue
            call = str(rec.get("call") or "unknown")
            _accumulate(bucket, rec)
            _accumulate(by_call.setdefault(call, _blank()), rec)
            _accumulate(totals, rec)

    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cost_basis": "notional_api_equivalent_not_billed",
        "cost_note": (
            "The brain runs on a SUBSCRIPTION via the claude CLI (OAuth; no "
            "ANTHROPIC_API_KEY exists in this system). notional_quota_usd is the "
            "CLI's API-equivalent figure — it measures quota consumed and the "
            "relative weight of one call type against another, NOT money billed."),
        "by_day": by_day,
        "by_call": dict(sorted(by_call.items(),
                               key=lambda kv: -kv[1]["notional_quota_usd"])),
        "totals": totals,
        "coverage": coverage(days),
    }


def coverage(days: int = 14) -> dict:
    """Are we actually recording the brain call on every run?

    The question the 2026-08-03 audit could not answer. A run with no `brain:*`
    record is a model invocation that happened and left no trace — that was 82% of
    the record and 100% of the cloud node. journal._reconcile_brain_call should now
    drive `runs_without_brain_call` to zero on any completed run; a non-zero value
    here means a new uninstrumented path has appeared, not that the brain sat idle.
    """
    runs = 0
    pre_telemetry = 0
    with_brain: set[str] = set()
    run_ids: list[str] = []
    for day in _days(days):
        for rec in _read_jsonl(JOURNAL / "runs" / f"{day}.jsonl"):
            if rec.get("halted"):
                continue  # preflight returned before the brain was ever woken
            if day < TELEMETRY_EPOCH:
                pre_telemetry += 1
                continue
            runs += 1
            if rec.get("run_id"):
                run_ids.append(rec["run_id"])
        for rec in _read_jsonl(JOURNAL / "brain_calls" / f"{day}.jsonl"):
            if str(rec.get("call") or "").startswith("brain") and rec.get("run_id"):
                with_brain.add(rec["run_id"])

    missing = [r for r in run_ids if r not in with_brain]
    return {
        "runs": runs,
        "runs_with_brain_call": runs - len(missing),
        "runs_without_brain_call": len(missing),
        "pct": round(100 * (runs - len(missing)) / runs, 1) if runs else None,
        "pre_telemetry_runs_excluded": pre_telemetry,
        "uninstrumented_run_ids": missing[:10],
    }


def _fmt(n: float, width: int = 8, places: int = 2) -> str:
    return f"{n:>{width},.{places}f}"


def render(data: dict) -> str:
    """Compact fixed-width table. Deliberately plain text: this gets read in a
    terminal and pasted into a run log, not rendered."""
    lines = [
        f"LLM QUOTA ROLLUP — last {data['window_days']} days "
        f"(as of {data['generated_at']})",
        "notional_usd = API-EQUIVALENT quota, NOT money billed "
        "(subscription auth, no API key)",
        "",
        f"{'DAY':<12}{'calls':>6}{'ok':>5}{'fail':>6}{'retry':>6}{'died':>6}"
        f"{'infer':>6}{'notional$':>11}{'in_tok':>10}{'out_tok':>9}",
    ]
    for day, b in data["by_day"].items():
        if not b["calls"] and not b["died_mid_call"]:
            continue
        lines.append(
            f"{day:<12}{b['calls']:>6}{b['ok']:>5}{b['failed']:>6}"
            f"{b['retries']:>6}{b['died_mid_call']:>6}{b['inferred']:>6}"
            f"{_fmt(b['notional_quota_usd'], 11)}"
            f"{b['input_tokens']:>10,}{b['output_tokens']:>9,}")

    t = data["totals"]
    lines += [
        "-" * 77,
        f"{'TOTAL':<12}{t['calls']:>6}{t['ok']:>5}{t['failed']:>6}"
        f"{t['retries']:>6}{t['died_mid_call']:>6}{t['inferred']:>6}"
        f"{_fmt(t['notional_quota_usd'], 11)}"
        f"{t['input_tokens']:>10,}{t['output_tokens']:>9,}",
        "",
        f"{'CALL TYPE':<24}{'calls':>6}{'fail':>6}{'notional$':>11}"
        f"{'avg$':>8}{'avg_s':>8}{'share':>8}",
    ]
    total_usd = t["notional_quota_usd"] or 1.0
    for call, b in data["by_call"].items():
        n = b["calls"] or 1
        lines.append(
            f"{call:<24}{b['calls']:>6}{b['failed']:>6}"
            f"{_fmt(b['notional_quota_usd'], 11)}"
            f"{_fmt(b['notional_quota_usd'] / n, 8)}"
            f"{_fmt(b['elapsed_s'] / n, 8, 1)}"
            f"{_fmt(100 * b['notional_quota_usd'] / total_usd, 7, 1)}%")

    c = data["coverage"]
    lines += [
        "",
        f"COVERAGE: {c['runs_with_brain_call']}/{c['runs']} completed runs carry a "
        f"brain call ({c['pct']}%)"
        + (f"  [{c['pre_telemetry_runs_excluded']} pre-telemetry runs excluded]"
           if c["pre_telemetry_runs_excluded"] else ""),
    ]
    if c["runs_without_brain_call"]:
        lines.append(
            f"  UNINSTRUMENTED: {c['runs_without_brain_call']} run(s) invoked a "
            f"model with no record — {', '.join(c['uninstrumented_run_ids'])}")
    if t["died_mid_call"]:
        lines.append(
            f"  DIED MID-CALL: {t['died_mid_call']} call(s) started and never "
            f"returned (quota spent, no answer)")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    n = 14
    if "--days" in argv:
        try:
            n = int(argv[argv.index("--days") + 1])
        except (IndexError, ValueError):
            pass
    data = rollup(n)
    print(json.dumps(data, indent=1) if "--json" in argv else render(data))
