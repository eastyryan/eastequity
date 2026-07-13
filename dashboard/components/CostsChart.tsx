type Trade = {
  closed_at: string;
  fees_usd?: { commission?: number; sec_fee?: number; taf?: number } | null;
  dividends_usd?: number;
};

const W = 800;
const H = 180;
const PAD = { top: 14, right: 12, bottom: 26, left: 58 };

function usd2(n: number) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

// Cumulative figures are small dollars-and-cents; keep cents on the axis until totals get large.
function fmtTick(n: number, big: boolean) {
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: big ? 0 : 2,
  });
}

function fmtDate(iso: string) {
  const d = new Date(iso.length <= 10 ? iso + "T00:00:00Z" : iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

export default function CostsChart({ closedTrades }: { closedTrades: Trade[] }) {
  if (!closedTrades || closedTrades.length === 0) {
    return <p className="mt-3 text-sm text-ink-2">Costs will appear once trades close.</p>;
  }

  // Order by close date, then accumulate fees paid and dividends received trade by trade.
  const sorted = [...closedTrades].sort((a, b) =>
    a.closed_at < b.closed_at ? -1 : a.closed_at > b.closed_at ? 1 : 0
  );
  let cumF = 0;
  let cumD = 0;
  const series = sorted.map((t) => {
    const f = t.fees_usd;
    const fee = (f?.commission ?? 0) + (f?.sec_fee ?? 0) + (f?.taf ?? 0);
    cumF += fee;
    cumD += t.dividends_usd ?? 0;
    return { date: t.closed_at, cumF, cumD };
  });

  const totalFees = cumF;
  const totalDiv = cumD;
  const n = series.length;

  const maxV = Math.max(totalFees, totalDiv);
  const range = maxV || 1;
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const x = (i: number) => (n === 1 ? PAD.left + innerW / 2 : PAD.left + (i / (n - 1)) * innerW);
  const y = (v: number) => PAD.top + innerH - (v / range) * innerH;
  const baseY = y(0);

  const feeLine = series.map((s, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(s.cumF)}`).join(" ");
  const divLine = series.map((s, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(s.cumD)}`).join(" ");
  const feeArea = n > 1 ? `${feeLine} L${x(n - 1)},${baseY} L${x(0)},${baseY} Z` : "";
  const divArea = n > 1 ? `${divLine} L${x(n - 1)},${baseY} L${x(0)},${baseY} Z` : "";

  const big = maxV >= 100;
  const ticks = (maxV > 0 ? [0, maxV / 2, maxV] : [0]).map((v) => ({ v, y: y(v) }));

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <div className="flex items-center gap-5 text-[13px] text-ink-2">
          <span className="flex items-center gap-2">
            <span className="inline-block h-0.5 w-5 rounded bg-red-700" aria-hidden />
            Fees
          </span>
          <span className="flex items-center gap-2">
            <span className="inline-block h-0.5 w-5 rounded bg-accent" aria-hidden />
            Dividends
          </span>
        </div>
        <div className="flex items-center gap-5 text-sm font-[family-name:var(--font-geist-mono)]">
          <span className="text-red-700">-{usd2(totalFees)} fees</span>
          <span className="text-accent">+{usd2(totalDiv)} div</span>
        </div>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto"
        role="img"
        aria-label="Cumulative trading fees versus dividends received over time"
      >
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={PAD.left} x2={W - PAD.right} y1={t.y} y2={t.y} stroke="#e4e4e7" strokeWidth="1" />
            <text
              x={PAD.left - 8}
              y={t.y + 4}
              textAnchor="end"
              fontSize="11"
              fill="#a1a1aa"
              fontFamily="var(--font-geist-mono)"
            >
              {fmtTick(t.v, big)}
            </text>
          </g>
        ))}

        {divArea && <path d={divArea} fill="#047857" fillOpacity={0.06} stroke="none" />}
        {feeArea && <path d={feeArea} fill="#b91c1c" fillOpacity={0.06} stroke="none" />}
        {n > 1 && <path d={divLine} fill="none" stroke="#047857" strokeWidth="1.5" strokeLinejoin="round" />}
        {n > 1 && <path d={feeLine} fill="none" stroke="#b91c1c" strokeWidth="1.5" strokeLinejoin="round" />}

        {series.map((s, i) => {
          const dot = n <= 12 ? 2 : i === n - 1 ? 3 : 0;
          if (dot === 0) return null;
          return (
            <g key={i}>
              <circle cx={x(i)} cy={y(s.cumD)} r={dot} fill="#047857" stroke="#fafafa" strokeWidth="1.5" />
              <circle cx={x(i)} cy={y(s.cumF)} r={dot} fill="#b91c1c" stroke="#fafafa" strokeWidth="1.5" />
            </g>
          );
        })}

        <text x={PAD.left} y={H - 8} fontSize="11" fill="#a1a1aa" fontFamily="var(--font-geist-mono)">
          {fmtDate(series[0].date)}
        </text>
        <text
          x={W - PAD.right}
          y={H - 8}
          textAnchor="end"
          fontSize="11"
          fill="#a1a1aa"
          fontFamily="var(--font-geist-mono)"
        >
          {fmtDate(series[n - 1].date)}
        </text>
      </svg>
    </div>
  );
}
