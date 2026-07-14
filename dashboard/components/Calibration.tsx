type CalibrationBucket = {
  trades: number;
  win_rate_pct: number;
  avg_stated_confidence_pct: number;
  calibration_gap_pct: number;
};

type CalibrationData = {
  note: string;
  by_confidence: Record<string, CalibrationBucket>;
  high_conf_0_70_plus: { trades: number; win_rate_pct: number | null; inflated: boolean };
};

export default function Calibration({ data }: { data: CalibrationData }) {
  const buckets = Object.entries(data.by_confidence ?? {});
  const hc = data.high_conf_0_70_plus;

  return (
    <section className="border-t border-line py-12">
      <h2 className="text-lg font-semibold tracking-tight">Confidence calibration</h2>
      <p className="mt-1 max-w-2xl text-sm text-ink-2">
        Every trade is entered with a stated confidence. Here is how those numbers held up against
        what actually happened - the agent grading its own conviction, bucket by bucket.
      </p>

      {hc?.inflated && (
        <div className="mt-6 flex gap-3 rounded-lg border border-attn/40 bg-attn-soft px-4 py-3 text-sm text-attn">
          <span aria-hidden className="mt-0.5 select-none leading-none">
            &#9650;
          </span>
          <p className="leading-relaxed">
            The agent&apos;s high-confidence calls are underperforming its stated confidence
            {hc.win_rate_pct != null && hc.trades
              ? ` - its 0.70+ bucket has won ${hc.win_rate_pct}% over ${hc.trades} trades`
              : ""}
            . It is capping stated confidence until the gap closes.
          </p>
        </div>
      )}

      {buckets.length > 0 && (
        <div className="mt-6 overflow-x-auto">
          <table className="w-full min-w-[440px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-line text-[12px] text-ink-3">
                <th className="pb-2 text-left font-normal">Stated confidence</th>
                <th className="pb-2 pl-4 text-right font-normal">Trades</th>
                <th className="pb-2 pl-4 text-right font-normal">Avg stated</th>
                <th className="pb-2 pl-4 text-right font-normal">Actual win rate</th>
                <th className="pb-2 pl-4 text-right font-normal">Gap</th>
              </tr>
            </thead>
            <tbody className="font-[family-name:var(--font-mono)]">
              {buckets.map(([bucket, b]) => {
                const gap = b.calibration_gap_pct;
                const gapClass = gap < 0 ? "text-neg" : gap > 0 ? "text-accent" : "text-ink-2";
                return (
                  <tr key={bucket} className="border-b border-line/60">
                    <td className="py-2.5 text-left">{bucket}</td>
                    <td className="py-2.5 pl-4 text-right">{b.trades}</td>
                    <td className="py-2.5 pl-4 text-right text-ink-2">{b.avg_stated_confidence_pct}%</td>
                    <td className="py-2.5 pl-4 text-right">{b.win_rate_pct}%</td>
                    <td className={`py-2.5 pl-4 text-right ${gapClass}`}>
                      {gap > 0 ? "+" : ""}
                      {gap}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-4 text-[12px] text-ink-3">
        Gap = actual win rate minus average stated confidence. A negative gap means the agent was
        overconfident in that band.
      </p>
    </section>
  );
}
