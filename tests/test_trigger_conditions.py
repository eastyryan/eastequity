"""Offline tests for trigger event/date gates — no network, no clock.

THE INCIDENT THESE COVER (2026-08-03, from the agent's own lesson LP-0693555695
written in run 20260717-e6c993): the deterministic trigger check fired on AMD
because price touched the parsed $500 level, while the published `would_buy_at`
was "a confirmed positive reaction to the 7/22-23 Advancing AI event ... basing
$500-520 -- not a bounce off today's low alone" — a compound condition whose
event was five days away and which excluded the pre-event dip in plain words.

Two things are being tested, and the second matters as much as the first:

  * the gate SUPPRESSES the alert on a condition that is not live yet, and
  * it FAILS OPEN on everything it is not certain about. A missed alert is now a
    real cost — tools/engagement made a fired trigger an obligation, so a
    suppression that is wrong hides a live setup rather than merely mis-grading
    one. Most of the cases below are the fail-open direction on purpose.

Every date is passed in; nothing here reads the wall clock or the network.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.trigger_conditions import (  # noqa: E402
    evaluate_trigger,
    filter_trigger_alerts,
    parse_trigger_gates,
    trigger_is_live,
)

# The day the AMD alert actually fired, and the event it fired five days ahead of.
RUN_DAY = date(2026, 7, 17)
AMD_TRIGGER = ("A confirmed positive reaction to the 7/22-23 Advancing AI event "
               "that reclaims and holds above the 50-DMA, basing $500-520 -- not "
               "a bounce off today's low alone.")


# ---------------------------------------------------------------------------
# The four shapes named in the spec
# ---------------------------------------------------------------------------
def test_price_only_trigger_is_untouched():
    """"near $58" has no gate at all — behaviour must be exactly as before."""
    v = evaluate_trigger("near $58", as_of=RUN_DAY)
    assert v["suppress"] is False
    assert v["gates"] == []


def test_or_joined_event_gate_never_suppresses():
    """"near $170 OR after the 8/4 print" — the price branch is live on its own."""
    assert trigger_is_live("near $170 or after the 8/4 earnings print",
                           as_of=RUN_DAY)
    # ... on every date, before and after the event.
    for day in (date(2026, 7, 1), date(2026, 8, 4), date(2026, 9, 1)):
        assert trigger_is_live("near $170 or after the 8/4 earnings print",
                               as_of=day)


def test_and_joined_dated_event_gate_suppresses_until_the_date():
    """"$345 after the 7/22-23 print" — both required, so price alone is not it."""
    t = "$345 after the 7/22-23 print"
    assert evaluate_trigger(t, as_of=date(2026, 7, 17))["suppress"] is True
    assert evaluate_trigger(t, as_of=date(2026, 7, 21))["suppress"] is True
    # Fires from the FIRST day of the print window: the text never says which
    # side of the close the print lands on, and the earlier read cannot cost a
    # live alert.
    assert evaluate_trigger(t, as_of=date(2026, 7, 22))["suppress"] is False
    assert evaluate_trigger(t, as_of=date(2026, 7, 30))["suppress"] is False


def test_undated_post_earnings_gate_is_parsed_but_fails_open():
    """"reclaim of $195 post-earnings" — both required, but WHICH print?

    Resolving that needs a calendar this module deliberately does not have, and
    guessing it wrong suppresses a live alert. So the gate is parsed and
    reported, and the alert still fires.
    """
    v = evaluate_trigger("reclaim of $195 post-earnings", as_of=RUN_DAY)
    assert v["suppress"] is False
    assert [g["event"] for g in v["gates"]] == ["earning"]
    assert v["gates"][0]["date"] is None


def test_an_undated_gate_resolves_when_the_caller_supplies_the_date():
    """The hook exists for a caller that genuinely knows which print was meant."""
    t = "reclaim of $195 post-earnings"
    assert evaluate_trigger(t, as_of=date(2026, 8, 1),
                            event_dates={"earnings": "2026-08-04"})["suppress"]
    assert not evaluate_trigger(t, as_of=date(2026, 8, 5),
                                event_dates={"earnings": "2026-08-04"})["suppress"]


# ---------------------------------------------------------------------------
# The incident itself
# ---------------------------------------------------------------------------
def test_the_amd_2026_07_17_alert_is_suppressed():
    """LP-0693555695: the exact trigger, on the exact day, must not fire."""
    v = evaluate_trigger(AMD_TRIGGER, as_of=RUN_DAY)
    assert v["suppress"] is True
    assert v["reason"] == "event_gate_not_yet_satisfied"
    assert v["blocking"][0]["date"] == "2026-07-22"
    assert "7/22-23" in v["blocking"][0]["raw"]
    assert v["note"] and "5 day" in v["note"]


def test_the_same_amd_trigger_fires_once_its_event_has_happened():
    """Suppression is a DELAY, not a veto — the level still matters after 7/22."""
    assert trigger_is_live(AMD_TRIGGER, as_of=date(2026, 7, 22))
    assert trigger_is_live(AMD_TRIGGER, as_of=date(2026, 7, 24))


def test_reaction_to_phrasing_is_recognised_not_just_after_post():
    """The AMD trigger said "reaction to", never "after" — an after/post-only cue
    list would have missed the very incident this module exists for."""
    gates = parse_trigger_gates("a confirmed reaction to the 8/4 print",
                                as_of=RUN_DAY)
    assert [g.date for g in gates] == [date(2026, 8, 4)]


# ---------------------------------------------------------------------------
# Composition with tools/signal_discipline.sanitize_trigger (which runs first)
# ---------------------------------------------------------------------------
def test_it_composes_with_the_dead_leg_sanitizer():
    """"close above $175 with volume confirmation after the 8/4 print" must keep
    BOTH the price gate and the event gate and lose only the volume leg.

    sanitize_trigger's `_TAIL` used to be a bare `[^,;]*`, which carried the
    event gate away with the dead clause and silently turned a GATED trigger into
    an ungated one — the AMD incident in reverse. `_TAIL` now stops at an event
    cue (after/before/post/once/until/...), so the STORED trigger keeps the gate
    and nothing has to reconstruct it from `would_buy_at_original`.
    """
    from tools.signal_discipline import sanitize_trigger
    from tools.watchlist_triggers import parse_price_level

    original = "close above $175 with volume confirmation after the 8/4 print"
    res = sanitize_trigger(original)

    # Only the volume leg goes. Price gate AND event gate survive in the stored
    # trigger — which matters because the stored text is what the brain reads and
    # what the next run's alert is checked against.
    assert res["changed"]
    assert "volume" not in res["trigger"].lower()
    assert res["trigger"] == "close above $175 after the 8/4 print"
    assert parse_price_level(res["trigger"]) == 175.0
    assert [g.date for g in parse_trigger_gates(res["trigger"], as_of=RUN_DAY)] \
        == [date(2026, 8, 4)]

    v = evaluate_trigger(res["trigger"], as_of=RUN_DAY)
    assert v["suppress"] is True
    assert v["blocking"][0]["date"] == "2026-08-04"
    assert trigger_is_live(res["trigger"], as_of=date(2026, 8, 4))

    # Reading the original alongside remains harmless belt-and-braces: same
    # verdict, so a half-migrated record that still carries only the old text is
    # gated identically.
    assert evaluate_trigger(res["trigger"], as_of=RUN_DAY,
                            original=original)["suppress"] is True


def test_a_trigger_resting_on_the_surviving_volume_read_still_gates():
    """no_supply_pullback is never stripped; the event gate still applies."""
    t = "a no_supply_pullback read near $170 after the 8/4 print"
    assert evaluate_trigger(t, as_of=RUN_DAY)["suppress"] is True


# ---------------------------------------------------------------------------
# AND / OR joining
# ---------------------------------------------------------------------------
def test_and_plus_with_all_mean_both_required():
    for joiner in ("and", "plus", "with"):
        t = f"$345 {joiner} confirmation after the 7/22 print"
        assert evaluate_trigger(t, as_of=RUN_DAY)["suppress"] is True, joiner


def test_only_one_ungated_branch_is_needed_to_fire():
    """Suppression requires EVERY "or" branch to be blocked."""
    t = ("After the 8/4 print, on a hold of the ~$300 shelf, "
         "or a pullback to $280 that holds")
    assert evaluate_trigger(t, as_of=RUN_DAY)["suppress"] is False


def test_every_branch_gated_does_suppress():
    t = "$300 after the 8/4 print, or $280 after the 8/6 print"
    v = evaluate_trigger(t, as_of=RUN_DAY)
    assert v["suppress"] is True
    assert {g["date"] for g in v["blocking"]} == {"2026-08-04", "2026-08-06"}
    # The EARLIER of the two is the one the note counts down to.
    assert "2026-08-04" in v["note"]


def test_uppercase_or_splits_too():
    t = ("A confirmed reaction to the 8/4 pre-market print that holds, "
         "OR the BKR seat closing to free the theme budget at $300")
    assert evaluate_trigger(t, as_of=RUN_DAY)["suppress"] is False


# ---------------------------------------------------------------------------
# "before <date>" windows
# ---------------------------------------------------------------------------
def test_a_before_window_is_live_through_its_stated_date():
    t = "a confirmed daily close above $175 before 8/4"
    assert trigger_is_live(t, as_of=date(2026, 7, 30))
    assert trigger_is_live(t, as_of=date(2026, 8, 4))  # permissive edge


def test_a_before_window_that_has_closed_suppresses():
    t = "a confirmed daily close above $175 before 8/4"
    v = evaluate_trigger(t, as_of=date(2026, 8, 10))
    assert v["suppress"] is True
    assert v["reason"] == "trigger_window_expired"
    assert "8/4" in v["note"]


def test_ahead_of_and_prior_to_are_window_gates():
    for cue in ("ahead of", "prior to"):
        t = f"a pullback to $500 {cue} the 8/5 print"
        assert trigger_is_live(t, as_of=date(2026, 8, 1)), cue
        assert not trigger_is_live(t, as_of=date(2026, 8, 20)), cue


# ---------------------------------------------------------------------------
# Date formats
# ---------------------------------------------------------------------------
def test_iso_dates():
    t = "After the 2026-08-04 BMO print, on confirmation the guide holds at $300"
    assert evaluate_trigger(t, as_of=date(2026, 8, 3))["suppress"] is True
    assert evaluate_trigger(t, as_of=date(2026, 8, 4))["suppress"] is False


def test_hyphenated_post_forms_from_the_real_watchlist():
    for text, when in (
            ("Post-2026-08-06 print, once IV crushes, on a hold above $250",
             date(2026, 8, 6)),
            ("A confirmed post-8/6-earnings reaction that holds above $250",
             date(2026, 8, 6)),
            ("Post-earnings (7/23), if the print confirms the thesis at $500",
             date(2026, 7, 23)),
            ("post-8/4 earnings, only if margin trend holds above $150",
             date(2026, 8, 4))):
        gates = [g for g in parse_trigger_gates(text, as_of=RUN_DAY) if g.date]
        assert gates and min(g.date for g in gates) == when, text


def test_month_name_dates():
    t = "after the July 22 print, if the reaction is positive and holds $500"
    assert evaluate_trigger(t, as_of=date(2026, 7, 20))["suppress"] is True
    assert evaluate_trigger(t, as_of=date(2026, 7, 23))["suppress"] is False


def test_a_bare_slash_range_takes_the_earlier_day_for_an_after_gate():
    gates = parse_trigger_gates("$500 after the 8/4-8/6 print", as_of=RUN_DAY)
    assert [g.date for g in gates] == [date(2026, 8, 4)]


def test_the_year_chosen_is_the_one_nearest_the_run_date():
    """A December trigger naming "1/15" means NEXT January, not eleven months ago.

    Reading it as the past would mark the gate satisfied and fire a month early.
    """
    gates = parse_trigger_gates("$500 after the 1/15 print",
                                as_of=date(2026, 12, 20))
    assert [g.date for g in gates] == [date(2027, 1, 15)]
    gates = parse_trigger_gates("$500 after the 12/20 print",
                                as_of=date(2027, 1, 5))
    assert [g.date for g in gates] == [date(2026, 12, 20)]


# ---------------------------------------------------------------------------
# FAIL OPEN. Everything below must keep firing on price alone.
# ---------------------------------------------------------------------------
def test_moving_average_ratios_are_not_dates():
    """"50/200" is a moving-average cross, and month 50 does not exist."""
    for t in ("a reclaim after the 50/200 golden cross above $170",
              "after the 20/50 crossover near $170"):
        assert parse_trigger_gates(t, as_of=RUN_DAY) == [], t
        assert trigger_is_live(t, as_of=RUN_DAY)


def test_session_counts_and_price_ranges_are_not_dates():
    for t in ("a close above $345-348 held for 2-3 sessions after the pullback",
              "a base above $49-50 that holds 2+ sessions once the tape calms"):
        assert parse_trigger_gates(t, as_of=RUN_DAY) == [], t


def test_a_bare_date_with_no_direction_word_is_not_a_gate():
    """"clarity on the 8/4 print" and "sizing around the 8/5 earnings" are not
    conditions on when the setup goes live, and reading them as gates would
    suppress alerts nobody gated."""
    for t in ("A 5-8% pullback that holds $170, with clarity on the 8/4 print",
              "A pullback that holds support near $50, sizing around (not "
              "through) the 8/5 earnings",
              "A base forming near $1,800 heading into the ~8/4 earnings print"):
        assert trigger_is_live(t, as_of=RUN_DAY), t


def test_an_ideally_hedged_gate_is_a_preference_not_a_condition():
    for t in ("A reclaim of the 50-DMA held on volume near $500, ideally after "
              "the 8/4 print settles",
              "A pullback toward $284, preferably after the 2026-08-26 print"):
        assert trigger_is_live(t, as_of=RUN_DAY), t


def test_an_undated_after_clause_never_suppresses():
    for t in ("A confirmed base above $340 after the current pullback resolves",
              "A reclaim of $48-49 once the CXMT reaction settles",
              "Only after the earnings date is confirmed and the print is past "
              "— then on a hold above $65"):
        assert trigger_is_live(t, as_of=RUN_DAY), t


def test_garbage_input_fails_open():
    for bad in (None, "", "   ", 123, [], {"a": 1}):
        assert evaluate_trigger(bad, as_of=RUN_DAY)["suppress"] is False
        assert trigger_is_live(bad, as_of=RUN_DAY)


def test_a_missing_or_unreadable_as_of_fails_open():
    for bad_day in (None, "", "not-a-date", 42):
        v = evaluate_trigger(AMD_TRIGGER, as_of=bad_day)
        assert v["suppress"] is False
        assert v["as_of"] is None


def test_as_of_accepts_a_string_date():
    """runlib.core.et_date() returns "YYYY-MM-DD", not a date object."""
    assert evaluate_trigger(AMD_TRIGGER, as_of="2026-07-17")["suppress"] is True
    assert evaluate_trigger(AMD_TRIGGER, as_of="2026-07-25")["suppress"] is False


def test_may_the_modal_verb_is_not_the_month():
    t = "a base that may 3 sessions later break above $170 after it forms"
    assert parse_trigger_gates(t, as_of=RUN_DAY) == []


# ---------------------------------------------------------------------------
# The alert-filter helper (ready for the bundle wiring)
# ---------------------------------------------------------------------------
def _alert(ticker: str, text: str) -> dict:
    return {"ticker": ticker, "trigger_text": text, "parsed_level": 500.0,
            "last_price": 501.0, "distance_from_level_pct": 0.2}


def test_filter_splits_live_alerts_from_gated_ones():
    alerts = [_alert("AMD", AMD_TRIGGER),
              _alert("ANET", "near $170 or after the 8/4 earnings print"),
              _alert("NVDA", "near $58")]
    live, held = filter_trigger_alerts(alerts, [], as_of=RUN_DAY)
    assert [a["ticker"] for a in live] == ["ANET", "NVDA"]
    assert [a["ticker"] for a in held] == ["AMD"]
    # Held alerts carry WHY — an invisible filter is how a real miss hides.
    assert held[0]["suppressed_by_gate"]["reason"] == "event_gate_not_yet_satisfied"
    assert held[0]["suppressed_by_gate"]["gates"][0]["date"] == "2026-07-22"


def test_filter_uses_would_buy_at_original_when_the_sanitizer_ate_the_gate():
    alerts = [_alert("ANET", "close above $175")]
    watchlist = [{"ticker": "ANET", "would_buy_at": "close above $175",
                  "would_buy_at_original":
                      "close above $175 with volume confirmation after "
                      "the 8/4 print"}]
    live, held = filter_trigger_alerts(alerts, watchlist, as_of=RUN_DAY)
    assert not live and [a["ticker"] for a in held] == ["ANET"]
    # Without the original there is no gate to see, and it fires as before.
    live2, held2 = filter_trigger_alerts(alerts, [], as_of=RUN_DAY)
    assert [a["ticker"] for a in live2] == ["ANET"] and not held2


def test_filter_never_raises_on_junk():
    live, held = filter_trigger_alerts(
        [None, 3, {"ticker": "X"}, _alert("AMD", AMD_TRIGGER)],  # type: ignore[list-item]
        None, as_of=RUN_DAY)
    assert [a["ticker"] for a in held] == ["AMD"]
    assert [a.get("ticker") for a in live] == ["X"]
    assert filter_trigger_alerts(None, None, as_of=RUN_DAY) == ([], [])


# ---------------------------------------------------------------------------
# The wiring in runlib.analytics.update_watchlist_outcomes — where a false hit
# now force-drops a name off the watchlist.
# ---------------------------------------------------------------------------
def _outcomes(tmp_path, monkeypatch, watchlist, prices, today):
    """Run update_watchlist_outcomes against a temp dashboard dir."""
    import runlib.analytics as analytics

    (tmp_path / "dashboard" / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(analytics, "ROOT", tmp_path)
    monkeypatch.setattr(analytics, "et_date", lambda: today)
    monkeypatch.setattr(analytics, "build_trade_events", lambda: [])
    rows = analytics.update_watchlist_outcomes(watchlist, prices, [])
    return {r["ticker"]: r for r in rows}


def test_a_gated_trigger_does_not_count_as_a_hit(tmp_path, monkeypatch):
    """The force-drop counter must not tick for a setup that was never live.

    Three unbought hit-days force-drop a name (tools/engagement.decay_watchlist).
    On 07-17 AMD's condition was five days from its own event, so this hit-day
    would have been the first of three counted against a miss that never
    happened.
    """
    rows = _outcomes(tmp_path, monkeypatch,
                     [{"ticker": "AMD", "would_buy_at": AMD_TRIGGER}],
                     {"AMD": 501.0}, "2026-07-17")
    amd = rows["AMD"]
    assert amd["hit_buy_level"] is False
    assert "unbought_hit_days" not in amd
    assert amd["parsed_level"] == 500.0          # the level itself still parses
    assert amd["trigger_gate"]["reason"] == "event_gate_not_yet_satisfied"


def test_the_same_name_counts_once_its_event_has_passed(tmp_path, monkeypatch):
    rows = _outcomes(tmp_path, monkeypatch,
                     [{"ticker": "AMD", "would_buy_at": AMD_TRIGGER}],
                     {"AMD": 501.0}, "2026-07-23")
    amd = rows["AMD"]
    assert amd["hit_buy_level"] is True
    assert amd["unbought_hit_days"] == ["2026-07-23"]
    assert "trigger_gate" not in amd


def test_an_ungated_price_trigger_is_graded_exactly_as_before(tmp_path, monkeypatch):
    rows = _outcomes(tmp_path, monkeypatch,
                     [{"ticker": "ANET", "would_buy_at": "near $170"},
                      {"ticker": "GEV", "would_buy_at": "near $170 or after "
                                                        "the 8/4 print"}],
                     {"ANET": 170.5, "GEV": 170.5}, "2026-07-17")
    for tk in ("ANET", "GEV"):
        assert rows[tk]["hit_buy_level"] is True, tk
        assert rows[tk]["unbought_hit_days"] == ["2026-07-17"], tk


def test_the_gate_reads_the_pre_sanitize_original_in_analytics(tmp_path, monkeypatch):
    rows = _outcomes(
        tmp_path, monkeypatch,
        [{"ticker": "ANET", "would_buy_at": "close above $175",
          "would_buy_at_original": "close above $175 with volume confirmation "
                                   "after the 8/4 print"}],
        {"ANET": 175.5}, "2026-07-17")
    assert rows["ANET"]["hit_buy_level"] is False
    assert rows["ANET"]["trigger_gate"]["gates"][0]["date"] == "2026-08-04"


def test_a_stale_gate_record_is_cleared_when_the_gate_clears(tmp_path, monkeypatch):
    """The trigger_gate marker must not outlive the condition it describes."""
    import json

    import runlib.analytics as analytics

    wl = [{"ticker": "AMD", "would_buy_at": AMD_TRIGGER}]
    _outcomes(tmp_path, monkeypatch, wl, {"AMD": 501.0}, "2026-07-17")
    stored = json.loads(
        (tmp_path / "dashboard" / "data" / "watchlist_outcomes.json").read_text())
    assert stored[0]["trigger_gate"]["held_on"] == "2026-07-17"

    rows = _outcomes(tmp_path, monkeypatch, wl, {"AMD": 501.0}, "2026-07-23")
    assert "trigger_gate" not in rows["AMD"]
    assert rows["AMD"]["hit_buy_level"] is True
    assert analytics is not None  # keep the import meaningful for linters
