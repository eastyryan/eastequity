"use client";

import Link from "next/link";
import { useState } from "react";
import type { HistoryPoint, Latest } from "@/lib/types";
import { formatEtStamp } from "@/lib/format";

// Data arrives as props, rendered from the JSON committed in dashboard/data.
//
// The original design re-fetched latest.json from raw.githubusercontent.com on
// mount to stay live between deploys. That only works for a public repo — this
// one is private, so every one of those requests 404'd and the page silently
// fell back to its server-rendered props anyway. Removed rather than left to
// fail on every load. Freshness already works without it: a trading run commits
// latest.json without a [vercel skip] marker, which redeploys this page.
const START = 10000;
const GREEN = "var(--ee-up)";
const RED = "var(--ee-down)";

const money = (n: number) =>
  "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const pct = (n: number) => (n >= 0 ? "+" : "") + Number(n).toFixed(2) + "%";

const col = (n: number) => (n >= 0 ? GREEN : RED);

// For CPI, yields, VIX and credit spreads, "rising" is the hostile direction —
// hence red up / green down, the inverse of a price.
const dcol = (dir?: string) => {
  const d = (dir || "").toLowerCase();
  if (d === "rising") return RED;
  if (d === "falling") return GREEN;
  return "var(--ee-muted)";
};

