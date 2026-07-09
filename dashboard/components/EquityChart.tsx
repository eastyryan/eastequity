"use client";

import { useMemo, useRef, useState } from "react";

type Point = { date: string; equity: number; benchmark_close?: number | null };

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

export default function EquityChart({ points, startingCapital }: { points: Point[]; startingCapital: number }) {
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

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = ((e.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - x) < Math.abs(xs[best] - x)) best = i;
    setHover(best);
  }

  const hasBench = benchVals.some((v) => v !== null);

  return (
    <div className="relative">
      {hasBench && view.length > 1 && (
        <div className="mb-3 flex items-center gap-5 text-[13px] text-ink-2">
          <span className="flex items-center gap-2">
            <span className="inline-block h-0.5 w-5 rounded bg-accent" aria-hidden />
            Agent
          </span>
          <span className="flex items-center gap-2">
            <span className="inline-block h-0.5 w-5 rounded bg-zinc-400" aria-hidden />
            S&amp;P 500 (SPY), same capital
          </span>
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
          style={{ left: `${(xs[hover] / W) * 100}%`, top: hasBench ? 28 : 0 }}
        >
          <div className="text-[11px] text-ink-3 font-[family-name:var(--font-geist-mono)]">{fmtDate(view[hover].date)}</div>
          <div className="text-sm font-medium font-[family-name:var(--font-geist-mono)]">{fmtUsd(view[hover].equity)}</div>
          {benchVals[hover] !== null && (
            <div className="text-[11px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
              SPY {fmtUsd(benchVals[hover]!)}
            </div>
          )}
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
