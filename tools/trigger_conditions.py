"""Trigger event/date gates — a price touch is not the trigger when the trigger said "after".

WHY THIS EXISTS (2026-08-03, from the agent's OWN lesson LP-0693555695, written
in run 20260717-e6c993 and carried in data/adopted_lessons.json ever since):

    "The deterministic trigger check fired on AMD purely because the *price*
     touched the parsed $500 level, but my published `would_buy_at` was a
     compound condition — *post-event* confirmation plus a 50-DMA reclaim,
     explicitly excluding a pre-event dip. The result is a full event-driven run
     spun up for a setup that was never actually live, five days before its own
     catalyst."

The trigger that fired was, verbatim from that week's watchlist:

    "A confirmed positive reaction to the 7/22-23 Advancing AI event that
     reclaims and holds above the 50-DMA, basing $500-520 -- not a bounce off
     today's low alone."

`parse_price_level` read "$500", price came within 2%, and the alert fired on
07-17 — five days BEFORE the event the sentence is entirely about, and against
a clause ("not a bounce off today's low alone") that says in plain words the
pre-event dip does not count.

That was merely wasteful when a fired trigger's only consequence was a shadow-
book row. It is not wasteful any more. tools/engagement.py (2026-08-03) made a
fired trigger an OBLIGATION: the run must file a `trigger_reviews` row resolving
it, and a name that reaches its level on `max_unbought_trigger_hits` (3) separate
days without being bought is FORCE-DROPPED from the stored watchlist. A trigger
that fires on price alone therefore now manufactures review busywork and can
force-drop a name for misses that never happened — the setup was not live, so
there was nothing to miss.

WHAT THIS MODULE DOES

Parses the EVENT/DATE GATE out of the trigger text and reports whether it is
satisfied as of a date the CALLER passes in. Four shapes, drawn from real
`would_buy_at` strings in dashboard/data/run_*.json:

    "near $170 or after the 8/4 earnings print"   OR-joined  -> never suppress
    "$345 after the 7/22-23 print"                AND-joined -> suppress pre-7/22
    "reclaim of $195 post-earnings"               AND-joined, UNDATED -> fail open
    "near $58"                                    no gate    -> unchanged

THREE RULES, ALL OF THEM BIASED TOWARD FIRING

  1. FAIL OPEN. A missed alert is now a real cost (the name sits unresearched
     while its level is live), so nothing is suppressed unless a HARD gate with
     a RESOLVED date says the setup is not live yet. An unparseable trigger, a
     date we cannot pin to a year, an undated "post-earnings", a gate hedged
     with "ideally" — all behave exactly as before this module existed.

  2. A DATE ONLY GATES WHEN A DIRECTION WORD ATTACHES IT. Bare dates are
     everywhere in these strings and most of them are not conditions ("clarity
     on the 8/4 print", "sizing around (not through) the 8/5 earnings"). A gate
     needs after/post/following/once/past/reaction-to (forward gate) or
     before/ahead-of/prior-to/until (window gate) within a short, punctuation-
     free reach of the date. This is also what keeps "50/200-DMA", "2-3
     sessions" and "$345-348" from being read as dates — a month must be 1-12
     and the token must actually look like a date.

  3. EVERY OR-BRANCH MUST BE BLOCKED. The trigger is split on top-level "or"
     and suppression requires EVERY branch to carry an unsatisfied hard gate.
     "near $170 or after the 8/4 print" has a branch that is live on price
     alone, so it fires — which is exactly what "or" means. "and"/"plus"/"with"
     do not split, because they conjoin conditions inside one branch.

COMPOSES WITH tools/signal_discipline.sanitize_trigger, WHICH RUNS FIRST AND CAN
EAT THE GATE. That sanitizer excises measured-dead confirmation legs together
with the rest of their clause (`_TAIL = r"[^,;]*"`), so

    "close above $175 with volume confirmation after the 8/4 print"
      -> stored as "close above $175"      (the event gate went with the tail)
      -> original kept on the entry as `would_buy_at_original`

is a real and common shape. `evaluate_trigger` therefore reads BOTH the stored
trigger and the original and suppresses if EITHER of them is fully gated. That
is safe in the one asymmetric case it creates: if sanitizing collapsed an
"X or <dead read>" into just "X", the branch that disappeared was a read this
system's own ablation scored as non-discriminating, and such a read may not
serve as a trigger condition in the first place.

Pure: no network, no config, no clock — the evaluation date is an argument.

CLI: python -m tools.trigger_conditions   (self-checks against the AMD incident)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

__all__ = [
    "Gate",
    "parse_trigger_gates",
    "evaluate_trigger",
    "trigger_is_live",
    "filter_trigger_alerts",
]


# ---------------------------------------------------------------------------
# Dates. Deliberately narrow: only tokens that cannot be read as something else.
# ---------------------------------------------------------------------------
# 2026-08-04. Written by the brain when it has a confirmed calendar entry
# ("Post-2026-08-06 print, once IV crushes and the gap settles").
_ISO_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")

# 7/22-23 and 8/4-8/6. A two-day print window is how the brain writes an event
# whose exact side of the close it does not know; the AMD trigger used this form.
_SLASH_RANGE_RE = re.compile(
    r"\b(\d{1,2})/(\d{1,2})\s*[-–—]\s*(?:(\d{1,2})/)?(\d{1,2})\b")

# 8/4, 7/29, 9/3. The `/` is load-bearing: it is what separates a date from the
# "2-3 sessions", "$345-348" and "20-DMA" numbers these strings are full of.
_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")

# "after the July 22 print", "past its early-August print" (the latter has no
# day and is deliberately NOT resolvable — see rule 1).
_MONTHNAME_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(20\d{2}))?\b", re.IGNORECASE)

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


# ---------------------------------------------------------------------------
# Direction cues. What turns a bare date into a condition.
# ---------------------------------------------------------------------------
# FORWARD gate ("the setup is not live until this has happened"). "reaction to"
# / "response to" are in here because that is how the AMD trigger was actually
# phrased — "a confirmed positive reaction to the 7/22-23 event" — and an
# after/post-only cue list would have missed the exact incident this exists for.
_AFTER_CUE = re.compile(
    r"\b(?:after|post|following|once|past|subsequent\s+to|"
    r"reaction\s+to|reacts?\s+to|reacting\s+to|response\s+to|responds?\s+to)\b",
    re.IGNORECASE)

# WINDOW gate ("only good until this"). "by" is deliberately absent: in this
# corpus it introduces a re-underwriting deadline, not a buy window ("If neither
# has happened by 8/14, re-underwrite rather than lower the bar").
_BEFORE_CUE = re.compile(
    r"\b(?:before|ahead\s+of|prior\s+to|in\s+advance\s+of|until)\b",
    re.IGNORECASE)

_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("after", _AFTER_CUE), ("before", _BEFORE_CUE))

# How far a cue may sit from its date, and what it may not cross. A clause break
# between them means they belong to different thoughts: in "...volume, ideally
# after the 8/4 print" the cue reaches its date, but in "...after this selloff
# settles, or a pullback into the $900-930 zone" nothing reaches anything.
_MAX_CUE_GAP = 24
_CLAUSE_STOP = re.compile(r"[,;.!?]")

# A hedged gate is a PREFERENCE, not a condition, and suppressing on one would
# cost a live alert. "A reclaim of the 50-DMA held for 2-3 sessions on
# above-average volume, ideally after the 8/4 print settles" still wants the
# alert when the reclaim happens on 8/1.
_SOFT_CUE = re.compile(
    r"\b(?:ideally|preferab\w*|optional\w*|hopefully|if\s+possible|"
    r"nice\s+to\s+have|would\s+prefer|prefer\w*)\b", re.IGNORECASE)
_SOFT_WINDOW = 40

# An event gate with no date at all — "reclaim of $195 post-earnings", "wait for
# post-earnings confirmation". Parsed and REPORTED, but never resolved from the
# text alone: which print, and did it already happen? Answering that needs a
# calendar, and guessing it wrong suppresses a live alert. The caller may pass
# `event_dates` to resolve it; otherwise it fails open.
_UNDATED_EVENT_RE = re.compile(
    r"\b(?:after|post|following|once|past|subsequent\s+to|reaction\s+to|"
    r"response\s+to)\b"
    r"[\s\-–—]*"
    r"(?:(?:the|its|their|a|an|this|next|upcoming|that)\s+)*"
    r"(?:(?:q[1-4]|fy\d{2}|fiscal|quarterly|next|upcoming)\s+)*"
    r"(earnings|print|reports?|results|guidance)\b", re.IGNORECASE)

# Top-level disjunction. "and"/"plus"/"with" are NOT separators — they conjoin
# legs inside one branch, which is why "$345 after the 7/22-23 print" and
# "reclaim of $195 post-earnings" are both-required while the "or" forms are not.
_OR_SPLIT = re.compile(r"(?:,\s*)?\bor\b", re.IGNORECASE)


@dataclass(frozen=True)
class Gate:
    """One event/date condition lifted out of a trigger string.

    kind      "after" (not live until) | "before" (only live until)
    date      resolved calendar date, or None when the text named an event but
              no date we could pin (see _UNDATED_EVENT_RE)
    event     the event noun when the text named one ("earnings", "print", ...)
    soft      hedged with "ideally"/"preferably" — never suppresses
    raw       the slice of trigger text the gate was read from
    branch    which top-level "or" branch it sits in
    """

    kind: str
    date: date | None
    event: str | None
    soft: bool
    raw: str
    branch: int = 0

    def satisfied(self, as_of: date) -> bool | None:
        """True/False, or None when the gate carries no resolvable date."""
        if self.date is None:
            return None
        if self.kind == "before":
            # Permissive edge: the window is still open ON the stated date.
            return as_of <= self.date
        # "after the 8/4 print" fires ON 8/4. The print may be before the open
        # or after the close and the text never says which, so the earlier read
        # is the one that cannot cost a live alert.
        return as_of >= self.date

    def blocks(self, as_of: date) -> bool:
        """Does this gate, on its own, mean the setup is not live?"""
        return (not self.soft) and self.satisfied(as_of) is False

    def as_dict(self) -> dict:
        return {"kind": self.kind,
                "date": self.date.isoformat() if self.date else None,
                "event": self.event, "soft": self.soft,
                "raw": self.raw, "branch": self.branch}


# ---------------------------------------------------------------------------
# Date resolution
# ---------------------------------------------------------------------------
def _as_date(value: Any) -> date | None:
    """Accept a date, a datetime, or a leading ISO YYYY-MM-DD string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _nearest_year(month: int, day: int, ref: date) -> date | None:
    """Pin a year-less "8/4" to the year that puts it CLOSEST to the run date.

    Not "this year": a trigger written on 12-20 that says "after the 1/15 print"
    means next January, and reading it as a date eleven months past would mark
    the gate satisfied and fire the alert a month early.
    """
    best: date | None = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - ref).days) < abs((best - ref).days):
            best = cand
    return best


