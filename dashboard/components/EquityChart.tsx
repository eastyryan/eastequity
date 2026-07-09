"use client";

import { useMemo, useRef, useState } from "react";

type Point = { date: string; equity: number };

const W = 800;
const H = 260;
const PAD = { top: 16, right: 12, bottom: 28, left: 56 };

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

export default function EquityChart({ points }: { points: Point[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const { path, area, xs, ys, ticks } = useMemo(() => {
    const vals = points.map((p) => p.equity);
    const min = Math.min(...vals, 9800);
    const max = Math.max(...vals, 10200);
    const range = max - min || 1;
    const innerW = W - PAD.left - PAD.right;
    const innerH = H - PAD.top - PAD.bottom;
    const xs = points.map((_, i) =>
      points.length === 1 ? PAD.left + innerW / 2 : PAD.left + (i / (points.length - 1)) * innerW
    );
    const ys = points.map((p) => PAD.top + innerH - ((p.equity - min) / range) * innerH);
    const path = xs.map((x, i) => `${i === 0 ? "M" : "L"}${x},${ys[i]}`).join(" ");
    const area =
      points.length > 1
        ? `${path} L${xs[xs.length - 1]},${H - PAD.bottom} L${xs[0]},${H - PAD.bottom} Z`
        : "";
    const tickVals = [min, (min + max) / 2, max];
    const ticks = tickVals.map((v) => ({
      v,
      y: PAD.top + innerH - ((v - min) / range) * innerH,
    }));
    return { path, area, xs, ys, ticks };
  }, [points]);

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = ((e.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - x) < Math.abs(xs[best] - x)) best = i;
    setHover(best);
  }

  return (
    <div className="relative">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto touch-none"
        role="img"
        aria-label="Portfolio equity over time"
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
        {area && <path d={area} fill="#047857" opacity="0.06" />}
        {points.length > 1 && <path d={path} fill="none" stroke="#047857" strokeWidth="2" strokeLinejoin="round" />}
        {points.map((p, i) => (
          <circle
            key={p.date}
            cx={xs[i]}
            cy={ys[i]}
            r={hover === i ? 5 : points.length <= 20 ? 3.5 : 0}
            fill="#047857"
            stroke="#fafafa"
            strokeWidth="2"
          />
        ))}
        {hover !== null && (
          <line x1={xs[hover]} x2={xs[hover]} y1={PAD.top} y2={H - PAD.bottom} stroke="#a1a1aa" strokeWidth="1" strokeDasharray="3 3" />
        )}
        <text x={PAD.left} y={H - 8} fontSize="11" fill="#a1a1aa" fontFamily="var(--font-geist-mono)">
          {fmtDate(points[0].date)}
        </text>
        <text x={W - PAD.right} y={H - 8} textAnchor="end" fontSize="11" fill="#a1a1aa" fontFamily="var(--font-geist-mono)">
          {fmtDate(points[points.length - 1].date)}
        </text>
      </svg>

      {hover !== null && (
        <div
          className="pointer-events-none absolute -translate-x-1/2 rounded-md border border-line bg-white px-3 py-1.5 shadow-sm"
          style={{ left: `${(xs[hover] / W) * 100}%`, top: 0 }}
        >
          <div className="text-[11px] text-ink-3 font-[family-name:var(--font-geist-mono)]">{fmtDate(points[hover].date)}</div>
          <div className="text-sm font-medium font-[family-name:var(--font-geist-mono)]">{fmtUsd(points[hover].equity)}</div>
        </div>
      )}

      {points.length < 2 && (
        <p className="mt-3 text-sm text-ink-2">
          Track record begins {fmtDate(points[0].date)}. The curve fills in with each trading day.
        </p>
      )}
    </div>
  );
}
