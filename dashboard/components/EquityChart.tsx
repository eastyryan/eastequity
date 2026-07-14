"use client";

import { useMemo, useRef, useState } from "react";

type Point = { date: string; equity: number; cash?: number; benchmark_close?: number | null };
type TradeEvent = { date: string; ticker: string; action: "BUY" | "SELL_TO_CLOSE"; verdict?: string | null };
type RangeKey = "1W" | "1M" | "3M" | "MAX";

const W = 800;
const H = 300;
const PAD = { top: 28, right: 30, bottom: 22, left: 14 };

// Design-system palette: green/red are P/L-only, benchmark is neutral, grid is
// faint dashed. CSS variables resolve from app/design-tokens.css.
const PAPER = "var(--surface-card)";
const INK = "var(--text-strong)";
const SUB = "var(--text-muted)";
const GREEN = "var(--chart-up)"; // portfolio line + positive values
const RED = "var(--chart-down)";
const GRAY = "var(--chart-bench)"; // S&P line + its endpoint
const BASELINE = "var(--chart-grid)";
const CHIP_BORDER = "var(--border-subtle)";
const MONO = "var(--font-mono)";

const RANGES: { key: RangeKey; days: number | null }[] = [
  { key: "1W", days: 7 },
  { key: "1M", days: 30 },
  { key: "3M", days: 90 },
  { key: "MAX", days: null },
];

const DAY_MS = 86_400_000;

function ts(iso: string) {
  return Date.parse(iso + "T00:00:00Z");
}

