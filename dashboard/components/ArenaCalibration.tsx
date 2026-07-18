"use client";

/**
 * Confidence calibration, rendered in the Arena's visual language.
 *
 * This replaces the reliability diagram that lived on the retired /ledger page.
 * That version was built against the legacy card system (Tailwind utilities,
 * --chart-* / --surface-* tokens, white panels). The Arena is a different design:
 * inline styles, --ee-* tokens, hairline rules instead of borders, serif display
 * type over an editorial grid. Porting the old component as-is would have dropped
 * a foreign card into the middle of the page, so the chart is rebuilt on the
 * Arena's own primitives rather than moved.
 *
 * What it shows: stated confidence at entry vs. realized win rate, one dot per
 * confidence bucket, against the 45° line of perfect calibration. Below the line
 * is overconfidence — the single most useful honesty check the agent publishes,
 * because its own calibration gate reads these same numbers to cap stated
 * confidence.
 */

type CalibrationBucket = {
  trades: number;
  win_rate_pct: number;
  avg_stated_confidence_pct: number;
  calibration_gap_pct: number;
};

export type CalibrationProp = {
  note?: string;
  by_confidence?: Record<string, CalibrationBucket>;
  high_conf_0_70_plus?: { trades: number; win_rate_pct: number | null; inflated: boolean };
} | null;

// Square inner plot so the 45° diagonal reads as a true 45°.
const SIZE = 360;
const PAD = { top: 18, right: 18, bottom: 54, left: 54 };
const INNER = SIZE - PAD.left - PAD.right;

// A realized win rate this far below stated confidence (or worse) is drawn as a
// miss. Small gaps stay neutral so the chart is informative, not alarmist.
const OVERCONFIDENT_GAP_PCT = -8;

const sx = (v: number) => PAD.left + (v / 100) * INNER;
const sy = (v: number) => PAD.top + INNER - (v / 100) * INNER;

