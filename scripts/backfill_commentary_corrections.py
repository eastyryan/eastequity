#!/usr/bin/env python3
"""One-shot backfill: stamp commentary_corrections onto ALREADY-PUBLISHED runs.

tools/commentary_reconcile fixes this going forward, but the false sentences are
already on the site — and on 2026-08-04 the offending run WAS latest.json, so the
front page read "I bought a small position in DexCom (DXCM) today" next to an
open-positions table holding only BKR. A forward-only fix leaves that standing.

Rewrites nothing but the added `commentary_correction` field (plus `corrected`
on the archive index). The original prose is never edited: the record keeps both
the claim and the retraction, which is the whole point of publishing a
correction rather than quietly deleting a sentence.

Idempotent — re-running produces no further change. Run from the repo root:

    python3 scripts/backfill_commentary_corrections.py [--apply]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.commentary_reconcile import reconcile  # noqa: E402

DASH = ROOT / "dashboard" / "data"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the files (default is a dry run)")
    args = ap.parse_args()

    corrections: dict[str, str] = {}
    for path in sorted(DASH.glob("run_*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skip {path.name}: {e}")
            continue
        report = reconcile(data.get("commentary"), data.get("proposals"),
                           data.get("fills"))
        correction = report.get("correction")
        if not correction:
            continue
        run_id = data.get("run_id") or path.stem.removeprefix("run_")
        corrections[run_id] = correction
        print(f"\n{run_id}  {report['issues']}")
        print(f"  {correction}")
        if args.apply and data.get("commentary_correction") != correction:
            data["commentary_correction"] = correction
            path.write_text(json.dumps(data, indent=2))

    # latest.json is a copy of the newest run and is what the front page renders.
    latest = DASH / "latest.json"
    if latest.exists():
        data = json.loads(latest.read_text())
        correction = corrections.get(data.get("run_id"))
        if correction:
            print(f"\nlatest.json ({data.get('run_id')}) carries the claim -> correcting")
            if args.apply and data.get("commentary_correction") != correction:
                data["commentary_correction"] = correction
                latest.write_text(json.dumps(data, indent=2))

    # The archive list headlines each run with its commentary's FIRST sentence,
    # which is exactly where these claims live.
    index = DASH / "runs_index.json"
    if index.exists():
        rows = json.loads(index.read_text())
        touched = 0
        for row in rows:
            correction = corrections.get(row.get("run_id"))
            if correction and row.get("commentary_correction") != correction:
                row["corrected"] = True
                row["commentary_correction"] = correction
                touched += 1
        print(f"\nruns_index.json: {touched} entries stamped")
        if args.apply and touched:
            index.write_text(json.dumps(rows, indent=2))

    print(f"\n{len(corrections)} run(s) corrected"
          + ("" if args.apply else "  (dry run — pass --apply to write)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
