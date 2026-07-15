"""Self-review, universe review, freshness audit."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import journal
import validator
from tools.portfolio_state import get_portfolio_state
from tools.performance_breakdown import build_performance_breakdown
from tools.universe_scanner import scan_universe
from runlib.core import ROOT, et_date, et_now, json_safe
from runlib.analytics import (
    compute_closed_trades, compute_calibration, recent_improvements,
)
from runlib.brain_io import run_claude
from runlib.publish import redeploy_dashboard

def self_review(run_id: str) -> int:
    closed = compute_closed_trades()
    portfolio = get_portfolio_state()
    runs = []
    for f in sorted((ROOT / "journal" / "runs").glob("*.jsonl"))[-5:]:
        runs.extend(json.loads(line) for line in f.read_text().splitlines())
    # THE LOOP: feed the review its own PRIOR reviews so it must grade last week's
    # stated behavior change - lessons compound instead of evaporating weekly.
    prior_reviews = [n for n in recent_improvements(60)
                     if str(n.get("note", "")).startswith("Weekly self-review:")][:8]
    try:
        from tools.shadow_portfolio import brain_facing_shadow_learning
        shadow = brain_facing_shadow_learning(20)
    except Exception:
        shadow = {}
    try:
        from tools.exit_autopsy import brain_facing_exit_lessons
        exits = brain_facing_exit_lessons(15)
    except Exception:
        exits = {}
    try:
        from tools.learning_adopt import brain_facing_adopted_lessons
        adopted = brain_facing_adopted_lessons(15)
    except Exception:
        adopted = {}
    try:
        from tools.calibration_gate import brain_facing_calibration_status
        cal_status = brain_facing_calibration_status()
    except Exception:
        cal_status = {}
    bundle = {"closed_trades": closed, "portfolio": portfolio,
              "breakdowns": build_performance_breakdown(closed),
              "calibration": compute_calibration(closed),
              "calibration_status": cal_status,
              "prior_self_reviews": prior_reviews,
              "recent_run_summaries": runs[-40:],
              "shadow_learning": shadow,
              "exit_lessons": exits,
              "adopted_lessons": adopted}
    review_file = ROOT / "state" / f"review_{run_id}.json"
    review_file.write_text(json.dumps(bundle, indent=2, default=str))

    prompt = (
        "Weekly self-review for East Equity Agent (no trading this run). Read CLAUDE.md, "
        f"then the audit bundle at {review_file}. FIRST: grade your previous review's "
        "stated behavior change (prior_self_reviews, newest first) - did you actually do "
        "it this week, and did it help? Quote it. Then audit the week honestly: for each "
        "open position, is the original thesis tracking or drifting? For each closed "
        "trade, was the exit right in hindsight? "
        "SHADOW LEARNING: review regret_misses (skips that would have worked) and "
        "good_skips (skips that saved you) — what process change follows? "
        "EXIT LESSONS: note binding process_fail patterns. "
        "CALIBRATION_STATUS: state the phase (anecdote/caution/binding). "
        "ADOPTED LESSONS: confirm you still honor them. "
        "Which predictions were wrong and WHY - bad data, bad reasoning, or bad luck? "
        "Use the breakdowns and calibration numbers, not vibes. End with a section "
        "titled 'Self-review:' containing 4-8 plain-English sentences for the public "
        "dashboard: what you got right, what you got wrong, whether last week's change "
        "stuck, and the ONE change you are making next week."
    )
    try:
        out = run_claude(prompt)  # pinned model + tool allowlist, with retry
    except Exception as e:
        print(f"self-review failed: {str(e)[:800]}")
        return 1
    m = re.search(r"Self-review:\s*(.+)", out, re.DOTALL)
    summary = (m.group(1).strip() if m else out.strip())[:2000]
    journal.log_improvement(f"Weekly self-review: {summary}", run_id)
    # Weekly adopt pipeline: harvest improvement notes → soft lessons auto-adopted
    try:
        from tools.learning_adopt import run_weekly_adopt_pipeline
        adopt = run_weekly_adopt_pipeline()
        print(f"  learning adopt: {adopt.get('auto_adopted', 0)} soft lessons, "
              f"{adopt.get('hard_pending', 0)} hard proposals pending")
        journal.log_improvement(
            f"[learning-adopt] auto_adopted={adopt.get('auto_adopted')} "
            f"hard_pending={adopt.get('hard_pending')} "
            f"ids={adopt.get('new_ids')}", run_id)
    except Exception as e:
        print(f"  (learning adopt pipeline skipped: {e})")
    # Mark shadows + post-exit runners with latest prices if available
    try:
        from tools.shadow_portfolio import mark_shadows, brain_facing_shadow_learning
        from tools.post_exit_runners import mark_post_exit_runners
        from tools.prices import get_closes
        import json as _json
        from pathlib import Path as _P
        # Best-effort marks on held shadow tickers + post-exit tracks
        sh = brain_facing_shadow_learning(50)
        tickers = list({
            *(r.get("ticker") for r in (sh.get("regret_misses") or [])),
            *(r.get("ticker") for r in (sh.get("good_skips") or [])),
            *(r.get("ticker") for r in (sh.get("open_running_best") or [])),
        })
        sp = _P(ROOT / "data" / "shadow_portfolio.json")
        if sp.exists():
            for p in (_json.loads(sp.read_text()).get("positions") or []):
                if p.get("ticker"):
                    tickers.append(p["ticker"])
        pxf = _P(ROOT / "data" / "post_exit_runners.json")
        if pxf.exists():
            for p in (_json.loads(pxf.read_text()).get("tracking") or []):
                if p.get("ticker"):
                    tickers.append(p["ticker"])
        tickers = [t for t in dict.fromkeys(tickers) if t]
        if tickers:
            px = get_closes(tickers) or {}
            mark_shadows(px)
            mark_post_exit_runners(px)
    except Exception as e:
        print(f"  (shadow/post-exit mark skipped: {e})")
    _write_review_to_dashboard = ROOT / "dashboard" / "data" / "latest.json"
    if _write_review_to_dashboard.exists():
        d = json.loads(_write_review_to_dashboard.read_text())
        d["improvements"] = ([{"date": et_date(),
                               "note": f"Weekly self-review: {summary}"}]
                             + d.get("improvements", []))[:30]
        _write_review_to_dashboard.write_text(json.dumps(d, indent=2, default=str))
    redeploy_dashboard()
    print("self-review complete and published")
    return 0


def run_freshness_audit(run_id: str) -> int:
    """Audit fundamentals freshness for EVERY universe name and publish the artifact.
    Runs weekly after the universe review and on demand via --freshness-audit. A
    stale name here means our XBRL extraction lags that company's latest filed
    report - the exact class of bug behind the DELL $23.4B incident."""
    from tools.freshness_audit import audit_universe
    audit = audit_universe(cross_check=True, progress=True)
    stale, errors = audit.get("stale_tickers", []), audit.get("error_tickers", [])
    mism = audit.get("value_mismatch_vs_yfinance", [])
    print(f"  audited {audit.get('audited')} names: {audit.get('fresh')} fresh, "
          f"{len(stale)} stale, {len(errors)} errors, {len(mism)} value mismatches")
    if stale or errors or mism:
        journal.log_improvement(
            f"Universe freshness audit: {len(stale)} stale ({stale}), {len(errors)} "
            f"errors ({errors}), {len(mism)} yfinance mismatches ({mism}) out of "
            f"{audit.get('audited')} names - investigate before trusting these names' "
            f"fundamentals.", run_id)
    else:
        journal.log_improvement(
            f"Universe freshness audit: all {audit.get('audited')} names verified "
            f"current against their latest SEC filings (+ yfinance cross-check).", run_id)
    redeploy_dashboard()
    return 0 if not stale else 1


def below_min_cap(tickers: list, floor_usd: float) -> set:
    """Added universe names under the market-cap floor. Fail-open: if EVERY lookup
    fails (feed down), return empty rather than wiping a good review."""
    if not tickers or not floor_usd:
        return set()
    bad, got_any = set(), False
    try:
        import yfinance as yf
        for t in tickers:
            try:
                mcap = yf.Ticker(t).fast_info.get("marketCap")
                if isinstance(mcap, (int, float)) and mcap > 0:
                    got_any = True
                    if mcap < floor_usd:
                        bad.add(t.upper())
            except Exception:
                continue
    except Exception:
        return set()
    return bad if got_any else set()


def unpriceable(tickers: list) -> set:
    """Names that return NO live price data (delisted / bad ticker). Fail-open: if the
    whole download fails (feed down), return empty so a good review is never wiped."""
    if not tickers:
        return set()
    try:
        import yfinance as yf
        data = yf.download(list(tickers), period="5d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
    except Exception:
        return set()
    bad, got_any = set(), False
    for t in tickers:
        try:
            df = data[t].dropna() if len(tickers) > 1 else data.dropna()
            if len(df) and float(df["Close"].iloc[-1]) > 0:
                got_any = True
            else:
                bad.add(t.upper())
        except Exception:
            bad.add(t.upper())
    return bad if got_any else set()  # nothing priced at all -> feed down, fail open


# ---------------------------------------------------------------------------
# Weekly universe review: the agent curates its own watchable universe.
# ---------------------------------------------------------------------------
def universe_log_append(entry: dict) -> None:
    """Append to the public universe changelog — INCLUDING failed/rejected
    reviews, so the dashboard explains why the universe did not change instead
    of rendering an eternally-empty section (the log used to be written only on
    a fully successful review, which had never happened)."""
    log_file = ROOT / "dashboard" / "data" / "universe_log.json"
    try:
        ulog = json.loads(log_file.read_text()) if log_file.exists() else []
    except Exception:
        ulog = []
    ulog.append(entry)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(json.dumps(ulog[-100:], indent=2))
    except Exception as e:
        print(f"  (universe_log write failed: {e})")


def universe_review(run_id: str) -> int:
    """The agent researches the broad market (web search enabled) and proposes
    edits to data/universe.json. Deterministic guardrails cap what can change:
    held and watchlist names are untouchable, size stays 150-220, max 10 adds and
    10 removals per review, tech/AI bias preserved."""
    universe_file = ROOT / "data" / "universe.json"
    current = json.loads(universe_file.read_text())
    portfolio = get_portfolio_state()
    protected = {p["ticker"].upper() for p in portfolio.get("positions", [])}
    latest_file = ROOT / "dashboard" / "data" / "latest.json"
    if latest_file.exists():
        protected |= {w.get("ticker", "").upper()
                      for w in json.loads(latest_file.read_text()).get("watchlist", [])}
    protected.discard("")

    print("  • fresh sector scan for the review...")
    scan = scan_universe(top_n=10)
    # Broad-market discovery sweep: candidates OUTSIDE the current universe so the
    # review curates from the whole market, not just names the model already knows.
    print("  • discovery sweep (broad market, ex-universe)...")
    try:
        from tools.discovery_screen import run_discovery
        discovery = run_discovery()
    except Exception as e:
        discovery = {"status": "error", "reason": str(e)[:150]}

    bundle = {
        "as_of_et": et_date(),
        "current_universe": current,
        "protected_tickers_never_remove": sorted(protected),
        "sector_relative_strength": scan.get("sector_relative_strength"),
        "top_setups_now": scan.get("top_setups"),
        "discovery_candidates": discovery,
        "track_record_breakdowns": build_performance_breakdown(compute_closed_trades()),
    }
    review_file = ROOT / "state" / f"universe_review_{run_id}.json"
    review_file.write_text(json.dumps(bundle, indent=2, default=str))

    prompt = (
        f"Today's date in the US market timezone (ET) is {et_date()} - use THIS date, not "
        f"the UTC run_date, in any prose. "
        "Weekly UNIVERSE REVIEW for East Equity Agent (no trading this run). Read CLAUDE.md, "
        f"then the bundle at {review_file}. Your job: curate the watchable universe honestly. "
        "Use WebSearch to research the broad market: which quality names are emerging as "
        "leaders (new highs, accelerating estimates, institutional accumulation) that we do "
        "NOT track, and which current universe names have lost leadership (persistent "
        "downtrends, broken growth, fading relevance)? Keep a strong AI/technology bias "
        "(at least ~70% of names) but market leaders from any sector earn a place. "
        "Constraints (code-enforced): US-listed common equities only, MINIMUM $1B market "
        "cap (sub-billion adds are dropped), no leveraged/inverse "
        "products, never remove the protected tickers, max 10 adds and 10 removals, final "
        "size 150-220 names. The bundle's discovery_candidates section (when present) is a "
        "broad-market momentum/RS sweep of ~600 liquid names OUTSIDE the current universe - "
        "treat it as your candidate shortlist so curation picks from the whole market, not "
        "just names you already know. Removing a name is healthy - a universe that only grows is a "
        "museum. ALSO maintain the AI-exposure map (data/ai_exposure.json in the bundle "
        "context): classify every ADDED name and refresh any existing label your research "
        "contradicts, using exactly one of ai_supplier / ai_beneficiary / ai_neutral / "
        "ai_at_risk with a <=160-char reason written as the plain retail bear/bull case "
        "(e.g. 'frontier LLMs teach languages free'). Output a ```json block: "
        "{\"sectors\": {<full updated sectors map>}, "
        "\"added\": [..], \"removed\": [..], "
        "\"ai_exposure_updates\": {\"TICK\": {\"exposure\": \"...\", \"reason\": \"...\"}}, "
        "\"rationale\": \"3-5 plain sentences for the public improvement log\"}."
    )
    universe_size = len({t.upper() for ts in current["sectors"].values() for t in ts})
    _fail_entry = {"date": et_date(), "added": [], "removed": [],
                   "droppedunpriceable": [], "size": universe_size}
    try:
        out = run_claude(prompt)  # pinned model + tool allowlist, with retry
    except Exception as e:
        print(f"universe review failed: {str(e)[:600]}")
        universe_log_append({**_fail_entry, "status": "error",
                              "rationale": f"Weekly review failed to run: {str(e)[:200]}"})
        return 1
    blocks = re.findall(r"```json\s*(.*?)```", out, re.DOTALL)
    data = None
    for block in reversed(blocks):
        try:
            cand = json.loads(block)
            if isinstance(cand, dict) and "sectors" in cand:
                data = cand
                break
        except json.JSONDecodeError:
            continue
    if data is None:
        print("universe review: no parsable proposal, universe unchanged")
        universe_log_append({**_fail_entry, "status": "unparsable",
                              "rationale": "Review produced no machine-readable proposal; "
                                           "universe unchanged."})
        return 1

    # Deterministic guardrails - the agent proposes, code disposes.
    new_tickers = {t.upper() for ts in data["sectors"].values() for t in ts}
    old_tickers = {t.upper() for ts in current["sectors"].values() for t in ts}
    added, removed = new_tickers - old_tickers, old_tickers - new_tickers
    problems = []
    if not 150 <= len(new_tickers) <= 220:
        problems.append(f"size {len(new_tickers)} outside 150-220")
    if len(added) > 10 or len(removed) > 10:
        problems.append(f"too many changes (+{len(added)}/-{len(removed)}, max 10 each)")
    if removed & protected:
        problems.append(f"tried to remove protected: {sorted(removed & protected)}")
    forbidden = set(validator.load_config()["hard_rules"]["forbidden_ticker_patterns"])
    if new_tickers & forbidden:
        problems.append(f"forbidden products: {sorted(new_tickers & forbidden)}")
    if any(not re.fullmatch(r"[A-Z]{1,5}", t) for t in new_tickers):
        problems.append("invalid ticker format present")
    if problems:
        print(f"universe review REJECTED by guardrails: {problems}")
        journal.log_improvement(
            f"Universe review rejected by guardrails ({'; '.join(problems)}) - no changes.",
            run_id)
        universe_log_append({**_fail_entry, "status": "rejected",
                              "rationale": f"Rejected by guardrails: {'; '.join(problems)}. "
                                           f"Universe unchanged."})
        return 1

    # Price-validate ADDED names: the review has no web access in the cloud sandbox and
    # can propose a delisted/unpriceable ticker (e.g. PSTG). Drop any added name that
    # returns no live price data before it can poison the universe. Fail-open: if the
    # whole check fails (yfinance down), keep the names rather than wipe a good review.
    dropped = unpriceable(sorted(added)) if added else set()
    # $1B market-cap floor: sub-billion adds are dropped the same way (hard rule).
    cap_floor = validator.load_config()["hard_rules"].get("min_market_cap_usd", 0)
    small = below_min_cap(sorted(added - dropped), cap_floor) if added else set()
    if small:
        print(f"  dropping sub-${cap_floor/1e9:.0f}B added names: {sorted(small)}")
    dropped |= small
    if dropped:
        print(f"  dropping unpriceable/sub-cap added names: {sorted(dropped)}")
        data["sectors"] = {s: [t for t in ts if t.upper() not in dropped]
                           for s, ts in data["sectors"].items()}
        added = added - dropped
        new_tickers = new_tickers - dropped

    # AI-exposure map maintenance (enum-guarded): the review classifies added names
    # and refreshes labels its research contradicts; code validates and merges so a
    # malformed label can never corrupt the business-reality layer the scanner reads.
    try:
        exp_file = ROOT / "data" / "ai_exposure.json"
        exp = json.loads(exp_file.read_text()) if exp_file.exists() else {
            "description": "per-name AI exposure", "valid_exposures":
            ["ai_supplier", "ai_beneficiary", "ai_neutral", "ai_at_risk"], "labels": {}}
        valid = set(exp.get("valid_exposures") or
                    ["ai_supplier", "ai_beneficiary", "ai_neutral", "ai_at_risk"])
        applied, rejected_lbl = 0, []
        for tk, upd in (data.get("ai_exposure_updates") or {}).items():
            tk = str(tk).upper()
            e = (upd or {}).get("exposure")
            reason = str((upd or {}).get("reason") or "")[:160]
            if tk in new_tickers and e in valid and reason:
                exp["labels"][tk] = {"exposure": e, "reason": reason}
                applied += 1
            else:
                rejected_lbl.append(tk)
        # prune labels for names no longer in the universe
        exp["labels"] = {t: v for t, v in exp["labels"].items() if t in new_tickers}
        exp["last_reviewed"] = et_date()
        exp_file.write_text(json.dumps(exp, indent=1))
        if applied or rejected_lbl:
            print(f"  ai_exposure: {applied} labels updated"
                  + (f", rejected {rejected_lbl}" if rejected_lbl else ""))
    except Exception as e:
        print(f"  (ai_exposure maintenance failed: {e})")

    current["sectors"] = data["sectors"]
    current["last_reviewed"] = et_date()
    universe_file.write_text(json.dumps(current, indent=2))
    drop_note = f" (dropped unpriceable: {sorted(dropped)})" if dropped else ""
    note = (f"Weekly universe review: added {sorted(added) if added else 'none'}, "
            f"removed {sorted(removed) if removed else 'none'} "
            f"({len(new_tickers)} names){drop_note}. {data.get('rationale', '')}")
    journal.log_improvement(note, run_id)
    # Persistent public log of universe changes (for the dashboard changelog).
    universe_log_append({"date": et_date(), "status": "applied",
                          "added": sorted(added), "removed": sorted(removed),
                          "droppedunpriceable": sorted(dropped), "size": len(new_tickers),
                          "rationale": data.get("rationale", "")})
    if latest_file.exists():
        d = json.loads(latest_file.read_text())
        d["improvements"] = ([{"date": et_date(), "note": note}]
                             + d.get("improvements", []))[:30]
        latest_file.write_text(json.dumps(d, indent=2, default=str))
    # Weekly full-universe freshness sweep rides the same Sunday slot: audit the
    # FINAL curated universe so every name - not just this week's focus set - is
    # verified current against its latest SEC filing before Monday's pre-market.
    try:
        from tools.freshness_audit import audit_universe
        audit = audit_universe(cross_check=True, progress=False)
        a_stale = audit.get("stale_tickers", [])
        print(f"  freshness sweep: {audit.get('fresh')}/{audit.get('audited')} fresh"
              + (f", STALE: {a_stale}" if a_stale else ""))
        if a_stale:
            journal.log_improvement(
                f"Freshness sweep found stale fundamentals for {a_stale} - "
                f"do not trust these names' fundamentals until resolved.", run_id)
    except Exception as e:
        print(f"  (freshness sweep failed: {e})")
    redeploy_dashboard()
    print(f"universe updated: +{len(added)} -{len(removed)} = {len(new_tickers)} names")
    return 0