export default function ArenaCalibration({ calibration }: { calibration: CalibrationProp }) {
  const buckets = Object.entries(calibration?.by_confidence ?? {});
  const totalTrades = buckets.reduce((n, [, b]) => n + (b.trades || 0), 0);

  if (buckets.length === 0) {
    return (
      <div
        style={{
          borderTop: "1px solid var(--ee-hair08)",
          padding: "26px 2px",
          fontSize: 12.5,
          color: "var(--ee-bodytx)",
          lineHeight: 1.65,
        }}
      >
        Calibration appears once trades close. Each closed trade is scored against the confidence the
        agent stated when it opened the position.
      </div>
    );
  }

  const maxTrades = Math.max(...buckets.map(([, b]) => b.trades), 1);
  // Area-proportional so a busy bucket carries visual weight without swamping the plot.
  const radius = (trades: number) => 5 + 14 * Math.sqrt(Math.max(1, trades) / maxTrades);

  return (
    // Layout lives in globals.css (.ee-calib-grid) so the 900px collapse to a single
    // column is expressible — an inline style cannot carry a media query.
    <div className="ee-calib-grid">
      <figure style={{ margin: 0 }}>
        <svg
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          style={{ width: "100%", height: "auto", display: "block" }}
          role="img"
          aria-label="Stated confidence versus realized win rate, one point per confidence bucket"
        >
          {[0, 25, 50, 75, 100].map((t) => (
            <g key={t}>
              <line
                x1={sx(t)}
                x2={sx(t)}
                y1={PAD.top}
                y2={PAD.top + INNER}
                stroke="var(--ee-hair07)"
                strokeWidth="1"
              />
              <line
                x1={PAD.left}
                x2={PAD.left + INNER}
                y1={sy(t)}
                y2={sy(t)}
                stroke="var(--ee-hair07)"
                strokeWidth="1"
              />
            </g>
          ))}

          {/* Perfect calibration */}
          <line
            x1={sx(0)}
            y1={sy(0)}
            x2={sx(100)}
            y2={sy(100)}
            stroke="var(--ee-faint)"
            strokeWidth="1.25"
            strokeDasharray="5 5"
          />

          {[0, 50, 100].map((t) => (
            <text
              key={`x${t}`}
              x={sx(t)}
              y={PAD.top + INNER + 18}
              textAnchor="middle"
              fontSize="10"
              fill="var(--ee-faint)"
            >
              {t}
            </text>
          ))}
          {[0, 50, 100].map((t) => (
            <text
              key={`y${t}`}
              x={PAD.left - 10}
              y={sy(t) + 3}
              textAnchor="end"
              fontSize="10"
              fill="var(--ee-faint)"
            >
              {t}
            </text>
          ))}

          <text
            x={PAD.left + INNER / 2}
            y={SIZE - 10}
            textAnchor="middle"
            fontSize="10"
            letterSpacing="0.18em"
            fill="var(--ee-muted)"
          >
            STATED CONFIDENCE
          </text>
          <text
            transform={`translate(13, ${PAD.top + INNER / 2}) rotate(-90)`}
            textAnchor="middle"
            fontSize="10"
            letterSpacing="0.18em"
            fill="var(--ee-muted)"
          >
            REALIZED WIN RATE
          </text>

          {buckets.map(([bucket, b]) => {
            const px = sx(b.avg_stated_confidence_pct);
            const py = sy(b.win_rate_pct);
            const r = radius(b.trades);
            const missed = b.calibration_gap_pct <= OVERCONFIDENT_GAP_PCT;
            const color = missed ? "var(--ee-down)" : "var(--ee-up)";
            // Flip labels inward near the right edge so they never clip.
            const flip = b.avg_stated_confidence_pct > 70;
            return (
              <g key={bucket}>
                {/* Drop to the diagonal: the visual length IS the calibration gap. */}
                <line
                  x1={px}
                  y1={py}
                  x2={px}
                  y2={sy(b.avg_stated_confidence_pct)}
                  stroke={color}
                  strokeWidth="1"
                  strokeOpacity="0.35"
                />
                <circle
                  cx={px}
                  cy={py}
                  r={r}
                  fill={color}
                  fillOpacity="0.82"
                  stroke="var(--ee-bg)"
                  strokeWidth="2"
                />
                <text
                  x={flip ? px - r - 6 : px + r + 6}
                  y={py + 3}
                  textAnchor={flip ? "end" : "start"}
                  fontSize="10"
                  fill="var(--ee-bodytx)"
                >
                  {bucket}
                </text>
              </g>
            );
          })}
        </svg>
      </figure>

      <div>
        <p
          style={{
            fontSize: 12.5,
            color: "var(--ee-bodytx)",
            lineHeight: 1.65,
            margin: "0 0 22px",
            textWrap: "pretty",
          }}
        >
          Every position is opened with a stated confidence. Once it closes, that number is scored
          against what actually happened. The dashed line is perfect calibration — a dot below it
          means the agent believed in a trade more than the result justified. Dot size scales with the
          number of trades in the bucket, and the vertical stem is the gap itself.
        </p>

        <div style={{ display: "flex", flexDirection: "column" }}>
          {buckets.map(([bucket, b]) => {
            const missed = b.calibration_gap_pct <= OVERCONFIDENT_GAP_PCT;
            return (
              <div
                key={bucket}
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  gap: 14,
                  padding: "11px 2px",
                  borderTop: "1px solid var(--ee-hair08)",
                  flexWrap: "wrap",
                }}
              >
                <span style={{ fontSize: 12.5, color: "var(--ee-strong)", width: 84, flexShrink: 0 }}>
                  {bucket}
                </span>
                <span style={{ fontSize: 12, color: "var(--ee-muted)", width: 74, flexShrink: 0 }}>
                  {b.trades} {b.trades === 1 ? "trade" : "trades"}
                </span>
                <span style={{ fontSize: 12, color: "var(--ee-bodytx)", flex: 1, minWidth: 140 }}>
                  said {Math.round(b.avg_stated_confidence_pct)}% · won {Math.round(b.win_rate_pct)}%
                </span>
                <span
                  style={{
                    fontSize: 12.5,
                    color: missed ? "var(--ee-down)" : "var(--ee-up)",
                    flexShrink: 0,
                  }}
                >
                  {b.calibration_gap_pct >= 0 ? "+" : ""}
                  {Math.round(b.calibration_gap_pct)} pts
                </span>
              </div>
            );
          })}
        </div>

        <p
          style={{
            fontSize: 11.5,
            color: "var(--ee-faint)",
            lineHeight: 1.7,
            margin: "18px 0 0",
          }}
        >
          {totalTrades < 15
            ? `Only ${totalTrades} closed ${
                totalTrades === 1 ? "trade" : "trades"
              } so far — far too few to read as a calibration signal. At this sample size the
               confidence interval spans most of the chart. It is published anyway because hiding
               an unflattering number until it improves is how track records get laundered.`
            : `Across ${totalTrades} closed trades. The agent's own calibration gate reads these
               buckets and caps the confidence it is allowed to state when a bucket runs hot.`}
        </p>
      </div>
    </div>
  );
}
