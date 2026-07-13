type CalibrationBucket = {
  trades: number;
  win_rate_pct: number;
  avg_stated_confidence_pct: number;
  calibration_gap_pct: number;
};

type CalibrationProp = {
  by_confidence: Record<string, CalibrationBucket>;
  high_conf_0_70_plus?: { trades: number; win_rate_pct: number | null; inflated: boolean };
} | null;

// Square plot. Inner area is kept square so the 45deg diagonal reads as a true 45deg.
const SIZE = 340;
const PAD = { top: 20, right: 20, bottom: 56, left: 56 };
// Inner is square (264 both ways) so the 45deg "perfect calibration" line reads true.
const INNER = SIZE - PAD.left - PAD.right; // 264 === SIZE - PAD.top - PAD.bottom

// Overconfidence tolerance: a realized win rate this many points below stated confidence
// (or worse) is drawn red. Small misses stay accent so the diagram is not alarmist.
const OVERCONFIDENT_GAP_PCT = -8;

const ACCENT = "#047857";
const RED = "#b91c1c";
const INK2 = "#52525b";
const INK3 = "#a1a1aa";
const LINE = "#e4e4e7";
const PAPER = "#fafafa";

const sx = (v: number) => PAD.left + (v / 100) * INNER;
const sy = (v: number) => PAD.top + INNER - (v / 100) * INNER;

export default function CalibrationDiagram({ calibration }: { calibration: CalibrationProp }) {
  const buckets = calibration ? Object.entries(calibration.by_confidence ?? {}) : [];

  if (buckets.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-line px-6 py-10 text-center">
        <p className="text-sm text-ink-2">Calibration appears once trades close.</p>
      </div>
    );
  }

  const maxTrades = Math.max(...buckets.map(([, b]) => b.trades), 1);
  // Area-proportional sizing so a busy bucket looks weightier without swamping the plot.
  const radius = (trades: number) => 5 + 15 * Math.sqrt(Math.max(1, trades) / maxTrades);

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        className="w-full h-auto max-w-[380px]"
        role="img"
        aria-label="Reliability diagram: stated confidence versus realized win rate, per confidence bucket"
      >
        {/* Grid */}
        {[0, 25, 50, 75, 100].map((t) => (
          <g key={t}>
            <line x1={sx(t)} x2={sx(t)} y1={PAD.top} y2={PAD.top + INNER} stroke={LINE} strokeWidth="1" />
            <line x1={PAD.left} x2={PAD.left + INNER} y1={sy(t)} y2={sy(t)} stroke={LINE} strokeWidth="1" />
          </g>
        ))}

        {/* Perfectly-calibrated diagonal */}
        <line
          x1={sx(0)}
          y1={sy(0)}
          x2={sx(100)}
          y2={sy(100)}
          stroke={INK3}
          strokeWidth="1.5"
          strokeDasharray="4 4"
        />

        {/* Axis ticks */}
        {[0, 50, 100].map((t) => (
          <text
            key={`xt-${t}`}
            x={sx(t)}
            y={PAD.top + INNER + 16}
            textAnchor="middle"
            fontSize="10"
            fill={INK3}
            fontFamily="var(--font-geist-mono)"
          >
            {t}
          </text>
        ))}
        {[0, 50, 100].map((t) => (
          <text
            key={`yt-${t}`}
            x={PAD.left - 8}
            y={sy(t) + 3}
            textAnchor="end"
            fontSize="10"
            fill={INK3}
            fontFamily="var(--font-geist-mono)"
          >
            {t}
          </text>
        ))}

        {/* Axis titles */}
        <text x={PAD.left + INNER / 2} y={SIZE - 8} textAnchor="middle" fontSize="11" fill={INK2}>
          Stated confidence (%)
        </text>
        <text
          transform={`translate(14, ${PAD.top + INNER / 2}) rotate(-90)`}
          textAnchor="middle"
          fontSize="11"
          fill={INK2}
        >
          Realized win rate (%)
        </text>

        {/* Buckets */}
        {buckets.map(([bucket, b]) => {
          const px = sx(b.avg_stated_confidence_pct);
          const py = sy(b.win_rate_pct);
          const r = radius(b.trades);
          const overconfident = b.calibration_gap_pct <= OVERCONFIDENT_GAP_PCT;
          const color = overconfident ? RED : ACCENT;
          // Keep labels off the right edge by flipping them left in the far-right third.
          const flipLeft = b.avg_stated_confidence_pct > 70;
          const lx = flipLeft ? px - r - 5 : px + r + 5;
          return (
            <g key={bucket}>
              <circle cx={px} cy={py} r={r} fill={color} fillOpacity="0.85" stroke={PAPER} strokeWidth="2" />
              <text
                x={lx}
                y={py + 3}
                textAnchor={flipLeft ? "end" : "start"}
                fontSize="10"
                fill={INK2}
                fontFamily="var(--font-geist-mono)"
              >
                {bucket}
              </text>
            </g>
          );
        })}
      </svg>

      <figcaption className="mt-3 max-w-md text-[12px] leading-relaxed text-ink-3">
        Dashed line = perfect calibration. A point below it means the agent was overconfident in that
        band; above means underconfident. Dot size scales with the number of trades.
      </figcaption>
    </figure>
  );
}