def _dates_from_match(rx: re.Pattern[str], m: re.Match[str],
                      ref: date) -> list[date]:
    """The one or two calendar dates a matched token denotes; [] if nonsense."""
    try:
        if rx is _ISO_RE:
            return [date(int(m.group(1)), int(m.group(2)), int(m.group(3)))]

        if rx is _SLASH_RANGE_RE:
            m1, d1 = int(m.group(1)), int(m.group(2))
            m2 = int(m.group(3)) if m.group(3) else m1
            d2 = int(m.group(4))
            if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
                return []
            first = _nearest_year(m1, d1, ref)
            if first is None:
                return []
            try:
                second = date(first.year, m2, d2)
            except ValueError:
                return [first]
            if second < first:  # a range straddling New Year ("12/30-1/2")
                try:
                    second = date(first.year + 1, m2, d2)
                except ValueError:
                    return [first]
            return [first, second]

        if rx is _SLASH_RE:
            mo, day = int(m.group(1)), int(m.group(2))
            if not 1 <= mo <= 12:
                return []
            year = m.group(3)
            if year:
                y = int(year)
                y = y + 2000 if y < 100 else y
                try:
                    return [date(y, mo, day)]
                except ValueError:
                    return []
            d = _nearest_year(mo, day, ref)
            return [d] if d else []

        if rx is _MONTHNAME_RE:
            word = m.group(1)
            # "may" is a modal verb far more often than a month in this corpus,
            # so an uncapitalised one is not a date.
            if word.lower() == "may" and not word[0].isupper():
                return []
            mo = _MONTHS.get(word.lower()[:3])
            if not mo:
                return []
            day = int(m.group(2))
            if m.group(3):
                try:
                    return [date(int(m.group(3)), mo, day)]
                except ValueError:
                    return []
            d = _nearest_year(mo, day, ref)
            return [d] if d else []
    except (ValueError, TypeError):
        return []
    return []