function fmtDate(iso: string) {
  return new Date(iso + "T00:00:00Z").toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

// Arrow / text / color for a signed percent. The card never flatters a drawdown:
// color always follows the sign.
function signed(pct: number) {
  return pct >= 0
    ? { arrow: "▲", text: `+${pct.toFixed(2)}%`, color: GREEN }
    : { arrow: "▼", text: `${pct.toFixed(2)}%`, color: RED };
}

export default function EquityChart({
  points,
  events = [],
}: {
  points: Point[];
  startingCapital?: number; // kept for backward compatibility; baseline is the window's first equity
  events?: TradeEvent[];
}) {
  const [range, setRange] = useState<RangeKey>("MAX");
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  // How many sessions each trailing window (measured back from the last recorded
  // date) would contain. Windows with <2 points can't draw a line, so their pills
  // go inert and selection falls back to MAX.
  const counts = useMemo(() => {
    const out = { "1W": 0, "1M": 0, "3M": 0, MAX: points.length } as Record<RangeKey, number>;
    if (points.length === 0) return out;
    const last = ts(points[points.length - 1].date);
    for (const r of RANGES) {
      if (r.days === null) continue;
      out[r.key] = points.filter((p) => ts(p.date) >= last - r.days! * DAY_MS).length;
    }
    return out;
  }, [points]);

  const effective: RangeKey = counts[range] >= 2 ? range : "MAX";

  const view = useMemo(() => {
    const days = RANGES.find((r) => r.key === effective)?.days ?? null;
    if (days === null || points.length === 0) return points;
    const last = ts(points[points.length - 1].date);
    return points.filter((p) => ts(p.date) >= last - days * DAY_MS);
  }, [points, effective]);

  const geo = useMemo(() => {
    const n = view.length;
    // Agent % baseline = first equity of the selected window.
    const baseEq = n > 0 && view[0].equity ? view[0].equity : null;
    const agentPct = view.map((p) => (baseEq ? (p.equity / baseEq) * 100 - 100 : 0));
    // Benchmark normalizes from the first non-null close in the window; null sessions are skipped.
    const benchBase = view.find((p) => p.benchmark_close)?.benchmark_close ?? null;
    const benchPct: (number | null)[] = view.map((p) =>
      benchBase && p.benchmark_close ? (p.benchmark_close / benchBase) * 100 - 100 : null
    );
    const benchIdx = benchPct.map((v, i) => (v === null ? -1 : i)).filter((i) => i >= 0);

    const vals = [...agentPct, ...benchPct.filter((v): v is number => v !== null), 0];
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const span = Math.max(hi - lo, 0.8); // floor keeps flat/single-point windows from collapsing
    const yMin = lo - span * 0.45;
    const yMax = hi + span * 0.45;
    const innerW = W - PAD.left - PAD.right;
    const innerH = H - PAD.top - PAD.bottom;
    const x = (i: number) => (n <= 1 ? PAD.left + innerW / 2 : PAD.left + (i / (n - 1)) * innerW);
    const y = (v: number) => PAD.top + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

    const xs = view.map((_, i) => x(i));
    const ysAgent = agentPct.map(y);
    const agentPath =
      n > 1 ? xs.map((px, i) => `${i === 0 ? "M" : "L"}${px.toFixed(2)},${ysAgent[i].toFixed(2)}`).join(" ") : "";
    const benchPath =
      benchIdx.length > 1
        ? benchIdx
            .map((bi, k) => `${k === 0 ? "M" : "L"}${xs[bi].toFixed(2)},${y(benchPct[bi]!).toFixed(2)}`)
            .join(" ")
        : "";
    const lastBench = benchIdx.length > 0 ? benchIdx[benchIdx.length - 1] : null;
    return { agentPct, benchPct, xs, ysAgent, agentPath, benchPath, y0: y(0), y, lastBench };
  }, [view]);

  // Map each trade event to the nearest session in the window, dropping events
  // outside it. Grouped by index so same-day events stack.
  const eventsByIndex = useMemo(() => {
    const map: Record<number, TradeEvent[]> = {};
    if (view.length === 0 || events.length === 0) return map;
    const times = view.map((p) => ts(p.date));
    const min = times[0];
    const max = times[times.length - 1];
    for (const ev of events) {
      const t = ts(ev.date);
      if (Number.isNaN(t) || t < min || t > max) continue;
      let best = 0;
      for (let i = 1; i < times.length; i++) if (Math.abs(times[i] - t) < Math.abs(times[best] - t)) best = i;
      (map[best] ??= []).push(ev);
    }
    return map;
  }, [events, view]);

  // Trade markers: a small green triangle under the line for BUY, red above for
  // SELL_TO_CLOSE; same-day repeats stack further from the line.
  const markers = useMemo(() => {
    const out: { cx: number; cy: number; dir: "up" | "down"; color: string; idx: number }[] = [];
    for (const key of Object.keys(eventsByIndex)) {
      const idx = Number(key);
      let buyN = 0;
      let sellN = 0;
      for (const ev of eventsByIndex[idx]) {
        if (ev.action === "BUY") {
          out.push({ cx: geo.xs[idx], cy: geo.ysAgent[idx] + 10 + buyN * 9, dir: "up", color: GREEN, idx });
          buyN++;
        } else {
          out.push({ cx: geo.xs[idx], cy: geo.ysAgent[idx] - 10 - sellN * 9, dir: "down", color: RED, idx });
          sellN++;
        }
      }
    }
    return out;
  }, [eventsByIndex, geo]);

  if (view.length === 0) return null;

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = ((e.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < geo.xs.length; i++) if (Math.abs(geo.xs[i] - x) < Math.abs(geo.xs[best] - x)) best = i;
    setHover(best);
  }

  const last = view.length - 1;
  const agentHdr = signed(geo.agentPct[last]);
  const benchHdr = geo.lastBench !== null ? signed(geo.benchPct[geo.lastBench]!) : null;
  const dateRange = `${fmtDate(view[0].date)} - ${fmtDate(view[last].date)}`;

  // Tooltip chip: follows the hovered session, pinned to the last point when idle.
  // Near the right edge it flips to the left of its anchor so it never covers the
  // endpoint markers.
  const active = hover ?? last;
  const flip = geo.xs[active] / W > 0.6;
  const activeAgent = signed(geo.agentPct[active]);
  const activeBench = geo.benchPct[active] !== null ? signed(geo.benchPct[active]!) : null;
  const sparse = view.length <= 15;

  return (
    <div className="ds-card px-4 pt-5 pb-4 sm:px-7 sm:pt-6 sm:pb-5">
      {/* Header: portfolio column left, benchmark column right */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/avatar.jpg"
              alt=""
              className="h-9 w-9 rounded-full border"
              style={{ borderColor: CHIP_BORDER }}
            />
            <span className="text-[14px] sm:text-[15px] font-bold tracking-tight" style={{ color: INK }}>
              EastEquity&apos;s Agent Portfolio
            </span>
          </div>
          <div
            className="mt-3 text-2xl sm:text-[30px] font-semibold leading-none tracking-tight"
            style={{ color: agentHdr.color, fontFamily: MONO, fontFeatureSettings: '"tnum" 1' }}
          >
            {agentHdr.arrow} {agentHdr.text}
          </div>
          <div className="mt-2 text-[12px]" style={{ color: SUB, fontFamily: MONO }}>
            {dateRange}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[14px] sm:text-[15px] font-semibold tracking-tight leading-9" style={{ color: INK }}>
            S&amp;P 500
          </div>
          <div
            className="mt-3 text-2xl sm:text-[30px] font-semibold leading-none tracking-tight"
            style={{
              color: benchHdr ? benchHdr.color : SUB,
              fontFamily: MONO,
              fontFeatureSettings: '"tnum" 1',
            }}
          >
            {benchHdr ? `${benchHdr.arrow} ${benchHdr.text}` : "n/a"}
          </div>
          <div className="mt-2 text-[12px]" style={{ color: SUB, fontFamily: MONO }}>
            {dateRange}
          </div>
        </div>
      </div>

      {/* Chart */}
      <div className="relative mt-4">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="h-auto w-full touch-none"
          role="img"
          aria-label="Portfolio percent return versus the S&P 500 over the selected timeframe"
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        >
          {/* Design-system chart chrome: area gradient + the signature soft
              neon glow under the portfolio line. */}
          <defs>
            <linearGradient id="eq-area" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="var(--chart-area-top)" />
              <stop offset="1" stopColor="var(--chart-area-bot)" />
            </linearGradient>
            <filter id="eq-glow" x="-10%" y="-40%" width="120%" height="180%">
              <feDropShadow dx="0" dy="0" stdDeviation="4" floodColor={GREEN} floodOpacity="0.5" />
            </filter>
          </defs>

          {/* Faint dashed horizontal grid (no vertical grid, no chartjunk) */}
          {[0, 1, 2, 3].map((i) => (
            <line
              key={`g${i}`}
              x1={PAD.left}
              x2={W - PAD.right}
              y1={PAD.top + ((H - PAD.top - PAD.bottom) / 3) * i}
              y2={PAD.top + ((H - PAD.top - PAD.bottom) / 3) * i}
              stroke={BASELINE}
              strokeWidth="1"
              strokeDasharray="3 5"
            />
          ))}

          {/* 0% baseline + vertical guide at the last session */}
          <line
            x1={PAD.left}
            x2={W - PAD.right}
            y1={geo.y0}
            y2={geo.y0}
            stroke={BASELINE}
            strokeWidth="1.2"
            strokeDasharray="4 4"
          />
          <line
            x1={geo.xs[last]}
            x2={geo.xs[last]}
            y1={PAD.top}
            y2={H - PAD.bottom}
            stroke={BASELINE}
            strokeWidth="1.2"
          />
          {hover !== null && hover !== last && (
            <line
              x1={geo.xs[hover]}
              x2={geo.xs[hover]}
              y1={PAD.top}
              y2={H - PAD.bottom}
              stroke={BASELINE}
              strokeWidth="1"
              strokeDasharray="3 3"
            />
          )}

          {/* S&P 500 benchmark: thin, neutral, dashed — beneath the agent line */}
          {geo.benchPath && (
            <path
              d={geo.benchPath}
              fill="none"
              stroke={GRAY}
              strokeWidth="1.5"
              strokeDasharray="4 4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          )}
          {sparse &&
            view.map((p, i) =>
              geo.benchPct[i] !== null && i !== geo.lastBench ? (
                <circle
                  key={`b${p.date}`}
                  cx={geo.xs[i]}
                  cy={geo.y(geo.benchPct[i]!)}
                  r={hover === i ? 4 : 2.5}
                  fill={GRAY}
                  stroke={PAPER}
                  strokeWidth="1.4"
                />
              ) : null
            )}
          {geo.lastBench !== null && (
            <g>
              <circle
                cx={geo.xs[geo.lastBench]}
                cy={geo.y(geo.benchPct[geo.lastBench]!)}
                r="10"
                fill={GRAY}
                opacity="0.22"
              />
              <circle
                cx={geo.xs[geo.lastBench]}
                cy={geo.y(geo.benchPct[geo.lastBench]!)}
                r="4.4"
                fill={GRAY}
                stroke={PAPER}
                strokeWidth="1.6"
              />
            </g>
          )}

          {/* Area fill under the agent line (vertical tint → transparent) */}
          {geo.agentPath && view.length > 1 && (
            <path
              d={`${geo.agentPath} L${geo.xs[last]},${H - PAD.bottom} L${geo.xs[0]},${H - PAD.bottom} Z`}
              fill="url(#eq-area)"
            />
          )}

          {/* Agent line on top, with the signature glow */}
          {geo.agentPath && (
            <path
              d={geo.agentPath}
              fill="none"
              stroke={GREEN}
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              filter="url(#eq-glow)"
            />
          )}
          {sparse &&
            view.map((p, i) =>
              i !== last ? (
                <circle
                  key={`a${p.date}`}
                  cx={geo.xs[i]}
                  cy={geo.ysAgent[i]}
                  r={hover === i ? 4.5 : 3}
                  fill={GREEN}
                  stroke={PAPER}
                  strokeWidth="1.5"
                />
              ) : null
            )}
          <circle cx={geo.xs[last]} cy={geo.ysAgent[last]} r="10.5" fill={GREEN} opacity="0.22" />
          <circle cx={geo.xs[last]} cy={geo.ysAgent[last]} r="4.6" fill={GREEN} stroke={PAPER} strokeWidth="1.6" />

          {/* Trade markers */}
          {markers.map((m, i) => {
            const s = 3.5;
            const pts =
              m.dir === "up"
                ? `${m.cx},${m.cy - s} ${m.cx - s},${m.cy + s} ${m.cx + s},${m.cy + s}`
                : `${m.cx},${m.cy + s} ${m.cx - s},${m.cy - s} ${m.cx + s},${m.cy - s}`;
            return (
              <polygon
                key={i}
                points={pts}
                fill={m.color}
                stroke={PAPER}
                strokeWidth="1"
                opacity={hover === null || hover === m.idx ? 0.9 : 0.45}
                onPointerEnter={() => setHover(m.idx)}
              />
            );
          })}
        </svg>

        {/* Tooltip chip */}
        <div
          className="pointer-events-none absolute z-10 min-w-[186px] rounded-xl border bg-card px-3.5 py-2.5 shadow-[0_2px_10px_rgba(20,20,19,0.08)]"
          style={{
            left: `${(geo.xs[active] / W) * 100}%`,
            top: 6,
            borderColor: CHIP_BORDER,
            transform: flip ? "translateX(calc(-100% - 14px))" : "translateX(14px)",
          }}
        >
          <div className="text-[12px] font-medium" style={{ color: INK, fontFamily: MONO }}>
            {fmtDate(view[active].date)}
          </div>
          <div className="my-1.5 h-px" style={{ backgroundColor: CHIP_BORDER }} />
          <div className="flex items-center gap-2 text-[12.5px]">
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: GREEN }} aria-hidden />
            <span style={{ color: INK }}>EastEquity Agent</span>
            <span
              className="ml-auto pl-3 font-semibold"
              style={{ color: activeAgent.color, fontFamily: MONO, fontFeatureSettings: '"tnum" 1' }}
            >
              {activeAgent.text}
            </span>
          </div>
          <div className="mt-1 flex items-center gap-2 text-[12.5px]">
            <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: GRAY }} aria-hidden />
            <span style={{ color: INK }}>S&amp;P 500</span>
            <span
              className="ml-auto pl-3 font-semibold"
              style={{
                color: activeBench ? activeBench.color : SUB,
                fontFamily: MONO,
                fontFeatureSettings: '"tnum" 1',
              }}
            >
              {activeBench ? activeBench.text : "—"}
            </span>
          </div>
          {(eventsByIndex[active] ?? []).map((ev, i) => (
            <div
              key={i}
              className="mt-1 flex items-center gap-2 text-[11.5px] font-medium"
              style={{ color: ev.action === "BUY" ? GREEN : RED }}
            >
              <span className="w-2 text-center text-[9px] leading-none" aria-hidden>
                {ev.action === "BUY" ? "▲" : "▼"}
              </span>
              {ev.action === "BUY" ? "Buy" : "Sell"} {ev.ticker}
              {ev.action === "SELL_TO_CLOSE" && ev.verdict ? ` · ${ev.verdict}` : ""}
            </div>
          ))}
        </div>
      </div>

      {view.length < 2 && (
        <p className="mt-3 text-center text-[12px]" style={{ color: SUB }}>
          Track record begins {fmtDate(view[0].date)}. The curve fills in with each session.
        </p>
      )}

      {/* Timeframe control (design-system Range: sunken track, raised selection) */}
      <div className="mt-4 flex items-center justify-center">
        <div className="inline-flex gap-0.5 rounded-lg border border-line-2 bg-sunken p-0.5">
          {RANGES.map((r) => {
            const enabled = r.key === "MAX" || counts[r.key] >= 2;
            const selected = effective === r.key;
            return (
              <button
                key={r.key}
                type="button"
                disabled={!enabled}
                onClick={() => {
                  setRange(r.key);
                  setHover(null);
                }}
                className={`num rounded-md px-3 py-1 text-[12px] font-medium transition-colors ${
                  selected
                    ? "bg-card text-ink shadow-[var(--shadow-xs)]"
                    : enabled
                      ? "text-ink-3 hover:text-ink-2"
                      : "cursor-default text-faint/60"
                }`}
              >
                {r.key}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
