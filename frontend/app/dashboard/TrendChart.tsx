"use client";

import { useEffect, useRef, useState } from "react";
import { TrendPoint, fmtNum } from "@/lib/api";

/**
 * DQ Score (line, overlaid) vs Critical Failed Checks (big bars). Ported
 * from the approved Mock A design: measures its OWN real container box at
 * render time (never guesses a fixed aspect ratio -- that caused real bugs
 * before), and mathematically caps the bars so the line's label band can
 * never collide with a bar's own label, regardless of the data.
 */
export default function TrendChart({ data }: { data: TrendPoint[] }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState("");
  const [box, setBox] = useState({ w: 640, h: 230 });

  useEffect(() => {
    if (wrapRef.current) {
      const rect = wrapRef.current.getBoundingClientRect();
      setBox({ w: Math.max(280, Math.round(rect.width)), h: Math.max(150, Math.round(rect.height)) });
    }
  }, [data]);

  useEffect(() => {
    if (!data.length) return;
    setSvg(buildSvg(data, box.w, box.h));
  }, [data, box]);

  return (
    <div ref={wrapRef} style={{ flex: 1, minHeight: 150, minWidth: 0, display: "flex" }}>
      {/* width:100% (not a fixed px width) so the SVG can shrink with its
          grid column -- a hard width here made the panel un-shrinkable and
          pushed the neighbouring panel off-screen. viewBox keeps the
          internal geometry correct at any rendered size. */}
      <svg width="100%" height={box.h} viewBox={`0 0 ${box.w} ${box.h}`}
           preserveAspectRatio="xMidYMid meet"
           dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  );
}

function buildSvg(data: TrendPoint[], W: number, H: number): string {
  const padL = 20, padR = 20, padT = 28, padB = 26;
  const plotW = W - padL - padR, plotH = H - padT - padB, baseY = padT + plotH;
  const n = data.length;
  const xs = data.map((_, i) => padL + (plotW / n) * (i + 0.5));
  const barW = Math.min(96, (plotW / n) * 0.62);

  const BAR_FRAC = 0.55;
  const rawMax = Math.max(...data.map((r) => r.critical_failed_checks), 1);
  const barH = (r: TrendPoint) => (r.critical_failed_checks / rawMax) * (plotH * BAR_FRAC);
  const tallestBarTopY = baseY - plotH * BAR_FRAC;

  const LABEL_GAP = 34;
  const lineBandTop = padT + 8;
  const lineBandBottom = Math.max(lineBandTop + 20, tallestBarTopY - LABEL_GAP);
  const dqVals = data.map((r) => r.dq_score ?? 0);
  const dqMin = Math.min(...dqVals) - 1.5, dqMax = Math.max(...dqVals) + 1.5;
  const lineY = (r: TrendPoint) =>
    lineBandBottom - (((r.dq_score ?? 0) - dqMin) / (dqMax - dqMin || 1)) * (lineBandBottom - lineBandTop);

  const pts = data.map((r, i) => [xs[i], lineY(r)] as [number, number]);
  let smooth = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)} `;
  for (let i = 0; i < pts.length - 1; i++) {
    const [x1, y1] = pts[i], [x2, y2] = pts[i + 1];
    const mx = (x1 + x2) / 2;
    smooth += `C${mx.toFixed(1)},${y1.toFixed(1)} ${mx.toFixed(1)},${y2.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)} `;
  }

  let out = `<defs><linearGradient id="barg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="var(--good)" stop-opacity="1"/>
    <stop offset="1" stop-color="var(--good)" stop-opacity=".7"/>
  </linearGradient></defs>`;
  out += `<line x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}" stroke="var(--line)" stroke-width="1.5"/>`;

  data.forEach((r, i) => {
    const h = barH(r), y = baseY - h;
    out += `<rect x="${(xs[i] - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="8" fill="url(#barg)"/>`;
    out += `<text x="${xs[i].toFixed(1)}" y="${(y - 10).toFixed(1)}" font-size="14" font-weight="500" fill="var(--good)" font-family="ui-monospace,monospace" text-anchor="middle">${fmtNum(r.critical_failed_checks)}</text>`;
  });

  out += `<path d="${smooth}" fill="none" stroke="var(--accent)" stroke-width="3.5" stroke-linecap="round"/>`;
  data.forEach((r, i) => {
    const [x, y] = pts[i];
    const label = `${r.dq_score ?? "-"}%`;
    const pillW = 15 + label.length * 8.6;
    out += `<rect x="${(x - pillW / 2).toFixed(1)}" y="${(y - 30).toFixed(1)}" width="${pillW.toFixed(1)}" height="20" rx="10" fill="var(--panel)" stroke="var(--accent)" stroke-width="1.5"/>`;
    out += `<text x="${x.toFixed(1)}" y="${(y - 16).toFixed(1)}" font-size="13" font-weight="500" fill="var(--accent)" font-family="ui-monospace,monospace" text-anchor="middle">${label}</text>`;
    out += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5.5" fill="var(--panel)" stroke="var(--accent)" stroke-width="3"/>`;
  });

  data.forEach((r, i) => {
    out += `<text x="${xs[i].toFixed(1)}" y="${(baseY + 22).toFixed(1)}" font-size="12" font-weight="500" fill="var(--ink)" font-family="ui-monospace,monospace" text-anchor="middle">${r.run_name}</text>`;
  });

  return out;
}
