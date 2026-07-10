"""Append an improvement/self-review note to the journal and the public dashboard.

Usage: python3 scripts/log_improvement.py "Weekly self-review: ..."
Used by cloud routines (which cannot invoke the local claude CLI self-review mode).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import journal  # noqa: E402

note = sys.argv[1].strip()
run_id = f"{datetime.now():%Y%m%d}-manual"
journal.log_improvement(note, run_id)

latest = ROOT / "dashboard" / "data" / "latest.json"
if latest.exists():
    d = json.loads(latest.read_text())
    d["improvements"] = ([{"date": datetime.now(timezone.utc).date().isoformat(), "note": note}]
                         + d.get("improvements", []))[:10]
    latest.write_text(json.dumps(d, indent=2, default=str))
print("logged")
