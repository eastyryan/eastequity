"use client";

import { useState } from "react";

type ClosedTrade = {
  ticker: string;
  entry_price: number;
  exit_price: number;
  opened_at: string;
  closed_at: string;
  days_held: number;
  pnl_usd: number | null;
  r_multiple: number | null;
  verdict: string;
  thesis?: string | null;
};

const PAGE = 10;

function usd(n: number) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

export default function ClosedTrades({ trades }: { trades: ClosedTrade[] }) {
  const [visible, setVisible] = useState(PAGE);
  const shown = trades.slice(0, visible);

  return (
    <div>
      <ul className="mt-6 divide-y divide-line/60">
        {shown.map((t, i) => (
          <li key={`${t.ticker}-${t.closed_at}-${i}`} className="py-4">
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <div className="flex items-baseline gap-3">
                <span className="font-medium font-[family-name:var(--font-geist-mono)]">{t.ticker}</span>
                <span className="text-[13px] text-ink-3">{t.verdict}</span>
              </div>
              <span
                className={`font-[family-name:var(--font-geist-mono)] text-sm ${
                  (t.pnl_usd ?? 0) >= 0 ? "text-accent" : "text-red-700"
                }`}
              >
                {(t.pnl_usd ?? 0) >= 0 ? "+" : ""}
                {usd(t.pnl_usd ?? 0)}
                {t.r_multiple !== null && ` (${t.r_multiple >= 0 ? "+" : ""}${t.r_multiple}R)`}
              </span>
            </div>
            <p className="mt-1 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
              ${t.entry_price.toFixed(2)} to ${t.exit_price.toFixed(2)} over {t.days_held}d, closed {t.closed_at}
            </p>
            {t.thesis && (
              <details className="mt-2">
                <summary className="cursor-pointer text-[13px] text-ink-2 hover:text-ink">
                  Original thesis
                </summary>
                <p className="mt-1.5 text-sm text-ink-2 leading-relaxed">{t.thesis}</p>
              </details>
            )}
          </li>
        ))}
      </ul>
      {trades.length > visible && (
        <button
          onClick={() => setVisible((v) => v + PAGE)}
          className="mt-4 rounded-md border border-line px-3.5 py-1.5 text-sm text-ink-2 hover:border-zinc-300 hover:text-ink active:scale-[0.98]"
        >
          Show older trades
        </button>
      )}
    </div>
  );
}
