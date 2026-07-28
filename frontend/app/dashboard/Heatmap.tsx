"use client";

import { HeatmapRow, DIMENSION_ORDER, ragClass } from "@/lib/api";

export default function Heatmap({ rows, onSelect }: { rows: HeatmapRow[]; onSelect: (objectId: number) => void }) {
  return (
    <table className="heat">
      <thead>
        <tr>
          <th className="obj">Object</th>
          {DIMENSION_ORDER.map((d) => <th key={d}>{shortLabel(d)}</th>)}
          <th>Overall</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.object_id} className="clk" onClick={() => onSelect(row.object_id)}>
            <td className="obj">{row.object_name}</td>
            {DIMENSION_ORDER.map((d) => {
              const score = row.dimensions[d];
              return (
                <td key={d} className={`cell c-${score != null ? ragClass(score) : "warn"}`}>
                  {score != null ? Math.round(score) : "-"}
                </td>
              );
            })}
            <td className={`cell c-${ragClass(row.overall)} c-ovr`}>{Math.round(row.overall)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function shortLabel(d: string) {
  return { Completeness: "Complete", "Ref Integrity": "Ref Int.", Relationship: "Rel'ship" }[d] || d;
}
