type Position = { ticker: string; sector?: string | null; market_value_usd: number };

type Slice = { label: string; value: number; pct: number; isCash: boolean };

function pctStr(n: number) {
  return `${n.toFixed(1)}%`;
}

export default function SectorExposure({
  positions,
  cash,
  totalEquity,
}: {
  positions: Position[];
  cash: number;
  totalEquity: number;
}) {
  // Sum market value by sector; anything without a sector is bucketed as "Other".
  const bySector = new Map<string, number>();
  for (const p of positions ?? []) {
    const key = p.sector && p.sector.trim() ? p.sector.trim() : "Other";
    bySector.set(key, (bySector.get(key) ?? 0) + (p.market_value_usd || 0));
  }

  const invested = [...bySector.values()].reduce((a, b) => a + b, 0);
  // Honest denominator: share of the whole book. Falls back to computed total if equity is unset.
  const denom = totalEquity > 0 ? totalEquity : invested + cash || 1;

  const sectorSlices: Slice[] = [...bySector.entries()]
    .filter(([, v]) => v > 0)
    .map(([label, value]) => ({ label, value, pct: (value / denom) * 100, isCash: false }));

  // All-cash: nothing invested. Show an honest, quiet state rather than an empty component.
  if (sectorSlices.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-line px-6 py-10 text-center">
        <p className="font-medium">Entirely in cash</p>
        <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-2 leading-relaxed">
          No open positions, so there is no sector exposure to chart. Cash is {pctStr((cash / denom) * 100)} of
          the book.
        </p>
      </div>
    );
  }

  const cashSlice: Slice = { label: "Cash", value: cash, pct: (cash / denom) * 100, isCash: true };
  const slices = [...sectorSlices, cashSlice].sort((a, b) => b.value - a.value);

  // Concentration note is about holdings, so it reports the largest *sector*, never cash.
  const topSector = sectorSlices.reduce((a, b) => (b.value > a.value ? b : a));

  return (
    <div>
      <ul className="space-y-3.5">
        {slices.map((s) => (
          <li key={s.label}>
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span className={s.isCash ? "text-ink-2" : "font-medium"}>{s.label}</span>
              <span className="font-[family-name:var(--font-geist-mono)] text-ink-2">{pctStr(s.pct)}</span>
            </div>
            <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-line">
              <div
                className={`h-full rounded-full ${s.isCash ? "bg-ink-3" : "bg-accent"}`}
                style={{ width: `${Math.min(100, Math.max(1, s.pct))}%` }}
              />
            </div>
          </li>
        ))}
      </ul>

      <p className="mt-4 text-[12px] text-ink-3">
        Most concentrated:{" "}
        <span className="font-[family-name:var(--font-geist-mono)]">{topSector.label}</span> at{" "}
        {pctStr(topSector.pct)} of equity.
      </p>
    </div>
  );
}
