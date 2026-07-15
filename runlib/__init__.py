"""Run-depth helpers for East Equity Agent (tiered cycles for speed + breadth).

Keeps schedule/depth policy out of the giant orchestrator body so slots can
change without touching gather/execute logic.
"""

from runlib.depths import (
    DEPTHS,
    depth_allows_new_buys,
    depth_description,
    resolve_depth,
    slot_depth_from_hhmm,
)

__all__ = [
    "DEPTHS",
    "depth_allows_new_buys",
    "depth_description",
    "resolve_depth",
    "slot_depth_from_hhmm",
]
