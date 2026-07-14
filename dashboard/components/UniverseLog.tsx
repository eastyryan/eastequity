type UniverseLogEntry = {
  date: string;
  added: string[];
  removed: string[];
  dropped_unpriceable: string[];
  size: number;
  rationale: string;
};

// Mirror of page.tsx's markdown-stripping helper for any agent prose.
function clean(s: string) {
  return s
    .replace(/—/g, "-")
    .replace(/–/g, "-")
    .replace(/\*\*/g, "")
    .replace(/[`*]/g, "");
}

export default function UniverseLog({ log }: { log: UniverseLogEntry[] }) {
  // Show newest first regardless of stored order; ISO / YYYY-MM-DD strings sort chronologically.
  const entries = [...(log ?? [])].sort((a, b) => (b.date ?? "").localeCompare(a.date ?? ""));

  return (
    <section className="border-t border-line py-12">
      <h2 className="text-lg font-semibold tracking-tight">Universe changes</h2>
      <p className="mt-1 max-w-2xl text-sm text-ink-2">
        The agent grooms its own hunting ground each week - adding emerging leaders, cutting names
        that have lost their edge. Every revision, newest first.
      </p>

      {entries.length === 0 ? (
        <div className="mt-6 rounded-lg border border-dashed border-line px-6 py-10 text-center">
          <p className="font-medium">No universe changes logged yet</p>
          <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-2 leading-relaxed">
            Revisions to the tradable universe will appear here after the agent&apos;s first review.
          </p>
        </div>
      ) : (
        <ul className="mt-8 space-y-8">
          {entries.map((e, i) => {
            const added = e.added ?? [];
            const removed = e.removed ?? [];
            const dropped = e.dropped_unpriceable ?? [];
            const droppedSet = new Set(dropped);
            return (
              <li key={`${e.date}-${i}`} className="relative border-l border-line pl-6">
                <span
                  className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-accent bg-paper"
                  aria-hidden
                />
                <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                  <span className="text-[13px] text-ink-3 font-[family-name:var(--font-mono)]">
                    {e.date}
                  </span>
                  <span className="text-[13px] text-ink-3 font-[family-name:var(--font-mono)]">
                    <span className="text-accent">+{added.length} added</span>
                    {" / "}
                    <span>−{removed.length} removed</span>
                    {typeof e.size === "number" ? ` · ${e.size} names` : ""}
                  </span>
                </div>

                {(added.length > 0 || removed.length > 0) && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {added.map((t) => (
                      <span
                        key={`add-${t}`}
                        className="rounded border border-pos/25 bg-accent-soft px-1.5 py-px text-[12px] text-accent font-[family-name:var(--font-mono)]"
                      >
                        +{t}
                      </span>
                    ))}
                    {removed.map((t) => (
                      <span
                        key={`rem-${t}`}
                        className={`rounded border border-line px-1.5 py-px text-[12px] font-[family-name:var(--font-mono)] ${
                          droppedSet.has(t) ? "text-attn line-through" : "text-ink-3 line-through"
                        }`}
                        title={droppedSet.has(t) ? "Dropped - no longer priceable" : undefined}
                      >
                        −{t}
                        {droppedSet.has(t) ? " ⚠" : ""}
                      </span>
                    ))}
                  </div>
                )}

                {dropped.length > 0 && (
                  <p className="mt-2 text-[12px] text-attn">
                    Dropped as unpriceable: {dropped.join(", ")}
                  </p>
                )}

                {e.rationale && (
                  <p className="mt-3 text-sm text-ink-2 leading-relaxed">{clean(e.rationale)}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
