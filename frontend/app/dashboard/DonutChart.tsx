"use client";

interface Segment {
  label: string;
  value: number;
  color: string;
}

const PALETTE = ["var(--crit)", "var(--warn)", "var(--accent)", "#9333ea", "var(--good)"];

export default function DonutChart({
  segments, centerValue, centerLabel,
}: {
  segments: { label: string; value: number }[];
  centerValue: string;
  centerLabel: string;
}) {
  const total = segments.reduce((a, s) => a + s.value, 0) || 1;
  let acc = 0;
  const stops: string[] = [];
  const withColor: Segment[] = segments.map((s, i) => {
    const color = PALETTE[i % PALETTE.length];
    const pct = (s.value / total) * 100;
    stops.push(`${color} ${acc}% ${acc + pct}%`);
    acc += pct;
    return { ...s, color };
  });

  return (
    <div className="gauge">
      <div className="ring" style={{ background: `conic-gradient(${stops.join(",")})` }}>
        <div className="rc">
          <div className="n tabular-nums">{centerValue}</div>
          <div className="t">{centerLabel}</div>
        </div>
      </div>
      <div className="legend">
        {withColor.map((s) => (
          <div className="li" key={s.label}>
            <span className="sw" style={{ background: s.color }} />
            <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.label}</span>
            <b>{fmt(s.value)} · {Math.round((s.value / total) * 100)}%</b>
          </div>
        ))}
        {segments.length === 0 && <p className="mini">No data.</p>}
      </div>
    </div>
  );
}

function fmt(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return Math.round(n / 1000) + "K";
  return String(n);
}
