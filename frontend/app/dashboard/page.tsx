"use client";

import { useEffect, useState } from "react";
import { api, BatchOption, Kpis, HeatmapRow, TopFailingItem, TrendPoint, FixProfile, CriticalByDimension, Drilldown } from "@/lib/api";
import Heatmap from "./Heatmap";
import DonutChart from "./DonutChart";
import TrendChart from "./TrendChart";
import ScoreGauge from "./ScoreGauge";

export default function DashboardPage() {
  const [selectedObjectId, setSelectedObjectId] = useState<string | null>(null);
  const [source, setSource] = useState("db_fetch");   // sensible default
  const [batchId, setBatchId] = useState<number | null>(null);
  const [batchOpts, setBatchOpts] = useState<BatchOption[]>([]);

  // Run ID is meaningless without a source, so it stays disabled until one is
  // picked and then only offers batches from that source.
  useEffect(() => {
    if (!source) { setBatchOpts([]); setBatchId(null); return; }
    api.batchOptions(source)
      .then((opts) => {
        setBatchOpts(opts);
        // land on the newest run rather than making the user pick one
        setBatchId(opts.length ? opts[0].batch_id : null);
      })
      .catch(() => { setBatchOpts([]); setBatchId(null); });
  }, [source]);

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Data Validation Dashboard</h1>
          <div className="sub">
            {selectedObjectId ? "Object drilldown" : "All objects · database overview"}
          </div>
        </div>
        <div className="spacer" />
        <div className="flt">
          <span className="fl">Data Source</span>
          <select value={source} onChange={(e) => { setSource(e.target.value); setBatchId(null); }}>
            <option value="">All sources</option>
            <option value="db_fetch">Database</option>
            <option value="file_upload">File upload</option>
          </select>
        </div>
        <div className="flt">
          <span className="fl">Run ID</span>
          <select value={batchId ?? ""} disabled={!source}
                  onChange={(e) => setBatchId(e.target.value ? Number(e.target.value) : null)}>
            <option value="">Latest per object</option>
            {batchOpts.map((b) => (
              <option key={b.batch_id} value={b.batch_id}>
                #{b.batch_id} · {b.entity_count} obj
              </option>
            ))}
          </select>
        </div>
        <div className="flt">
          <span className="fl">Run Date</span>
          <span style={{ fontSize: 12.5, fontFamily: "ui-monospace, monospace", fontWeight: 700, paddingTop: 6 }}>
            {new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })}
          </span>
        </div>
      </div>

      <div className="content">
        {selectedObjectId == null ? (
          <Overview onSelectObject={setSelectedObjectId} batchId={batchId} />
        ) : (
          <ObjectDrilldown objectId={selectedObjectId} onBack={() => setSelectedObjectId(null)} />
        )}
      </div>
    </>
  );
}

function Overview({ onSelectObject, batchId }: { onSelectObject: (id: string) => void; batchId: number | null }) {
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [heatmap, setHeatmap] = useState<HeatmapRow[]>([]);
  const [topFailing, setTopFailing] = useState<TopFailingItem[]>([]);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [fixProfile, setFixProfile] = useState<FixProfile | null>(null);
  const [critByDim, setCritByDim] = useState<CriticalByDimension | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // single request instead of six -- see /api/dashboard/summary
    setKpis(null);
    api.summary(batchId)
      .then((d) => {
        setKpis(d.kpis); setHeatmap(d.heatmap); setTopFailing(d.top_failing);
        setTrend(d.trend); setFixProfile(d.fix_profile); setCritByDim(d.critical_by_dimension);
      })
      .catch((e) => setError(String(e.message || e)));
  }, [batchId]);

  if (error) {
    return (
      <div className="card">
        <h2>No completed runs yet</h2>
        <p className="mini">
          Upload a file on the <b>Runs</b> page, or seed demo data with <code>python3 seed_dummy.py --reset</code>.
        </p>
        <p className="mini" style={{ marginTop: 8, opacity: .7 }}>{error}</p>
      </div>
    );
  }
  if (!kpis) return <SkeletonOverview />;

  return (
    <>
      <div className="kpis k5">
        <Kpi label="Overall DQ Score" value={`${kpis.overall_dq_score ?? "—"}%`} strip={ragStrip(kpis.overall_dq_score)} sub="weighted across all objects" />
        <Kpi label="Objects Checked" value={kpis.objects_checked} strip="acc" sub="critical data objects" />
        <Kpi label="Critical Failed Checks" value={fmt(kpis.critical_failed_checks)} strip="crit" sub="severity = Critical" />
        <Kpi label="Records Scanned" value={fmt(kpis.records_scanned)} strip="acc" sub={`${kpis.objects_checked} objects`} />
        <Kpi label="Rule Coverage" value={`${kpis.rule_coverage_pct}%`} strip="acc" sub="CDEs with active rules" />
      </div>

      <div className="row" style={{ gridTemplateColumns: "1.55fr 1fr" }}>
        <div className="panel">
          <h4>Object × Dimension Heatmap <span className="hint">&gt;90 green · 80–90 amber · &lt;80 red · click a row to drill →</span></h4>
          <Heatmap rows={heatmap} onSelect={onSelectObject} />
        </div>
        <div className="panel">
          <h4>Critical Failures by Dimension <span className="hint">{critByDim ? fmt(critByDim.total) + " total" : ""}</span></h4>
          <DonutChart
            segments={(critByDim?.breakdown || []).map((b) => ({ label: b.dimension, value: b.count }))}
            centerValue={critByDim ? fmt(critByDim.total) : "0"}
            centerLabel="CRITICAL"
          />
        </div>
      </div>

      <div className="row" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
        <div className="panel">
          <h4>Top Failing Data Elements <span className="hint">across all objects</span></h4>
          <div className="flist">
            {topFailing.map((t) => (
              <div className="frow" key={`${t.object_name}-${t.element_name}`}>
                <span className="fname">{t.element_name}<small>{t.object_name}</small></span>
                <span className={`fscore tone-${t.score_pct > 90 ? "good" : t.score_pct >= 80 ? "warn" : "crit"}`}>{t.score_pct}%</span>
              </div>
            ))}
            {topFailing.length === 0 && <p className="mini">No failures recorded.</p>}
          </div>
        </div>
        <div className="panel">
          <h4>Quality Trend <span className="hint">all runs · not filtered</span></h4>
          <TrendChart data={trend} />
        </div>
        <div className="panel">
          <h4>Severity Split <span className="hint">of {fixProfile ? fmt(fixProfile.critical_count + fixProfile.warning_count) : "0"} failed checks</span></h4>
          {fixProfile && (
            <ScoreGauge
              criticalPct={fixProfile.critical_pct}
              warningPct={fixProfile.warning_pct}
              criticalCount={fixProfile.critical_count}
              warningCount={fixProfile.warning_count}
            />
          )}
        </div>
      </div>
    </>
  );
}

