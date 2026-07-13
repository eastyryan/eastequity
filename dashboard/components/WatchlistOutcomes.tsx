// Permissive: the orchestrator emits these with nulls/omissions (price may be missing on
// a blocked run). Runtime code below guards every field via num()/truthiness, so the type
// mirrors the real data shape rather than an idealized one.
type WatchlistOutcome = {
  ticker: string;
  first_watched: string;
  price_when_added?: number | null;
  would_buy_at?: string | null;
  one_line?: string;
  currently_watched?: boolean;
  hit_buy_level?: boolean;
  hit_date?: string;
  acted?: boolean;
  latest_price?: number | null;
  move_pct_since_watched?: number | null;
  dropped_date?: string;
};

// Mirror of page.tsx's markdown-stripping helper for any agent prose.
function clean(s: string) {
  return s
    .replace(/—/g, "-")
    .replace(/–/g, "-")
    .replace(/\*\*/g, "")
    .replace(/[`*]/g, "");
}

function num(v: unknown): number | null {
  return typeof v === "number" && !Number.isNaN(v) ? v : null;
}

function OutcomeCard({ o }: { o: WatchlistOutcome }) {
  const move = num(o.move_pct_since_watched);
  const priceAdded = num(o.price_when_added);
  const priceNow = num(o.latest_price);
  const buyAt = o.would_buy_at ? clean(o.would_buy_at) : null;

  return (
    <li className="py-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex flex-wrap items-baseline gap-2.5">
          <span className="font-medium font-[family-name:var(--font-geist-mono)]">{o.ticker}</span>
          {o.acted ? (
            <span className="rounded-full border border-emerald-300 bg-accent-soft px-2 py-0.5 text-[10px] font-medium uppercase text-accent font-[family-name:var(--font-geist-mono)]">
              Bought
            </span>
          ) : (
            <span className="rounded-full border border-line bg-white px-2 py-0.5 text-[10px] font-medium uppercase text-ink-3 font-[family-name:var(--font-geist-mono)]">
              Not acted
            </span>
          )}
          {o.hit_buy_level && (
            <span className="inline-flex items-center gap-1 text-[12px] text-accent font-[family-name:var(--font-geist-mono)]">
              <span aria-hidden>✓</span> hit buy level{o.hit_date ? ` ${o.hit_date}` : ""}
            </span>
          )}
        </div>
        {move != null && (
          <span
            className={`text-sm font-[family-name:var(--font-geist-mono)] ${
              move >= 0 ? "text-accent" : "text-red-700"
            }`}
          >
            {move >= 0 ? "+" : ""}
            {move.toFixed(1)}% since watched
          </span>
        )}
      </div>

      {o.one_line && (
        <p className="mt-1.5 text-sm text-ink-2 leading-relaxed">{clean(o.one_line)}</p>
      )}

      <p className="mt-2 flex flex-wrap gap-x-2 gap-y-0.5 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
        {o.first_watched && <span>Watched since {o.first_watched}</span>}
        {buyAt && (
          <>
            <span aria-hidden>·</span>
            <span>would buy {buyAt}</span>
          </>
        )}
        {!o.hit_buy_level && (
          <>
            <span aria-hidden>·</span>
            <span>not yet triggered</span>
          </>
        )}
        {priceAdded != null && priceNow != null && (
          <>
            <span aria-hidden>·</span>
            <span>
              ${priceAdded.toFixed(2)} → ${priceNow.toFixed(2)}
            </span>
          </>
        )}
        {o.dropped_date && (
          <>
            <span aria-hidden>·</span>
            <span>dropped {o.dropped_date}</span>
          </>
        )}
      </p>
    </li>
  );
}

export default function WatchlistOutcomes({ outcomes }: { outcomes: WatchlistOutcome[] }) {
  const all = outcomes ?? [];
  // A name is "still watched" unless it has been explicitly dropped or flagged not-watched.
  const watching = all.filter((o) => o.currently_watched !== false && !o.dropped_date);
  const dropped = all.filter((o) => o.currently_watched === false || o.dropped_date);

  return (
    <section className="border-t border-line py-12">
      <h2 className="text-lg font-semibold tracking-tight">Watchlist foresight</h2>
      <p className="mt-1 max-w-2xl text-sm text-ink-2">
        Grading the agent&apos;s calls before it acts: every name it flagged, whether the price
        reached its stated buy level, whether it followed through, and how the name moved since.
      </p>

      {all.length === 0 ? (
        <div className="mt-6 rounded-lg border border-dashed border-line px-6 py-10 text-center">
          <p className="font-medium">No watchlist history yet</p>
          <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-2 leading-relaxed">
            Once the agent has watched names over time, their outcomes will be tracked here.
          </p>
        </div>
      ) : (
        <div className="mt-6 space-y-10">
          {watching.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-ink-2">Currently watching</h3>
              <ul className="mt-2 divide-y divide-line/60">
                {watching.map((o, i) => (
                  <OutcomeCard key={`w-${o.ticker}-${i}`} o={o} />
                ))}
              </ul>
            </div>
          )}

          {dropped.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-ink-3">Dropped from the watchlist</h3>
              <ul className="mt-2 divide-y divide-line/60 opacity-80">
                {dropped.map((o, i) => (
                  <OutcomeCard key={`d-${o.ticker}-${i}`} o={o} />
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