def _date_tokens(text: str, ref: date) -> list[tuple[int, int, list[date]]]:
    """(start, end, dates) for every date-looking token, longest form winning."""
    spans: list[tuple[int, int, list[date]]] = []
    for rx in (_ISO_RE, _SLASH_RANGE_RE, _SLASH_RE, _MONTHNAME_RE):
        for m in rx.finditer(text):
            if any(m.start() < e and m.end() > s for s, e, _ in spans):
                continue  # already claimed by a more specific pattern
            dates = _dates_from_match(rx, m, ref)
            if dates:
                spans.append((m.start(), m.end(), dates))
    spans.sort()
    return spans


def _classify_prefix(prefix: str) -> tuple[str | None, int, bool]:
    """(gate kind, cue start index, soft) for the cue nearest the end of `prefix`."""
    kind: str | None = None
    start = -1
    for label, rx in _CUES:
        for m in rx.finditer(prefix):
            gap = prefix[m.end():]
            if len(gap) > _MAX_CUE_GAP or _CLAUSE_STOP.search(gap):
                continue
            if m.start() > start:
                kind, start = label, m.start()
    if kind is None:
        return None, -1, False
    lead = prefix[max(0, start - _SOFT_WINDOW):start]
    return kind, start, bool(_SOFT_CUE.search(lead))


