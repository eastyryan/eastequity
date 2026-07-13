"use client";

import { useMemo, useRef, useState } from "react";

type Point = { date: string; equity: number; benchmark_close?: number | null };
type TradeEvent = { date: string; ticker: string; action: "BUY" | "SELL_TO_CLOSE"; verdict?: string | null };

const W = 800;
const H = 260;
const PAD = { top: 16, right: 12, bottom: 28, left: 56 };
const VISIBLE_SESSIONS = 30; // keep the chart readable as history grows

function fmtUsd(n: number) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

function fmtDate(iso: string) {
  return new Date(iso + "T00:00:00Z").toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export default function EquityChart({
  points,
  startingCapital,
  events = [],
}: {
  points: Point[];
  startingCapital: number;
  events?: TradeEvent[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const view = useMemo(() => points.slice(-VISIBLE_SESSIONS), [points]);

  const { agentPath, benchPath, xs, ys, benchVals, ticks } = useMemo(() => {
    // Benchmark indexed to the same starting capital, from the first visible session.
    const baseBench = view.find((p) => p.benchmark_close)?.benchmark_close ?? null;
    const benchVals = view.map((p) =>
      baseBench && p.benchmark_close ? (p.benchmark_close / baseBench) * startingCapital : null
    );
    const all = [...view.map((p) => p.equity), ...benchVals.filter((v): v is number => v !== null)];
    const min = Math.min(...all, startingCapital * 0.98);
    const max = Math.max(...all, startingCapital * 1.02);
    const range = max - min || 1;
    const innerW = W - PAD.left - PAD.right;
    const innerH = H - PAD.top - PAD.bottom;
    const x = (i: number) =>
      view.length === 1 ? PAD.left + innerW / 2 : PAD.left + (i / (view.length - 1)) * innerW;
    const y = (v: number) => PAD.top + innerH - ((v - min) / range) * innerH;
    const xs = view.map((_, i) => x(i));
    const ys = view.map((p) => y(p.equity));
    const agentPath = xs.map((px, i) => `${i === 0 ? "M" : "L"}${px},${ys[i]}`).join(" ");
    const benchPath = benchVals
      .map((v, i) => (v === null ? "" : `${i === 0 || benchVals[i - 1] === null ? "M" : "L"}${xs[i]},${y(v)}`))
      .join(" ");
    const tickVals = [min, (min + max) / 2, max];
    const ticks = tickVals.map((v) => ({ v, y: y(v) }));
    return { agentPath, benchPath, xs, ys, benchVals, ticks };
  }, [view, startingCapital]);

  // Map each trade event to the nearest visible session by date, dropping events that fall
  // outside the visible window. Grouped by session index so same-day events can be stacked.
  const eventsByIndex = useMemo(() => {
    const map: Record<number, TradeEvent[]> = {};
    if (view.length === 0 || events.length === 0) return map;
    const times = view.map((p) => Date.parse(p.date + "T00:00:00Z"));
    const min = times[0];
    const max = times[times.length - 1];
    for (const ev of events) {
      const t = Date.parse(ev.date + "T00:00:00Z");
      if (Number.isNaN(t) || t < min || t > max) continue;
      let best = 0;
      for (let i = 1; i < times.length; i++) if (Math.abs(times[i] - t) < Math.abs(times[best] - t)) best = i;
      (map[best] ??= []).push(ev);
    }
    return map;
  }, [events, view]);

  // Concrete marker geometry: a green ▲ just below the line for BUY, a red ▼ just above it for
  // SELL_TO_CLOSE. Same-day events on the same side stack further from the line.
  const markers = useMemo(() => {
    const out: { cx: number; cy: number; dir: "up" | "down"; color: string; idx: number }[] = [];
    for (const key of Object.keys(eventsByIndex)) {
      const idx = Number(key);
      let buyN = 0;
      let sellN = 0;
      for (const ev of eventsByIndex[idx]) {
        if (ev.action === "BUY") {
          out.push({ cx: xs[idx], cy: ys[idx] + 7 + buyN * 9, dir: "up", color: "#047857", idx });
          buyN++;
        } else {
          out.push({ cx: xs[idx], cy: ys[idx] - 7 - sellN * 9, dir: "down", color: "#b91c1c", idx });
          sellN++;
        }
      }
    }
    return out;
  }, [eventsByIndex, xs, ys]);

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = ((e.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - x) < Math.abs(xs[best] - x)) best = i;
    setHover(best);
  }

  const hasBench = benchVals.some((v) => v !== null);
  const showLegend = (hasBench && view.length > 1) || markers.length > 0;

  return (
    <div className="relative">
      {showLegend && (
        <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[13px] text-ink-2">
          {hasBench && view.length > 1 && (
            <>
              <span className="flex items-center gap-2">
                <span className="inline-block h-0.5 w-5 rounded bg-accent" aria-hidden />
                Agent
              </span>
              <span className="flex items-center gap-2">
                <span className="inline-block h-0.5 w-5 rounded bg-zinc-400" aria-hidden />
                S&amp;P 500 (SPY), same capital
              </span>
            </>
          )}
          {markers.length > 0 && (
            <>
              <span className="flex items-center gap-1.5">
                <span className="text-[10px] leading-none text-accent" aria-hidden>
                  ▲
                </span>
                Buy
              </span>
              <span className="flex items-center gap-1.5">
                <span className="text-[10px] leading-none text-red-700" aria-hidden>
                  ▼
                </span>
                Sell
              </span>
            </>
          )}
        </div>
      )}
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto touch-none"
        role="img"
        aria-label="Portfolio equity versus S&P 500 over time"
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={PAD.left} x2={W - PAD.right} y1={t.y} y2={t.y} stroke="#e4e4e7" strokeWidth="1" />
            <text x={PAD.left - 8} y={t.y + 4} textAnchor="end" fontSize="11" fill="#a1a1aa" fontFamily="var(--font-geist-mono)">
              {fmtUsd(t.v)}
            </text>
          </g>
        ))}
        {view.length > 1 && benchPath && (
          <path d={benchPath} fill="none" stroke="#a1a1aa" strokeWidth="1.5" strokeLinejoin="round" />
        )}
        {view.length > 1 && (
          <path d={agentPath} fill="none" stroke="#047857" strokeWidth="1.5" strokeLinejoin="round" />
        )}
        {view.map((p, i) => (
          <circle
            key={p.date}
            cx={xs[i]}
            cy={ys[i]}
            r={hover === i ? 4.5 : view.length <= 15 ? 3 : 0}
            fill="#047857"
            stroke="#fafafa"
            strokeWidth="2"
          />
        ))}
        {markers.map((m, i) => {
          const s = 4;
          const pts =
            m.dir === "up"
              ? `${m.cx},${m.cy - s} ${m.cx - s},${m.cy + s} ${m.cx + s},${m.cy + s}`
              : `${m.cx},${m.cy + s} ${m.cx - s},${m.cy - s} ${m.cx + s},${m.cy - s}`;
          return (
            <polygon
              key={i}
              points={pts}
              fill={m.color}
              opacity={hover === null || hover === m.idx ? 1 : 0.5}
              onPointerEnter={() => setHover(m.idx)}
            />
          );
        })}
        {hover !== null && (
          <line x1={xs[hover]} x2={xs[hover]} y1={PAD.top} y2={H - PAD.bottom} stroke="#a1a1aa" strokeWidth="1" strokeDasharray="3 3" />
        )}
        <text x={PAD.left} y={H - 8} fontSize="11" fill="#a1a1aa" fontFamily="var(--font-geist-mono)">
          {fmtDate(view[0].date)}
        </text>
        <text x={W - PAD.right} y={H - 8} textAnchor="end" fontSize="11" fill="#a1a1aa" fontFamily="var(--font-geist-mono)">
          {fmtDate(view[view.length - 1].date)}
        </text>
      </svg>

      {hover !== null && (
        <div
          className="pointer-events-none absolute -translate-x-1/2 rounded-md border border-line bg-white px-3 py-1.5 shadow-sm"
          style={{ left: `${(xs[hover] / W) * 100}%`, top: showLegend ? 28 : 0 }}
        >
          <div className="text-[11px] text-ink-3 font-[family-name:var(--font-geist-mono)]">{fmtDate(view[hover].date)}</div>
          <div className="text-sm font-medium font-[family-name:var(--font-geist-mono)]">{fmtUsd(view[hover].equity)}</div>
          {benchVals[hover] !== null && (
            <div className="text-[11px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
              SPY {fmtUsd(benchVals[hover]!)}
            </div>
          )}
          {(eventsByIndex[hover] ?? []).map((ev, i) => (
            <div
              key={i}
              className={`text-[11px] font-[family-name:var(--font-geist-mono)] ${
                ev.action === "BUY" ? "text-accent" : "text-red-700"
              }`}
            >
              {ev.action === "BUY" ? "BUY" : "SELL"} {ev.ticker}
              {ev.action === "SELL_TO_CLOSE" && ev.verdict ? ` · ${ev.verdict}` : ""}
            </div>
          ))}
        </div>
      )}

      {view.length < 2 && (
        <p className="mt-3 text-sm text-ink-2">
          Track record begins {fmtDate(view[0].date)}. The curve fills in with each session, charted
          against holding the S&amp;P 500 with the same capital.
        </p>
      )}
      {points.length > VISIBLE_SESSIONS && (
        <p className="mt-2 text-[13px] text-ink-3">Showing the latest {VISIBLE_SESSIONS} sessions.</p>
      )}
    </div>
  );
}
