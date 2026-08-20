"use client";

import { fmtNum as fmt } from "@/lib/api";

/**
 * Severity split as a semicircular gauge.
 *
 * The arc is TWO segments -- red for the Critical share, amber for the
 * Warning share -- so the proportion is readable at a glance without reading
 * a number. The centre shows the Critical percentage, because that is the
 * figure anyone reviewing this actually acts on.
 *
 * Text is SVG <text>, not HTML overlaid on top, so it scales with the arc at
 * any card width and can never drift out of position.
 */
export default function ScoreGauge({
  criticalPct, warningPct, criticalCount, warningCount,
}: {
  criticalPct: number; warningPct: number;
  criticalCount: number; warningCount: number;
}) {
  const crit = Math.max(0, Math.min(100, criticalPct));

  const CX = 100, CY = 100, R = 78;
  const pt = (pct: number) => {
    const a = (180 - (pct / 100) * 180) * (Math.PI / 180);
    return [CX + R * Math.cos(a), CY - R * Math.sin(a)] as const;
  };
  const [sx, sy] = pt(0);
  const [mx, my] = pt(crit);
  const [ex, ey] = pt(100);

  const total = criticalCount + warningCount;

  return (
    <div className="gwrap">
      <svg viewBox="0 0 200 126" className="garc">
        {/* warning segment spans the whole arc; critical is drawn over it */}
        <path d={`M ${sx} ${sy} A ${R} ${R} 0 0 1 ${ex} ${ey}`}
              fill="none" stroke="var(--warn)" strokeWidth="16" strokeLinecap="round" />
        {crit > 0.5 && (
          <path d={`M ${sx} ${sy} A ${R} ${R} 0 0 1 ${mx.toFixed(2)} ${my.toFixed(2)}`}
                fill="none" stroke="var(--crit)" strokeWidth="16" strokeLinecap="round" />
        )}
        <text x={CX} y="90" textAnchor="middle" fill="var(--crit)"
              style={{ fontSize: 38, fontWeight: 500, letterSpacing: "normal" }}>
          {crit.toFixed(1)}%
        </text>
        <text x={CX} y="117" textAnchor="middle" fill="var(--crit)"
              style={{ fontSize: 12, fontWeight: 500, letterSpacing: "normal" }}>
          CRITICAL
        </text>
      </svg>
      <div className="glegend">
        <span><i style={{ background: "var(--crit)" }} />Critical {crit.toFixed(1)}%</span>
        <span><i style={{ background: "var(--warn)" }} />Warning {warningPct.toFixed(1)}%</span>
      </div>
      <div className="gstats">
        <div className="gstat"><div className="v tone-crit">{fmt(criticalCount)}</div><div className="l">Critical</div></div>
        <div className="gstat"><div className="v tone-warn">{fmt(warningCount)}</div><div className="l">Warning</div></div>
        <div className="gstat"><div className="v">{fmt(total)}</div><div className="l">Total failed</div></div>
      </div>
    </div>
  );
}