def _branches(text: str) -> list[str]:
    """Split on top-level "or" only. See rule 3 in the module docstring."""
    parts = [p.strip() for p in _OR_SPLIT.split(text)]
    return parts or [text]


def parse_trigger_gates(trigger: Any, *, as_of: Any) -> list[Gate]:
    """Every event/date gate in a trigger string. Never raises; [] when unsure."""
    text = trigger if isinstance(trigger, str) else ""
    ref = _as_date(as_of)
    if not text.strip() or ref is None:
        return []
    gates: list[Gate] = []
    try:
        for b_idx, branch in enumerate(_branches(text)):
            if not branch:
                continue
            claimed: list[tuple[int, int]] = []
            for start, end, dates in _date_tokens(branch, ref):
                kind, cue_start, soft = _classify_prefix(branch[:start])
                if kind is None:
                    continue  # a bare date is not a condition (rule 2)
                # Permissive edge of a two-day print window in BOTH directions:
                # "after 7/22-23" fires from the 22nd, "before 8/4-8/6" stays
                # open through the 6th. Either way the choice cannot cost an
                # alert that the text says is live.
                when = min(dates) if kind == "after" else max(dates)
                gates.append(Gate(kind=kind, date=when, event=None, soft=soft,
                                  raw=branch[cue_start:end].strip(),
                                  branch=b_idx))
                claimed.append((cue_start, end))
            for m in _UNDATED_EVENT_RE.finditer(branch):
                if any(m.start() < e and m.end() > s for s, e in claimed):
                    continue  # this cue already produced a dated gate
                _k, cue_start, soft = _classify_prefix(branch[:m.end()])
                gates.append(Gate(
                    kind="after", date=None,
                    event=str(m.group(1)).lower().rstrip("s") or None,
                    soft=soft, raw=m.group(0).strip(), branch=b_idx))
    except Exception:
        return []  # a parser crash must never suppress an alert
    return gates


