"use client";

import { useMemo, useRef, useState } from "react";

type Bar = { date: string; open: number; high: number; low: number; close: number };
type ChartData = {
  bars: Bar[];
  avg_cost?: number | null;
  last_price?: number | null;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
  opened_at?: string | null;
};

const W = 800;
const H = 220;
const PAD = { top: 12, right: 64, bottom: 22, left: 8 };

function fmtPrice(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtDate(iso: string) {
  return new Date(iso + "T00:00:00Z").toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export default function PositionChart({ ticker, data }: { ticker: string; data: ChartData | null }) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const bars = data?.bars ?? [];

  const geom = useMemo(() => {
    if (bars.length === 0) return null;
    const innerW = W - PAD.left - PAD.right;
    const innerH = H - PAD.top - PAD.bottom;
    // Y-domain spans the price action AND every plan line so stop/target are visible.
    const lineVals = [data?.stop, data?.target, data?.entry, data?.avg_cost].filter(
      (v): v is number => typeof v === "number"
    );
    const lo = Math.min(...bars.map((b) => b.low), ...lineVals);
    const hi = Math.max(...bars.map((b) => b.high), ...lineVals);
    const pad = (hi - lo) * 0.05 || 1;
    const min = lo - pad;
    const max = hi + pad;
    const range = max - min || 1;
    const step = innerW / bars.length;
    const cw = Math.max(1, Math.min(9, step * 0.62));
    const x = (i: number) => PAD.left + step * (i + 0.5);
    const y = (v: number) => PAD.top + innerH - ((v - min) / range) * innerH;
    const openedIdx = data?.opened_at ? bars.findIndex((b) => b.date >= data!.opened_at!) : -1;
    return { innerW, innerH, min, max, x, y, step, cw, openedIdx };
  }, [bars, data]);

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || !geom) return;
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.round((px - PAD.left) / geom.step - 0.5);
    setHover(i >= 0 && i < bars.length ? i : null);
  }

  if (!geom) {
    return (
      <div className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-[13px] text-ink-3">
        Price history unavailable for {ticker}.
      </div>
    );
  }

  const { y, x, cw, openedIdx } = geom;
  const lines: { v: number; label: string; color: string; dash?: string }[] = [];
  if (typeof data?.target === "number") lines.push({ v: data.target, label: `T ${fmtPrice(data.target)}`, color: "var(--chart-up)", dash: "4 3" });
  if (typeof data?.stop === "number") lines.push({ v: data.stop, label: `S ${fmtPrice(data.stop)}`, color: "var(--chart-down)", dash: "4 3" });
  const basis = data?.avg_cost ?? data?.entry;
  if (typeof basis === "number") lines.push({ v: basis, label: `cost ${fmtPrice(basis)}`, color: "var(--text-faint)" });

  return (
    <div className="relative">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto touch-none"
        role="img"
        aria-label={`${ticker} price with stop, target, and cost basis`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        {/* plan lines */}
        {lines.map((ln, i) => (
          <g key={i}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(ln.v)} y2={y(ln.v)} stroke={ln.color} strokeWidth="1" strokeDasharray={ln.dash} opacity="0.85" />
            <text x={W - PAD.right + 4} y={y(ln.v) + 3} fontSize="10" fill={ln.color} fontFamily="var(--font-mono)">
              {ln.label}
            </text>
          </g>
        ))}
        {/* opened-at hairline */}
        {openedIdx >= 0 && (
          <g>
            <line x1={x(openedIdx)} x2={x(openedIdx)} y1={PAD.top} y2={H - PAD.bottom} stroke="var(--text-strong)" strokeWidth="1" strokeDasharray="2 2" opacity="0.35" />
            <path d={`M${x(openedIdx) - 3},${H - PAD.bottom} L${x(openedIdx) + 3},${H - PAD.bottom} L${x(openedIdx)},${H - PAD.bottom - 5} Z`} fill="var(--text-strong)" opacity="0.6" />
          </g>
        )}
        {/* candles */}
        {bars.map((b, i) => {
          const up = b.close >= b.open;
          const color = up ? "var(--chart-up)" : "var(--chart-down)";
          const bodyTop = y(Math.max(b.open, b.close));
          const bodyBot = y(Math.min(b.open, b.close));
          return (
            <g key={b.date} opacity={hover === null || hover === i ? 1 : 0.55}>
              <line x1={x(i)} x2={x(i)} y1={y(b.high)} y2={y(b.low)} stroke={color} strokeWidth="1" />
              <rect x={x(i) - cw / 2} y={bodyTop} width={cw} height={Math.max(1, bodyBot - bodyTop)} fill={color} />
            </g>
          );
        })}
        {/* axis dates */}
        <text x={PAD.left} y={H - 6} fontSize="10" fill="var(--text-faint)" fontFamily="var(--font-mono)">
          {fmtDate(bars[0].date)}
        </text>
        <text x={W - PAD.right} y={H - 6} textAnchor="end" fontSize="10" fill="var(--text-faint)" fontFamily="var(--font-mono)">
          {fmtDate(bars[bars.length - 1].date)}
        </text>
      </svg>

      {hover !== null && bars[hover] && (
        <div
          className="pointer-events-none absolute -translate-x-1/2 rounded-md border border-line bg-card px-2.5 py-1 shadow-sm"
          style={{ left: `${(x(hover) / W) * 100}%`, top: 0 }}
        >
          <div className="text-[10px] text-ink-3 font-[family-name:var(--font-mono)]">{fmtDate(bars[hover].date)}</div>
          <div className="text-[11px] font-[family-name:var(--font-mono)]">
            O{fmtPrice(bars[hover].open)} H{fmtPrice(bars[hover].high)} L{fmtPrice(bars[hover].low)} C{fmtPrice(bars[hover].close)}
          </div>
        </div>
      )}
    </div>
  );
}