// The agent writes prose with occasional markdown emphasis; strip the markers
// rather than render literal asterisks.
const strip = (s?: string | null) => (s || "").replace(/\*\*/g, "").replace(/[`*]/g, "");

type Chart = {
  eqPath: string;
  eqArea: string;
  benchPath: string;
  zeroY: string;
  color: string;
  fill: string;
  xLabels: string[];
  eqEnd: string;
  benchEnd: string;
};

function buildChart(H: HistoryPoint[]): Chart | null {
  if (!Array.isArray(H) || H.length === 0) return null;

  const eq = H.map((d) => d.equity);
  const firstEq = eq[0];
  if (!firstEq) return null;

  // Benchmark closes are absent on non-trading days; carry the last known close
  // forward so the comparison line stays continuous instead of dropping to zero.
  let lastB: number | null = null;
  const bench = H.map((d) => {
    if (d.benchmark_close != null) lastB = d.benchmark_close;
    return lastB;
  });
  const firstB = bench.find((x) => x != null) ?? 1;

  const eqPct = eq.map((v) => (v / firstEq - 1) * 100);
  const bPct = bench.map((v) => (v == null ? 0 : (v / firstB - 1) * 100));

  const all = eqPct.concat(bPct);
  let mn = Math.min(...all);
  let mx = Math.max(...all);
  if (mn === mx) {
    mn -= 1;
    mx += 1;
  }
  const pad = (mx - mn) * 0.18;
  mn -= pad;
  mx += pad;

  const W = 1000;
  const HH = 260;
  const pl = 8;
  const pr = 8;
  const pt = 14;
  const pb = 16;
  const plotW = W - pl - pr;
  const plotH = HH - pt - pb;
  const n = H.length;

  const X = (i: number) => pl + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const Y = (v: number) => pt + (1 - (v - mn) / (mx - mn)) * plotH;
  const line = (arr: number[]) =>
    arr.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");

  const eqPath = line(eqPct);
  const eqArea =
    eqPath +
    " L" +
    X(n - 1).toFixed(1) +
    " " +
    (pt + plotH).toFixed(1) +
    " L" +
    X(0).toFixed(1) +
    " " +
    (pt + plotH).toFixed(1) +
    " Z";

  const end = eqPct[n - 1];

  // History grows by a point per session — show at most ~8 dates so the axis
  // stays readable a year from now.
  const step = Math.max(1, Math.ceil(n / 8));
  const xLabels = H.filter((_, i) => i % step === 0 || i === n - 1).map((d) => {
    const [, mm, dd] = d.date.split("-");
    return mm + "/" + dd;
  });

  return {
    eqPath,
    eqArea,
    benchPath: line(bPct),
    zeroY: Y(0).toFixed(1),
    color: end >= 0 ? GREEN : RED,
    fill: end >= 0 ? "var(--ee-up-fill)" : "var(--ee-down-fill)",
    xLabels,
    eqEnd: pct(end),
    benchEnd: pct(bPct[n - 1]),
  };
}

function toggleTheme() {
  const el = document.documentElement;
  const dark = !el.classList.contains("dark");
  el.classList.toggle("dark", dark);
  el.setAttribute("data-theme", dark ? "dark" : "light");
  try {
    localStorage.setItem("ee-theme", dark ? "dark" : "light");
  } catch {
    /* private mode — theme just won't persist */
  }
}

// Status signals surface ONLY when something is actually wrong: stale/degraded
// data, an active risk halt, a safety-layer exit, or a pipeline missing runs.
// A healthy run renders nothing here.
type Alert = { tone: "bad" | "warn"; label: string; text: string };

function alertsFor(d: Latest): Alert[] {
  const out: Alert[] = [];
  const dq = d.data_quality;

  if (dq?.source === "degraded_empty") {
    out.push({
      tone: "bad",
      label: "DEGRADED DATA",
      text: d.stale_data_notice || dq.note || "Live feeds were unreachable for this run.",
    });
  } else if (dq?.stale) {
    const age = dq.age_hours != null ? ` ${Math.round(dq.age_hours)}h old` : "";
    out.push({
      tone: "warn",
      label: `STALE DATA${age.toUpperCase()}`,
      text: d.stale_data_notice || dq.note || "Prices behind this run are not current.",
    });
  } else if (!dq && d.stale_data_notice) {
    out.push({ tone: "bad", label: "DEGRADED DATA", text: d.stale_data_notice });
  }

  (d.risk_halts ?? []).forEach((h) =>
    out.push({ tone: "bad", label: "RISK HALT", text: strip(h) }),
  );

  (d.forced_exits ?? []).forEach((fe) =>
    out.push({
      tone: "warn",
      label: "SAFETY EXIT",
      text:
        fe.reason === "stop_loss_breached"
          ? `${fe.ticker} — stop loss breached (last $${fe.last_price ?? "?"} vs stop $${fe.stop_loss ?? "?"})`
          : fe.reason === "horizon_expired"
            ? `${fe.ticker} — holding horizon expired (${fe.days_held ?? "?"}d of ${fe.horizon ?? "?"}d)`
            : `${fe.ticker} — ${strip(fe.reason)}`,
    }),
  );

  const h = d.health;
  if (h && (h.status ?? "ok") !== "ok") {
    const missed = h.missed ? `${h.missed} missed · ` : "";
    out.push({
      tone: "warn",
      label: "PIPELINE",
      text: `${missed}${h.completed_scheduled_runs ?? 0}/${h.expected_runs_so_far ?? 0} scheduled runs today${
        h.bundle_age_hours != null ? ` · data bundle ${h.bundle_age_hours}h old` : ""
      }`,
    });
  }

  return out;
}

export default function Arena({ latest, history }: { latest: Latest; history: HistoryPoint[] }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const d = latest;
  const hist = history;

  const perf = d.performance ?? null;
  const eqNow = d.portfolio?.total_equity_usd ?? 0;
  const totalRet = (eqNow / START - 1) * 100;
  const positions = d.portfolio?.positions ?? [];
  const ch = buildChart(hist);
  const alerts = alertsFor(d);

  const ms = d.macro_snapshot ?? null;
  const macro = [
    { label: "CPI YOY", o: ms?.cpi_yoy_pct, unit: "%" },
    { label: "10Y YIELD", o: ms?.ten_year_yield, unit: "%" },
    { label: "VIX", o: ms?.vix, unit: "" },
    { label: "10Y–2Y", o: ms?.yield_curve_10y2y, unit: "pp" },
    { label: "HY SPREAD", o: ms?.hy_credit_spread, unit: "%" },
  ].map((m) => ({
    label: m.label,
    value: m.o ? m.o.latest + m.unit : "—",
    direction: (m.o?.direction || "").toUpperCase(),
    dirColor: dcol(m.o?.direction),
  }));

  const outcomes: Record<string, { latest_price?: number | null; move_pct_since_watched?: number | null }> =
    {};
  (d.watchlist_outcomes ?? []).forEach((o) => {
    outcomes[o.ticker] = o;
  });

  type Tick = { kind: string; ticker: string; price: string; chg: string; chgColor: string };
  const tickerItems: Tick[] = [];
  positions.forEach((p) =>
    tickerItems.push({
      kind: "OPEN",
      ticker: p.ticker,
      price: p.last_price ? money(p.last_price) : "",
      chg: p.unrealized_pct != null ? pct(p.unrealized_pct) : "",
      chgColor: col(p.unrealized_pct ?? 0),
    }),
  );
  if (!positions.length) {
    tickerItems.push({
      kind: "BOOK",
      ticker: "FLAT",
      price: money(eqNow),
      chg: "100% CASH",
      chgColor: "var(--ee-amber)",
    });
  }
  (d.watchlist ?? []).forEach((w) => {
    const o = outcomes[w.ticker] ?? {};
    tickerItems.push({
      kind: "WATCH",
      ticker: w.ticker,
      price: o.latest_price != null ? money(o.latest_price) : "",
      chg: o.move_pct_since_watched != null ? pct(o.move_pct_since_watched) : "",
      chgColor: col(o.move_pct_since_watched ?? 0),
    });
  });
  const tickerLoop = tickerItems.concat(tickerItems);

  const watchlist = (d.watchlist ?? []).map((w) => {
    const o = outcomes[w.ticker] ?? {};
    const open = !!expanded[w.ticker];
    return {
      ticker: w.ticker,
      oneLine: strip(w.one_line),
      thoughts: strip(w.thoughts),
      buyAt: strip(w.would_buy_at),
      price: o.latest_price != null ? money(o.latest_price) : "—",
      chg: o.move_pct_since_watched != null ? pct(o.move_pct_since_watched) : "",
      chgColor: col(o.move_pct_since_watched ?? 0),
      open,
    };
  });

  const feed = (d.trade_events ?? [])
    .slice()
    .reverse()
    .map((f) => ({
      date: f.date ? f.date.slice(5).replace("-", "/") : "",
      action: f.action === "BUY" ? "BUY" : "SELL",
      actColor: f.action === "BUY" ? GREEN : RED,
      ticker: f.ticker,
      price: f.price != null ? "@ " + money(f.price) : "",
      note: f.verdict ? f.verdict.toUpperCase() : "FILLED",
    }));

  const calibSrc = d.calibration?.by_confidence ?? {};
  const calib = Object.keys(calibSrc).map((k) => {
    const c = calibSrc[k];
    const lo = k.split("-").map((x) => Math.round(parseFloat(x) * 100));
    return {
      bucket: lo[0] + "–" + lo[1] + "%",
      trades: c.trades,
      win: (c.win_rate_pct != null ? c.win_rate_pct.toFixed(0) : "—") + "%",
      gap: c.calibration_gap_pct != null ? pct(c.calibration_gap_pct) : "—",
      gapColor: col(c.calibration_gap_pct ?? 0),
    };
  });

  const thoughts = [
    { tag: "EASTEQUITY · MARKET DIGEST",
      time: formatEtStamp(d.generated_at) || d.as_of_et || "",
      text: strip(d.commentary) },
  ].concat(
    (d.closed_trades ?? []).map((t) => ({
      tag:
        "EASTEQUITY · " +
        t.ticker +
        " EXIT · " +
        (t.verdict || "").toUpperCase() +
        " · " +
        ((t.total_pnl_usd ?? 0) >= 0 ? "+" : "−") +
        money(Math.abs(t.total_pnl_usd ?? 0)),
      time:
        t.closed_at +
        " · " +
        t.days_held +
        "D HELD · " +
        Number(t.r_multiple ?? 0).toFixed(2) +
        "R · CONF " +
        Math.round((t.confidence ?? 0) * 100) +
        "%",
      text: strip(t.thesis),
    })),
  );

  const improvements = (d.improvements ?? []).slice(0, 6);
  const regimeLabel = (d.macro_regime_hint?.label ?? "supportive").toUpperCase();
  const regimeScore = d.macro_regime_hint?.score_0_to_5 ?? 4;

  return (
    <div className="ee-root" style={{ minHeight: "100vh" }}>
      {/* ---------------- Sticky header ---------------- */}
      <div
        style={{
          position: "sticky",
          top: 0,
          zIndex: 50,
          backdropFilter: "blur(14px)",
          WebkitBackdropFilter: "blur(14px)",
          background: "var(--ee-bg-glass)",
        }}
      >
        <div className="ee-hdr">
          <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/avatar.jpg"
              alt="EastEquity"
              style={{ width: 42, height: 42, borderRadius: "50%", objectFit: "cover", display: "block" }}
            />
          </div>
          <nav className="ee-nav">
            <a href="#board">Leaderboard</a>
            <a href="#floor">The Floor</a>
            <a href="#machine">The Machine</a>
            <a href="#log">The Log</a>
            <Link href="/ledger">The Ledger</Link>
            <Link href="/learning">Study Hall</Link>
          </nav>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <button
              onClick={toggleTheme}
              aria-label="Toggle theme"
              className="ee-themebtn"
              style={{
                background: "none",
                border: "1px solid var(--ee-hair14)",
                color: "var(--ee-muted)",
                width: 38,
                height: 38,
                borderRadius: "50%",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}
            >
              <svg
                className="ee-when-dark"
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              >
                <circle cx="12" cy="12" r="4.4" />
                <path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8" />
              </svg>
              <svg
                className="ee-when-light"
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M20.5 14.2A8.5 8.5 0 1 1 9.8 3.5a7 7 0 1 0 10.7 10.7z" />
              </svg>
            </button>
          </div>
        </div>
      </div>

      {/* ---------------- Hero ---------------- */}
      <section
        style={{
          position: "relative",
          overflow: "hidden",
          minHeight: "calc(100vh - 73px)",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Both scenes and both veils stay mounted and crossfade on the theme
            clock — same landscape, dawn to dusk. */}
        <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="ee-hero-layer ee-layer-day" src="/hero-day.jpg" alt="" />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="ee-hero-layer ee-layer-night" src="/hero-night.jpg" alt="" />
          <div className="ee-hero-layer ee-layer-day ee-veil-day" />
          <div className="ee-hero-layer ee-layer-night ee-veil-night" />
        </div>

        <div
          style={{
            position: "relative",
            flex: 1,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
            maxWidth: 1140,
            width: "100%",
            margin: "0 auto",
            padding: "80px 32px",
            textAlign: "center",
            pointerEvents: "none",
          }}
        >
          <h1
            className="ee-serif"
            style={{
              fontSize: "clamp(56px, 8.5vw, 118px)",
              lineHeight: 1.02,
              letterSpacing: "-0.01em",
              color: "var(--ee-hero)",
              textShadow: "var(--ee-hero-shadow)",
            }}
          >
            EastEquity&apos;s
            <br />
            <i>Trading Agent.</i>
          </h1>
          <div
            style={{
              marginTop: 44,
              display: "flex",
              justifyContent: "center",
              alignItems: "center",
              gap: 26,
              flexWrap: "wrap",
            }}
          >
            <span style={{ fontSize: 11, letterSpacing: "0.32em", color: "var(--ee-powered)" }}>POWERED BY</span>
            <span style={{ fontSize: 16, fontWeight: 700, color: "var(--ee-brand)" }}>Claude</span>
            <span style={{ fontSize: 16, fontWeight: 700, color: "var(--ee-brand)" }}>Grok</span>
            <span style={{ fontSize: 16, fontWeight: 700, color: "var(--ee-brand)" }}>Python Risk Desk</span>
          </div>
        </div>

        {/* Ticker marquee */}
        <div
          style={{
            position: "relative",
            background: "var(--ee-bg-solid)",
            overflow: "hidden",
            padding: "13px 0",
          }}
        >
          <div style={{ display: "flex", gap: 28, width: "max-content" }} className="ee-marquee">
            {tickerLoop.map((t, i) => (
              <span
                key={i}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 10,
                  fontSize: 12.5,
                  whiteSpace: "nowrap",
                }}
              >
                <span style={{ color: "var(--ee-faint)", marginRight: 18 }}>|</span>
                <span style={{ color: "var(--ee-muted)" }}>{t.kind}</span>
                <span style={{ fontWeight: 700, color: "var(--ee-ink)" }}>{t.ticker}</span>
                <span style={{ color: "var(--ee-soft)" }}>{t.price}</span>
                <span style={{ color: t.chgColor }}>{t.chg}</span>
              </span>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------- Leaderboard ---------------- */}
      <section id="board" className="ee-section">
        {alerts.length > 0 && (
          <div style={{ marginBottom: 40, display: "flex", flexDirection: "column", gap: 10 }}>
            {alerts.map((a, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  gap: 14,
                  alignItems: "baseline",
                  flexWrap: "wrap",
                  borderLeft: `2px solid ${a.tone === "bad" ? "var(--ee-down)" : "var(--ee-amber)"}`,
                  paddingLeft: 14,
                }}
              >
                <span
                  style={{
                    fontSize: 10,
                    letterSpacing: "0.18em",
                    fontWeight: 700,
                    color: a.tone === "bad" ? "var(--ee-down)" : "var(--ee-amber)",
                    whiteSpace: "nowrap",
                  }}
                >
                  {a.label}
                </span>
                <span style={{ fontSize: 12, color: "var(--ee-bodytx)", lineHeight: 1.6 }}>{a.text}</span>
              </div>
            ))}
          </div>
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-end",
            flexWrap: "wrap",
            gap: 14,
          }}
        >
          <div>
            <div style={{ fontSize: 12, letterSpacing: "0.42em", color: "var(--ee-accent)", marginBottom: 14 }}>
              THE BIG BOARD
            </div>
            <h2 className="ee-serif" style={{ fontSize: "clamp(40px, 5vw, 64px)", lineHeight: 1 }}>
              Live <i>leaderboard</i>
            </h2>
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              fontSize: 11.5,
              letterSpacing: "0.16em",
              color: "var(--ee-muted)",
            }}
          >
            LAST UPDATE{" "}
            <b style={{ color: "var(--ee-ink)" }}>
              {formatEtStamp(d.generated_at) || d.as_of_et || "—"}
            </b>
          </div>
        </div>

        <div className="ee-split" style={{ marginTop: 48 }}>
          {/* Left: book */}
          <div className="ee-col-l">
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                flexWrap: "wrap",
                gap: 8,
              }}
            >
              <span className="ee-serif" style={{ fontSize: 26 }}>
                Open positions
              </span>
              <span style={{ fontSize: 10, letterSpacing: "0.2em", color: "var(--ee-muted)" }}>
                EQUITY · P&amp;L · CURVE
              </span>
            </div>

            <div
              style={{ display: "flex", alignItems: "baseline", gap: 14, marginTop: 20, flexWrap: "wrap" }}
            >
              <span style={{ fontSize: 42, fontWeight: 700, letterSpacing: "-0.02em" }}>{money(eqNow)}</span>
              <span style={{ fontSize: 19, fontWeight: 700, color: col(totalRet) }}>{pct(totalRet)}</span>
            </div>

            <div style={{ display: "flex", gap: 26, flexWrap: "wrap", marginTop: 16, fontSize: 12 }}>
              <span>
                <span style={{ color: "var(--ee-muted)" }}>CASH </span>
                <b>{money(d.portfolio?.cash_usd ?? 0)}</b>
              </span>
              <span>
                <span style={{ color: "var(--ee-muted)" }}>REALIZED </span>
                <b style={{ color: col(perf?.realized_pnl_usd ?? 0) }}>
                  {(perf?.realized_pnl_usd ?? 0) >= 0 ? "+" : "−"}
                  {money(Math.abs(perf?.realized_pnl_usd ?? 0))}
                </b>
              </span>
              <span>
                <span style={{ color: "var(--ee-muted)" }}>WIN </span>
                <b>{(perf?.win_rate_pct != null ? perf.win_rate_pct.toFixed(0) : "0") + "%"}</b>
              </span>
              <span>
                <span style={{ color: "var(--ee-muted)" }}>MAX DD </span>
                <b style={{ color: "var(--ee-down)" }}>
                  −{perf?.max_drawdown_pct != null ? perf.max_drawdown_pct.toFixed(2) : "0"}%
                </b>
              </span>
            </div>

            {/* Equity curve */}
            {ch && (
              <div style={{ marginTop: 22, borderTop: "1px solid var(--ee-hair08)", paddingTop: 18 }}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: 11,
                    letterSpacing: "0.14em",
                    color: "var(--ee-muted)",
                    marginBottom: 8,
                    gap: 10,
                    flexWrap: "wrap",
                  }}
                >
                  <span>EQUITY CURVE · % VS BENCHMARK</span>
                  <span>
                    AGENT <b style={{ color: ch.color }}>{ch.eqEnd}</b> · BENCH{" "}
                    <b style={{ color: "var(--ee-soft)" }}>{ch.benchEnd}</b>
                  </span>
                </div>
                <svg
                  viewBox="0 0 1000 260"
                  preserveAspectRatio="none"
                  style={{ width: "100%", height: 170, display: "block" }}
                >
                  <line
                    x1="8"
                    y1={ch.zeroY}
                    x2="992"
                    y2={ch.zeroY}
                    style={{ stroke: "var(--ee-hair14)" }}
                    strokeWidth="1"
                    strokeDasharray="3 4"
                  />
                  <path d={ch.eqArea} style={{ fill: ch.fill }} />
                  <path
                    d={ch.benchPath}
                    fill="none"
                    style={{ stroke: "var(--ee-faint)" }}
                    strokeWidth="2"
                    strokeDasharray="5 5"
                    strokeLinejoin="round"
                  />
                  <path
                    d={ch.eqPath}
                    fill="none"
                    style={{ stroke: ch.color }}
                    strokeWidth="3"
                    strokeLinejoin="round"
                    strokeLinecap="round"
                  />
                </svg>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    fontSize: 10.5,
                    color: "var(--ee-faint)",
                    marginTop: 6,
                  }}
                >
                  {ch.xLabels.map((l, i) => (
                    <span key={i}>{l}</span>
                  ))}
                </div>
              </div>
            )}

            {/* Positions table / cash state */}
            <div style={{ marginTop: 20, borderTop: "1px solid var(--ee-hair08)", paddingTop: 18 }}>
              {positions.length > 0 ? (
                <>
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr 1fr 1fr",
                      fontSize: 10,
                      letterSpacing: "0.14em",
                      color: "var(--ee-muted)",
                      paddingBottom: 10,
                    }}
                  >
                    <span>TICKER</span>
                    <span>ENTRY</span>
                    <span>SIZE</span>
                    <span>UNREALIZED</span>
                  </div>
                  {positions.map((p) => (
                    <div
                      key={p.ticker}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "1fr 1fr 1fr 1fr",
                        fontSize: 13.5,
                        padding: "10px 0",
                        borderTop: "1px solid var(--ee-hair06)",
                      }}
                    >
                      <b>{p.ticker}</b>
                      <span>{p.avg_cost ? money(p.avg_cost) : "—"}</span>
                      <span>
                        {p.notional_usd
                          ? money(p.notional_usd)
                          : p.market_value_usd
                            ? money(p.market_value_usd)
                            : "—"}
                      </span>
                      <span style={{ color: col(p.unrealized_pct ?? 0) }}>
                        {p.unrealized_pct != null ? pct(p.unrealized_pct) : "—"}
                      </span>
                    </div>
                  ))}
                </>
              ) : (
                <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
                  <span
                    className="ee-serif"
                    style={{ fontSize: 30, color: "var(--ee-accent)", fontStyle: "italic" }}
                  >
                    Cash.
                  </span>
                  <span style={{ fontSize: 12, color: "var(--ee-muted)", lineHeight: 1.6, flex: 1, minWidth: 220 }}>
                    100% cash — no open positions. {strip(d.no_trade_reason)}
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Right: watchlist */}
          <div className="ee-col-r">
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                flexWrap: "wrap",
                gap: 8,
                marginBottom: 14,
              }}
            >
              <span className="ee-serif" style={{ fontSize: 26 }}>
                Watchlist
              </span>
              <span style={{ fontSize: 10, letterSpacing: "0.2em", color: "var(--ee-muted)" }}>
                {watchlist.length} NAMES · CLICK TO EXPAND
              </span>
            </div>
            {watchlist.map((w) => (
              <div key={w.ticker} style={{ borderTop: "1px solid var(--ee-hair08)" }}>
                <button
                  className="ee-wrow"
                  onClick={() => setExpanded((s) => ({ ...s, [w.ticker]: !s[w.ticker] }))}
                  aria-expanded={w.open}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 18,
                    padding: "16px 2px",
                    cursor: "pointer",
                    width: "100%",
                    background: "none",
                    border: "none",
                    color: "inherit",
                    font: "inherit",
                    textAlign: "left",
                  }}
                >
                  <span style={{ fontSize: 15, fontWeight: 700 }}>{w.ticker}</span>
                  <span style={{ flex: 1 }} />
                  <span style={{ fontSize: 13, color: "var(--ee-soft)", whiteSpace: "nowrap" }}>{w.price}</span>
                  <span style={{ fontSize: 13, whiteSpace: "nowrap", color: w.chgColor }}>{w.chg}</span>
                  <span style={{ color: "var(--ee-muted)", fontSize: 12 }}>{w.open ? "▲" : "▼"}</span>
                </button>
                {w.open && (
                  <div style={{ padding: "2px 2px 20px 2px" }}>
                    <p
                      style={{
                        fontSize: 13,
                        color: "var(--ee-strong)",
                        lineHeight: 1.6,
                        fontWeight: 500,
                        textWrap: "pretty",
                      }}
                    >
                      {w.oneLine}
                    </p>
                    <p
                      style={{
                        fontSize: 12.5,
                        color: "var(--ee-bodytx)",
                        lineHeight: 1.65,
                        marginTop: 10,
                        textWrap: "pretty",
                      }}
                    >
                      {w.thoughts}
                    </p>
                    {w.buyAt && (
                      <>
                        <div
                          style={{
                            marginTop: 12,
                            fontSize: 10,
                            letterSpacing: "0.16em",
                            color: "var(--ee-muted)",
                          }}
                        >
                          WOULD BUY AT
                        </div>
                        <div
                          style={{
                            fontSize: 12.5,
                            color: "var(--ee-accent)",
                            lineHeight: 1.55,
                            marginTop: 4,
                          }}
                        >
                          {w.buyAt}
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------- The Floor ---------------- */}
      <section id="floor" className="ee-section">
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: 12, letterSpacing: "0.42em", color: "var(--ee-accent)", marginBottom: 14 }}>
            THOUGHT → TRADE
          </div>
          <h2 className="ee-serif" style={{ fontSize: "clamp(40px, 5vw, 64px)", lineHeight: 1 }}>
            The trading <i>floor</i>
          </h2>
        </div>

        <div className="ee-split ee-split-floor" style={{ marginTop: 56 }}>
          <div className="ee-col-l" style={{ display: "flex", flexDirection: "column", gap: 44 }}>
            {/* Execution feed */}
            <div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: 8,
                  gap: 8,
                  flexWrap: "wrap",
                }}
              >
                <span className="ee-serif" style={{ fontSize: 24 }}>
                  Execution feed
                </span>
                <span
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    fontSize: 10,
                    letterSpacing: "0.18em",
                    color: "var(--ee-muted)",
                  }}
                >
                  SIM ORDERS
                </span>
              </div>
              {feed.map((f, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 16,
                    padding: "13px 0",
                    borderTop: "1px solid var(--ee-hair07)",
                    fontSize: 13,
                  }}
                >
                  <span style={{ color: "var(--ee-faint)", width: 78, flexShrink: 0 }}>{f.date}</span>
                  <span style={{ fontWeight: 700, width: 44, color: f.actColor, flexShrink: 0 }}>{f.action}</span>
                  <b style={{ width: 52, flexShrink: 0 }}>{f.ticker}</b>
                  <span style={{ color: "var(--ee-soft)", flex: 1, textAlign: "right" }}>{f.price}</span>
                  <span style={{ fontSize: 11, color: "var(--ee-muted)" }}>{f.note}</span>
                </div>
              ))}
              <div
                style={{
                  borderTop: "1px solid var(--ee-hair07)",
                  paddingTop: 14,
                  marginTop: 2,
                  textAlign: "center",
                  fontSize: 10,
                  letterSpacing: "0.18em",
                  color: "var(--ee-faint)",
                }}
              >
                PAPER TRADING · SIMULATED FILLS, NO REAL SETTLEMENT
              </div>
            </div>

            {/* Calibration */}
            <div style={{ borderTop: "1px solid var(--ee-hair14)", paddingTop: 36 }}>
              <span className="ee-serif" style={{ fontSize: 24 }}>
                Confidence calibration
              </span>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr 1fr 1fr",
                  fontSize: 10,
                  letterSpacing: "0.12em",
                  color: "var(--ee-muted)",
                  marginTop: 16,
                  paddingBottom: 10,
                }}
              >
                <span>STATED CONF</span>
                <span>TRADES</span>
                <span>WIN RATE</span>
                <span>GAP</span>
              </div>
              {calib.map((c, i) => (
                <div
                  key={i}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr 1fr 1fr",
                    fontSize: 14,
                    padding: "11px 0",
                    borderTop: "1px solid var(--ee-hair07)",
                  }}
                >
                  <b>{c.bucket}</b>
                  <span>{c.trades}</span>
                  <span>{c.win}</span>
                  <b style={{ color: c.gapColor }}>{c.gap}</b>
                </div>
              ))}
              <p
                style={{
                  fontSize: 11.5,
                  color: "var(--ee-muted)",
                  lineHeight: 1.6,
                  marginTop: 14,
                  textWrap: "pretty",
                }}
              >
                Realized win rate vs. the confidence the agent stated at entry. A large negative gap means its
                confidence is inflated — the validator caps stated confidence until the gap closes.
              </p>
            </div>

            {/* Macro tape */}
            <div style={{ borderTop: "1px solid var(--ee-hair14)", paddingTop: 36 }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: 8,
                }}
              >
                <span className="ee-serif" style={{ fontSize: 24 }}>
                  Macro tape
                </span>
                <span
                  style={{
                    fontSize: 10,
                    letterSpacing: "0.14em",
                    color: "var(--ee-pill-ink)",
                    background: "var(--ee-pill-bg)",
                    padding: "3px 10px",
                    borderRadius: 12,
                    fontWeight: 700,
                  }}
                >
                  {regimeLabel} {regimeScore}/5
                </span>
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))",
                  gap: 12,
                  marginTop: 18,
                }}
              >
                {macro.map((m, i) => (
                  <div key={i}>
                    <div style={{ fontSize: 9.5, letterSpacing: "0.12em", color: "var(--ee-muted)" }}>
                      {m.label}
                    </div>
                    <div style={{ fontSize: 20, fontWeight: 700, marginTop: 6 }}>{m.value}</div>
                    <div style={{ fontSize: 10.5, marginTop: 3, color: m.dirColor }}>{m.direction}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Right: thinking */}
          <div className="ee-col-r">
            <span className="ee-serif" style={{ fontSize: 24 }}>
              What it&apos;s thinking
            </span>
            <div style={{ marginTop: 8 }}>
              {thoughts.map((th, i) => (
                <div key={i} style={{ padding: "20px 0", borderTop: "1px solid var(--ee-hair07)" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <span
                      style={{
                        fontSize: 11.5,
                        letterSpacing: "0.14em",
                        color: "var(--ee-accent)",
                        fontWeight: 700,
                      }}
                    >
                      {th.tag}
                    </span>
                    <span style={{ fontSize: 11, color: "var(--ee-faint)" }}>{th.time}</span>
                  </div>
                  <p
                    style={{
                      fontSize: 13,
                      color: "var(--ee-thought)",
                      lineHeight: 1.75,
                      marginTop: 12,
                      textWrap: "pretty",
                    }}
                  >
                    {th.text}
                  </p>
                </div>
              ))}
            </div>
            <div
              style={{
                borderTop: "1px solid var(--ee-hair07)",
                paddingTop: 14,
                fontSize: 10,
                letterSpacing: "0.16em",
                color: "var(--ee-faint)",
              }}
            >
              {(d.schedule_note || "").toUpperCase()}
            </div>
          </div>
        </div>
      </section>

      {/* ---------------- The Machine ---------------- */}
      <section id="machine" className="ee-section" style={{ paddingBottom: 20 }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: 12, letterSpacing: "0.42em", color: "var(--ee-accent)", marginBottom: 14 }}>
            UNDER THE HOOD
          </div>
          <h2 className="ee-serif" style={{ fontSize: "clamp(40px, 5vw, 64px)", lineHeight: 1 }}>
            The <i>machine</i>
          </h2>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
            gap: "0 60px",
            marginTop: 56,
          }}
        >
          {[
            {
              h: "Long-only, swing horizon",
              b: `US equities held 3–90 days. No shorting, no options, no leverage. ${
                d.universe_size ?? 182
              }-name AI supply-chain universe — semis, data-center infra, AI beneficiaries.`,
            },
            {
              h: "Adversarial risk desk",
              b: "Every BUY runs a kill checklist and needs a demand driver + thesis invalidator. Sector exposure hard-capped at 40%; rejected proposals logged with machine-readable reasons.",
            },
            {
              h: "Kill switch & buy caps",
              b: "A single file halts all new orders instantly. Calendar-day BUY caps, per-day new-position caps, and broker readback confirmation before any fill is journaled.",
            },
            {
              h: "Shadow portfolio",
              b: "Every skip and reject is tracked and marked at 30/60/90 days — regret-miss vs. good-skip — so the agent learns from the trades it didn't take.",
            },
            {
              h: "Exit autopsies & memory",
              b: "Deterministic process-win / fail graded at every close, plus durable per-ticker concept memory that compounds understanding across runs.",
            },
            {
              h: "Weekly adopt pipeline",
              b: "Improvement notes become soft standing lessons — pruned, capped, and superseded — and a calibration gate caps stated confidence where results lag.",
            },
          ].map((c) => (
            <div key={c.h} style={{ borderTop: "1px solid var(--ee-hair14)", padding: "24px 0 28px" }}>
              <div className="ee-serif" style={{ fontSize: 20, marginBottom: 8 }}>
                {c.h}
              </div>
              <div style={{ fontSize: 12.5, color: "var(--ee-bodytx)", lineHeight: 1.65 }}>{c.b}</div>
            </div>
          ))}
        </div>
      </section>

      {/* ---------------- The Log ---------------- */}
      <section id="log" className="ee-section" style={{ padding: "70px 32px 10px" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 20, flexWrap: "wrap", marginBottom: 26 }}>
          <h3 className="ee-serif" style={{ fontSize: 34 }}>
            Engineering <i>log</i>
          </h3>
          <span style={{ fontSize: 11, letterSpacing: "0.2em", color: "var(--ee-muted)" }}>
            THE AGENT CRITIQUES ITS OWN PIPELINE
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column" }}>
          {improvements.map((im, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                gap: 22,
                padding: "15px 2px",
                borderTop: "1px solid var(--ee-hair08)",
                flexWrap: "wrap",
              }}
            >
              <span style={{ fontSize: 12, color: "var(--ee-accent)", flexShrink: 0, width: 88 }}>{im.date}</span>
              <span
                style={{
                  fontSize: 12.5,
                  color: "var(--ee-bodytx)",
                  lineHeight: 1.65,
                  textWrap: "pretty",
                  flex: 1,
                  minWidth: 240,
                }}
              >
                {strip(im.note)}
              </span>
            </div>
          ))}
        </div>
      </section>

      {/* ---------------- Footer ---------------- */}
      <footer
        style={{
          maxWidth: 1440,
          margin: "60px auto 0",
          padding: "34px 32px 70px",
          borderTop: "1px solid var(--ee-hair10)",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: 24,
            flexWrap: "wrap",
            alignItems: "flex-end",
          }}
        >
          <div style={{ maxWidth: "64ch" }}>
            <div className="ee-serif" style={{ fontSize: 20, marginBottom: 10 }}>
              EastEquity&apos;s Trading Agent
            </div>
            <p style={{ fontSize: 11.5, color: "var(--ee-faint)", lineHeight: 1.7 }}>
              Paper trading only — all fills are simulated. Nothing here is investment advice, a solicitation,
              or a record of real money. This page rebuilds automatically from the agent&apos;s own run state
              every time it trades.
            </p>
          </div>
          <div style={{ display: "flex", gap: 18, fontSize: 13, flexWrap: "wrap" }}>
            <Link href="/ledger" style={{ color: "var(--ee-accent)" }}>
              The Ledger
            </Link>
            <Link href="/runs" style={{ color: "var(--ee-accent)" }}>
              Run archive
            </Link>
            <Link href="/learning" style={{ color: "var(--ee-accent)" }}>
              Study Hall
            </Link>
            <a href="https://x.com/EastEquity" target="_blank" rel="noreferrer" style={{ color: "var(--ee-accent)" }}>
              @EastEquity ↗
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}