def _resolve(gates: list[Gate], event_dates: Any) -> list[Gate]:
    """Give undated event gates a date the CALLER knows (e.g. a print date).

    Never inferred here. `event_dates` maps an event noun (or "*") to a date;
    an entry only helps when the caller genuinely knows WHICH print the trigger
    meant, which is why nothing in this module tries to look one up.
    """
    if not isinstance(event_dates, dict) or not event_dates:
        return gates
    lookup: dict[str, date] = {}
    for key, value in event_dates.items():
        d = _as_date(value)
        if d is not None:
            lookup[str(key).strip().lower().rstrip("s")] = d
    if not lookup:
        return gates
    out: list[Gate] = []
    for g in gates:
        if g.date is None and g.event:
            when = lookup.get(g.event) or lookup.get("earning") or lookup.get("*")
            if when is not None:
                g = Gate(kind=g.kind, date=when, event=g.event, soft=g.soft,
                         raw=g.raw, branch=g.branch)
        out.append(g)
    return out


def _evaluate_one(text: str, ref: date, event_dates: Any) -> tuple[bool, list[Gate]]:
    """(suppress, gates) for a single trigger string."""
    gates = _resolve(parse_trigger_gates(text, as_of=ref), event_dates)
    if not gates:
        return False, []
    n_branches = len(_branches(text))
    blocked = {g.branch for g in gates if g.blocks(ref)}
    # EVERY branch blocked, or the trigger still has a live way to be true.
    return len(blocked) >= n_branches >= 1, gates


def evaluate_trigger(trigger: Any, *, as_of: Any, original: Any = None,
                     event_dates: Any = None) -> dict:
    """Should a price-level alert on this trigger be suppressed today?

    Returns {suppress, gates, blocking, reason, note, as_of}. `suppress` is
    False for anything this module is not certain about — an unreadable date, a
    hedged gate, an undated "post-earnings", a parser exception. Pass `original`
    (`would_buy_at_original`) when signal_discipline rewrote the trigger: its
    dead-leg excision takes the rest of the clause with it and can carry the
    event gate away, so the pre-sanitize text is where the gate still lives.
    """
    ref = _as_date(as_of)
    out: dict = {"suppress": False, "gates": [], "blocking": [],
                 "reason": None, "note": None,
                 "as_of": ref.isoformat() if ref else None}
    if ref is None:
        return out

    seen: list[str] = []
    for candidate in (trigger, original):
        if isinstance(candidate, str) and candidate.strip() \
                and candidate.strip() not in seen:
            seen.append(candidate.strip())
    if not seen:
        return out

    suppress = False
    all_gates: list[Gate] = []
    for text in seen:
        try:
            hit, gates = _evaluate_one(text, ref, event_dates)
        except Exception:
            continue  # fail open, always
        suppress = suppress or hit
        for g in gates:
            if g.as_dict() not in [x.as_dict() for x in all_gates]:
                all_gates.append(g)

    out["gates"] = [g.as_dict() for g in all_gates]
    if not suppress:
        return out

    blocking = [g for g in all_gates if g.blocks(ref)]
    out["suppress"] = True
    out["blocking"] = [g.as_dict() for g in blocking]
    forward = [g for g in blocking if g.kind == "after" and g.date]
    if forward:
        when = min(g.date for g in forward)  # type: ignore[type-var]
        days = (when - ref).days
        out["reason"] = "event_gate_not_yet_satisfied"
        out["note"] = (
            f"price is at the stated level, but the trigger conditions on an "
            f"event {days} day(s) out ({when.isoformat()}) — "
            f"\"{forward[0].raw}\". Alert held until then: firing early is the "
            f"AMD 2026-07-17 failure (LP-0693555695), and a fired trigger now "
            f"owes a trigger_reviews row and counts toward a force-drop.")
    else:
        when = max((g.date for g in blocking if g.date), default=None)
        out["reason"] = "trigger_window_expired"
        out["note"] = (
            f"price is at the stated level, but the trigger only held until "
            f"{when.isoformat() if when else 'a date now past'} "
            f"(\"{blocking[0].raw}\"). The stated window has closed — this needs "
            f"re-underwriting, not an alert.")
    return out


