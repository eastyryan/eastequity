// Sparkline — tiny in-row/in-card line chart from the design system: colored by
// direction, optional soft neon glow. Pure SVG, server-renderable (pass a
// unique `id` per instance for the filter def).
type SparkProps = {
  id: string;
  data: number[];
  direction?: "up" | "down";
  width?: number;
  height?: number;
  strokeWidth?: number;
  glow?: boolean;
};

function linePath(vals: number[], w: number, h: number, pad: number) {
  const n = vals.length;
  if (n < 2) return "";
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  return vals
    .map((v, i) => {
      const x = (i / (n - 1)) * w;
      const y = h - pad - ((v - min) / span) * (h - pad * 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

export default function Spark({
  id,
  data,
  direction,
  width = 80,
  height = 38,
  strokeWidth = 2,
  glow = true,
}: SparkProps) {
  if (data.length < 2) return null;
  const dir = direction ?? (data[data.length - 1] >= data[0] ? "up" : "down");
  const color = dir === "down" ? "var(--chart-down)" : "var(--chart-up)";
  const d = linePath(data, width, height, 3);
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      fill="none"
      style={{ display: "block", overflow: "visible" }}
      aria-hidden
    >
      {glow ? (
        <defs>
          <filter id={`spark-${id}`} x="-30%" y="-60%" width="160%" height="220%">
            <feDropShadow dx="0" dy="0" stdDeviation="2.2" floodColor={color} floodOpacity="0.6" />
          </filter>
        </defs>
      ) : null}
      <path
        d={d}
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        filter={glow ? `url(#spark-${id})` : undefined}
      />
    </svg>
  );
}