function ObjectDrilldown({ objectId, onBack }: { objectId: string; onBack: () => void }) {
  const [data, setData] = useState<Drilldown | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    api.drilldown(objectId).then(setData).catch((e) => setError(String(e.message || e)));
  }, [objectId]);

  if (error) return <p className="mini">{error}</p>;
  if (!data) return <p className="mini">Loading…</p>;

  const dims = Object.entries(data.dimension_scores).sort((a, b) => b[1] - a[1]);
  const totalFailed = data.elements.reduce((a, e) => a + e.records_failed, 0);

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16, flexWrap: "wrap" }}>
        <button className="backbtn" onClick={onBack}>◂ Back to overview</button>
        <span className="crumb">All objects&nbsp;›&nbsp;<b>{data.object_name}</b></span>
      </div>

      <div className="kpis k4">
        <Kpi label="Overall DQ Score" value={`${data.overall_score}%`} strip={ragStrip(data.overall_score)} sub={data.object_name} />
        <Kpi label="Elements Checked" value={data.elements_checked} strip="acc" sub="CDEs on this object" />
        <Kpi label="Records Scanned" value={fmt(data.records_scanned)} strip="acc" sub="this object" />
        <Kpi label="Failing Checks" value={fmt(totalFailed)} strip="warn" sub="this object" />
      </div>

      <div className="row" style={{ gridTemplateColumns: ".85fr 1.6fr" }}>
        <div className="panel">
          <h4>Dimension Breakdown</h4>
          <DonutChart
            segments={dims
              .map(([dim]) => ({
                label: dim,
                value: data.elements.filter((e) => e.dimension === dim).reduce((a, e) => a + e.records_failed, 0),
              }))
              .sort((a, b) => b.value - a.value)}
            centerValue={`${data.overall_score}%`}
            centerLabel="OVERALL"
          />
        </div>
        <div className="panel">
          <h4>{data.object_name} · Data Elements <span className="hint">element → dimension → score → failed</span></h4>
          <table className="cde">
            <thead>
              <tr><th>Element</th><th>Dimension</th><th>Score</th><th>Failed</th><th>Severity</th></tr>
            </thead>
            <tbody>
              {data.elements.map((el) => (
                <tr key={el.element_name}>
                  <td className="el">{el.element_name}</td>
                  <td>{el.dimension}</td>
                  <td className={`sc tone-${el.score_pct > 90 ? "good" : el.score_pct >= 80 ? "warn" : "crit"}`}>{el.score_pct}%</td>
                  <td className="tnum">{el.records_failed.toLocaleString()}</td>
                  <td><span className={`badge ${el.severity === "Critical" ? "b-crit" : "b-warn"}`}>{el.severity}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function Kpi({ label, value, strip, sub }: { label: string; value: string | number; strip: string; sub?: string }) {
  return (
    <div className="kpi">
      <span className={`strip s-${strip}`} />
      <div className="lab">{label}</div>
      <div className={`val tone-${strip} tnum`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

/** Keeps the layout stable while data loads instead of flashing a bare "Loading…". */
function SkeletonOverview() {
  return (
    <>
      <div className="kpis k5">
        {Array.from({ length: 5 }).map((_, i) => <div key={i} className="kpi sk" style={{ height: 86 }} />)}
      </div>
      <div className="row" style={{ gridTemplateColumns: "1.55fr 1fr" }}>
        <div className="panel sk" style={{ height: 240 }} />
        <div className="panel sk" style={{ height: 240 }} />
      </div>
      <div className="row" style={{ gridTemplateColumns: "1fr 1fr 1fr" }}>
        {Array.from({ length: 3 }).map((_, i) => <div key={i} className="panel sk" style={{ height: 200 }} />)}
      </div>
    </>
  );
}

function ragStrip(score: number | null): string {
  if (score == null) return "acc";
  if (score > 90) return "good";
  if (score >= 80) return "warn";
  return "crit";
}

function fmt(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return Math.round(n / 1000) + "K";
  return String(n);
}