def trigger_is_live(trigger: Any, *, as_of: Any, original: Any = None,
                    event_dates: Any = None) -> bool:
    """Convenience inverse of evaluate_trigger()['suppress']."""
    return not evaluate_trigger(trigger, as_of=as_of, original=original,
                                event_dates=event_dates)["suppress"]


def filter_trigger_alerts(alerts: Any, watchlist: Any = None, *, as_of: Any,
                          event_dates: Any = None) -> tuple[list[dict], list[dict]]:
    """Split price-level alerts into (live, held_by_an_event_gate).

    `watchlist` is optional and only used to recover `would_buy_at_original`
    for a trigger that signal_discipline rewrote. Each held alert carries a
    `suppressed_by_gate` block so the suppression is visible rather than silent
    — an invisible filter is how a real miss would go unnoticed.
    """
    live: list[dict] = []
    held: list[dict] = []
    rows = alerts if isinstance(alerts, list) else []
    originals: dict[str, str] = {}
    for w in (watchlist if isinstance(watchlist, list) else []):
        if isinstance(w, dict) and w.get("would_buy_at_original"):
            originals[str(w.get("ticker") or "").upper()] = \
                str(w["would_buy_at_original"])
    for a in rows:
        if not isinstance(a, dict):
            continue
        try:
            verdict = evaluate_trigger(
                a.get("trigger_text"), as_of=as_of,
                original=originals.get(str(a.get("ticker") or "").upper()),
                event_dates=(event_dates or {}).get(
                    str(a.get("ticker") or "").upper())
                if isinstance(event_dates, dict) else None)
        except Exception:
            live.append(a)
            continue
        if verdict["suppress"]:
            held.append({**a, "suppressed_by_gate": {
                "reason": verdict["reason"], "note": verdict["note"],
                "gates": verdict["blocking"]}})
        else:
            live.append(a)
    return live, held


if __name__ == "__main__":  # pragma: no cover - self-check, mirrors the incident
    import json

    RUN_DAY = date(2026, 7, 17)  # the day the AMD alert actually fired
    cases = [
        # THE INCIDENT, verbatim from that week's watchlist.
        ("A confirmed positive reaction to the 7/22-23 Advancing AI event that "
         "reclaims and holds above the 50-DMA, basing $500-520 -- not a bounce "
         "off today's low alone."),
        "near $170 or after the 8/4 earnings print",
        "$345 after the 7/22-23 print",
        "reclaim of $195 post-earnings",
        "near $58",
        "close above $175 with volume confirmation after the 8/4 print",
        "A reclaim of the 50-DMA held on volume, ideally after the 8/4 print",
    ]
    for text in cases:
        v = evaluate_trigger(text, as_of=RUN_DAY)
        print(f"{'HOLD ' if v['suppress'] else 'FIRE '} {text[:70]!r}")
        if v["suppress"]:
            print("        " + json.dumps(v["blocking"]))
