"""Tests for the LEARNED, PERSISTENT ticker->CUSIP map in smart_money_13f.

There is no authoritative free ticker->CUSIP feed, so the tool accumulates
CUSIPs it resolves confidently (via strict issuer-name matches) into
data/cusip_map.json and reuses them authoritatively on later runs. These tests
cover the storage/merge/learn helpers in isolation with INJECTED fakes (no
network): load, merge (passed arg wins), the anti-poisoning learn gate,
atomic-and-durable persistence, and fail-soft degradation on a corrupt/missing
file.

Pure Python, no network - run with:  python3 tests/test_cusip_map.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import smart_money_13f as sm

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class _tmp_map:
    """Point the module's CUSIP_MAP_FILE at a throwaway file for one test."""
    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self._orig = sm.CUSIP_MAP_FILE
        sm.CUSIP_MAP_FILE = Path(self._dir.name) / "cusip_map.json"
        return sm.CUSIP_MAP_FILE

    def __exit__(self, *exc):
        sm.CUSIP_MAP_FILE = self._orig
        self._dir.cleanup()


def test_entry_extraction():
    print("CUSIP extraction accepts rich dict AND bare string forms:")
    check("rich dict", sm._cusip_from_entry({"cusip": "67066g104"}) == "67066G104")
    check("bare string", sm._cusip_from_entry("67066g104") == "67066G104")
    check("junk -> ''", sm._cusip_from_entry({"nope": 1}) == "")
    check("None -> ''", sm._cusip_from_entry(None) == "")


def test_load_missing_and_corrupt_degrade():
    print("missing/corrupt file degrades to {} and never raises:")
    with _tmp_map() as f:
        check("missing -> {}", sm._load_cusip_map() == {})
        f.write_text("{ this is not valid json ]]")
        check("corrupt -> {} (no raise)", sm._load_cusip_map() == {})
        f.write_text(json.dumps(["not", "a", "dict"]))
        check("non-dict json -> {}", sm._load_cusip_map() == {})
        # A degraded map means matching just falls back to today's name-matching.
        check("merged corrupt -> only passed", sm._merged_cusip_map({"NVDA": "x"})
              == {"NVDA": "X"})


def test_load_flattens_both_formats():
    print("load flattens rich + bare entries, upper-cases tickers/cusips:")
    with _tmp_map() as f:
        f.write_text(json.dumps({
            "nvda": {"cusip": "67066g104", "learned_at": "z", "source_manager": "m"},
            "AMD": "007903109",
            "BAD": {"learned_at": "z"},  # no cusip -> dropped
        }))
        m = sm._load_cusip_map()
        check("NVDA flattened+upper", m.get("NVDA") == "67066G104", m.get("NVDA"))
        check("AMD bare form", m.get("AMD") == "007903109", m.get("AMD"))
        check("cusip-less entry dropped", "BAD" not in m, list(m))


def test_merge_passed_wins():
    print("merge: caller-passed map overrides the learned on-disk map:")
    with _tmp_map() as f:
        f.write_text(json.dumps({"NVDA": {"cusip": "LEARNED01"},
                                 "AMD": {"cusip": "007903109"}}))
        merged = sm._merged_cusip_map({"nvda": "passed99"})
        check("passed overrides learned", merged["NVDA"] == "PASSED99", merged.get("NVDA"))
        check("unpassed learned survives", merged["AMD"] == "007903109", merged.get("AMD"))
        check("passed normalized upper", "PASSED99" == merged["NVDA"])


def test_resolve_learned_anti_poison():
    print("learn gate rejects ambiguous / conflicting / already-known tickers:")
    # Clean single match across one or more agreeing managers -> learned.
    cand = {"NVDA": {"67066G104": "Coatue Management"}}
    out = sm._resolve_learned(cand, blocked=set(), existing={})
    check("clean single learned", out.get("NVDA") == ("67066G104", "Coatue Management"), out)

    # Two managers name-matched the SAME ticker to DIFFERENT cusips -> not learned.
    cand = {"FOO": {"AAA": "Mgr A", "BBB": "Mgr B"}}
    out = sm._resolve_learned(cand, blocked=set(), existing={})
    check("cross-manager disagreement rejected", "FOO" not in out, out)

    # A manager matched >1 issuer for the name (blocked) -> not learned even if
    # another manager gave a clean single match.
    cand = {"BAR": {"CCC": "Mgr A"}}
    out = sm._resolve_learned(cand, blocked={"BAR"}, existing={})
    check("ambiguous(blocked) rejected", "BAR" not in out, out)

    # Already authoritatively mapped -> never re-learned / overwritten.
    cand = {"AMD": {"NEWCUSIP": "Mgr A"}}
    out = sm._resolve_learned(cand, blocked=set(), existing={"AMD": "007903109"})
    check("existing authoritative not overwritten", "AMD" not in out, out)


def test_persist_round_trip_and_no_clobber():
    print("persist writes rich entries, is durable, and never clobbers existing:")
    with _tmp_map():
        added = sm._persist_learned({"NVDA": ("67066G104", "Coatue Management")})
        check("reports what it added", added == {"NVDA": "67066G104"}, added)
        raw = sm._load_cusip_map_raw()
        e = raw.get("NVDA", {})
        check("stored as rich dict", e.get("cusip") == "67066G104", e)
        check("has source_manager", e.get("source_manager") == "Coatue Management", e)
        check("has learned_at stamp", bool(e.get("learned_at")), e)
        check("reloads via flatten", sm._load_cusip_map().get("NVDA") == "67066G104")

        # Re-learning an existing ticker (even with a different cusip) is a no-op:
        # the authoritative entry wins and is never overwritten.
        added2 = sm._persist_learned({"NVDA": ("WRONGCUSIP", "Bad Mgr")})
        check("no clobber of existing", added2 == {}, added2)
        check("original cusip intact", sm._load_cusip_map()["NVDA"] == "67066G104")

        # Adding a genuinely new ticker merges alongside the old one.
        sm._persist_learned({"AMD": ("007903109", "Tiger Global")})
        m = sm._load_cusip_map()
        check("merge keeps both", m.get("NVDA") == "67066G104" and m.get("AMD") == "007903109", m)


def test_atomic_write_leaves_no_temp():
    print("atomic write leaves no .tmp turds and produces valid JSON:")
    with _tmp_map() as f:
        sm._save_cusip_map({"NVDA": {"cusip": "67066G104"}})
        leftovers = list(f.parent.glob("*.tmp"))
        check("no leftover temp files", leftovers == [], leftovers)
        check("output is valid json", isinstance(json.loads(f.read_text()), dict))
        # os.replace means the destination is always either old or new, never half.
        check("file exists after replace", f.exists())


def test_persist_empty_is_noop():
    print("persisting nothing writes nothing (no file churn):")
    with _tmp_map() as f:
        check("empty learned -> {}", sm._persist_learned({}) == {})
        check("no file created", not f.exists())


if __name__ == "__main__":
    for fn in (test_entry_extraction, test_load_missing_and_corrupt_degrade,
               test_load_flattens_both_formats, test_merge_passed_wins,
               test_resolve_learned_anti_poison, test_persist_round_trip_and_no_clobber,
               test_atomic_write_leaves_no_temp, test_persist_empty_is_noop):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        sys.exit(1)
    print("All cusip-map load/merge/learn/persist/atomicity checks passed.")
