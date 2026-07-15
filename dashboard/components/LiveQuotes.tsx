"use client";

import { useEffect, useState } from "react";

// Near-live quote strip for holdings + watchlist names. Hydrates client-side
// from /api/live-prices (which reads the live-data branch) and refreshes every
// 60s while the tab is visible. Renders nothing when the feed is absent or has
// no overlap with the page's tickers, so the static publish flow is unchanged.

type Feed = { as_of: string | null; prices: Record<string, number> };

export default function LiveQuotes({
  tickers,
  avgCost = {},
}: {
  tickers: string[];
  avgCost?: Record<string, number>;
}) {
  const [feed, setFeed] = useState<Feed | null>(null);

  useEffect(() => {
    let stopped = false;
    const load = async () => {
      try {
        const r = await fetch("/api/live-prices", { cache: "no-store" });
        const j = (await r.json()) as Feed;
        if (!stopped) setFeed(j);
      } catch {
        /* feed is best-effort; keep whatever we had */
      }
    };
    load();
    const id = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 60_000);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  }, []);

  if (!feed?.as_of) return null;
  const rows = tickers.filter((t) => typeof feed.prices[t] === "number");
  if (rows.length === 0) return null;

  const ageMin = Math.max(
    0,
    Math.round((Date.now() - new Date(feed.as_of).getTime()) / 60_000),
  );
  // Older than a session's worth of staleness = market closed; still show the
  // last session quotes but drop the "live" affordance.
  const fresh = ageMin <= 10;
  const ageLabel =
    ageMin < 1 ? "just now" : ageMin < 90 ? `${ageMin}m ago` : "last session";

  return (
    <section className="ds-card mt-5 px-5 py-4 sm:px-7">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
        <span className="flex shrink-0 items-center gap-2">
          <span
            className={`h-2 w-2 rounded-full ${
              fresh ? "animate-pulse bg-pos" : "bg-faint"
            }`}
            aria-hidden
          />
          <span className="text-[13px] font-semibold tracking-tight text-ink">
            Live quotes
          </span>
          <span className="num text-[12px] text-faint">{ageLabel}</span>
        </span>
        <ul className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          {rows.map((t) => {
            const px = feed.prices[t];
            const cost = avgCost[t];
            const pct = cost ? ((px - cost) / cost) * 100 : null;
            return (
              <li
                key={t}
                className="num flex items-baseline gap-1.5 rounded-lg border border-line bg-sunken px-2.5 py-1 text-[12px]"
              >
                <span className="font-semibold text-ink">{t}</span>
                <span className="text-ink-2">
                  {px.toLocaleString("en-US", {
                    style: "currency",
                    currency: "USD",
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                </span>
                {pct !== null && (
                  <span
                    className={`font-semibold ${
                      Math.abs(pct) < 1e-9
                        ? "text-faint"
                        : pct >= 0
                          ? "text-pos"
                          : "text-neg"
                    }`}
                  >
                    {pct >= 0 ? "+" : ""}
                    {pct.toFixed(1)}%
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}
