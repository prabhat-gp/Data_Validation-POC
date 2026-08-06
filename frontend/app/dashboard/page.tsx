"use client";

import { useEffect, useState } from "react";
import { Rule, api, BatchOption, Kpis, HeatmapRow, TopFailingItem, TrendPoint, FixProfile, CriticalByDimension, Drilldown, DrilldownElement } from "@/lib/api";
import Heatmap from "./Heatmap";
import DonutChart from "./DonutChart";
import TrendChart from "./TrendChart";
import ScoreGauge from "./ScoreGauge";
import RuleDetail, { RuleRunResult } from "@/app/components/RuleDetail";

export default function DashboardPage() {
  const [selectedObjectId, setSelectedObjectId] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [batchId, setBatchId] = useState<number | null>(null);
  const [batchOpts, setBatchOpts] = useState<BatchOption[]>([]);
  // Objects in the current run, lifted out of the heatmap so the drilldown can
  // offer a "jump to object" picker without going back to the overview -- and
  // without a second request, since the overview already fetched this list.
  const [objects, setObjects] = useState<ObjectOption[]>([]);

  // Run ID is meaningless without a source, so it stays disabled until one is
  // picked and then only offers batches from that source.
  // the Data Source picker now lives in the sidebar
  useEffect(() => {
    setSource(localStorage.getItem("source") || "Hybris");
    const onSrc = (e: any) => setSource(e.detail);
    window.addEventListener("dq-source", onSrc);
    return () => window.removeEventListener("dq-source", onSrc);
  }, []);

  useEffect(() => {
    let stale = false;                       // set when `source` changes again
    if (!source) { setBatchOpts([]); setBatchId(null); return; }
    api.batchOptions(source)
      .then((opts) => {
        if (stale) return;                   // a newer source won -- drop this
        setBatchOpts(opts);
        // land on the newest run rather than making the user pick one
        setBatchId(opts.length ? opts[0].batch_id : null);
      })
      .catch(() => { if (!stale) { setBatchOpts([]); setBatchId(null); } });
    return () => { stale = true; };
  }, [source]);

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Data Validation Dashboard</h1>
          <div className="sub">
            {selectedObjectId ? "Object drilldown" : source ? `${source} · all objects` : ""}
          </div>
        </div>
        <div className="spacer" />
        <div className="flt">
          <span className="fl">Run ID</span>
          <select value={batchId ?? ""}
                  onChange={(e) => setBatchId(e.target.value ? Number(e.target.value) : null)}>
            <option value="">Latest</option>
            {batchOpts.map((b) => <option key={b.batch_id} value={b.batch_id}>#{b.batch_id}</option>)}
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
          source && <Overview onSelectObject={setSelectedObjectId} onObjects={setObjects} batchId={batchId} source={source} />
        ) : (
          <ObjectDrilldown objectId={selectedObjectId} source={source || ""} objects={objects}
                           onSelectObject={setSelectedObjectId} onBack={() => setSelectedObjectId(null)} />
        )}
      </div>
    </>
  );
}

interface ObjectOption { id: string; name: string }

function Overview({ onSelectObject, onObjects, batchId, source }: {
  onSelectObject: (id: string) => void; onObjects: (o: ObjectOption[]) => void;
  batchId: number | null; source: string;
}) {
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [heatmap, setHeatmap] = useState<HeatmapRow[]>([]);
  const [topFailing, setTopFailing] = useState<TopFailingItem[]>([]);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [fixProfile, setFixProfile] = useState<FixProfile | null>(null);
  const [critByDim, setCritByDim] = useState<CriticalByDimension | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // single request instead of six -- see /api/dashboard/summary
    let stale = false;              // guards against an out-of-order response
    setKpis(null);
    api.summary(batchId, source)
      .then((d) => {
        if (stale) return;
        setError(null);
        setKpis(d.kpis); setHeatmap(d.heatmap); setTopFailing(d.top_failing);
        setTrend(d.trend); setFixProfile(d.fix_profile); setCritByDim(d.critical_by_dimension);
        onObjects(d.heatmap.map((h) => ({ id: h.object_id, name: h.object_name })));
      })
      .catch((e) => { if (!stale) setError(String(e.message || e)); });
    return () => { stale = true; };
  }, [batchId, source]);

  if (error) {
    return (
      <div className="card">
        <h2>No completed runs for {source}</h2>
        <p className="mini">
          Go to <b>Runs</b> and validate some objects from this source, or pick a
          different source in the sidebar.
        </p>
        <p className="mini" style={{ marginTop: 8, opacity: .7 }}>{error}</p>
      </div>
    );
  }
  if (!kpis) return <SkeletonOverview />;

  return (
    <>
      <div className="kpis k6">
        <Kpi label="Overall DQ Score" value={`${kpis.overall_dq_score ?? "—"}%`} strip={ragStrip(kpis.overall_dq_score)} sub={`${fmt(kpis.checks_run - kpis.checks_failed)} of ${fmt(kpis.checks_run)} checks passed`} />
        <Kpi label="Objects Checked" value={kpis.objects_checked} strip="acc" sub="Critical Data Objects" />
        <Kpi label="CDEs Checked" value={kpis.cdes_checked} strip="acc" sub="Critical Data Elements" />
        <Kpi label="Records Scanned" value={fmt(kpis.records_scanned)} strip="acc" sub={`${kpis.objects_checked} objects · ${fmt(kpis.records_affected)} affected`} />
        <Kpi label="Critical Failed Checks" value={fmt(kpis.critical_failed_checks)} strip="crit" sub={`of ${fmt(kpis.checks_run)} checks run`} />
        <Kpi label="Rule Coverage" value={`${kpis.rule_coverage_pct}%`} strip="acc" sub={`${kpis.cdes_checked} CDEs have rules`} />
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

function ObjectDrilldown({ objectId, source, objects, onSelectObject, onBack }: {
  objectId: string; source: string; objects: ObjectOption[];
  onSelectObject: (id: string) => void; onBack: () => void;
}) {
  const [data, setData] = useState<Drilldown | null>(null);
  const [error, setError] = useState<string | null>(null);
  // which rule produced the row the user clicked, plus that row's numbers
  const [ruleDetail, setRuleDetail] = useState<{ rule: Rule; result: RuleRunResult } | null>(null);

  async function showRule(el: DrilldownElement) {
    try {
      const rule = await api.rule(el.rule_id);
      setRuleDetail({
        rule,
        result: {
          entity_name: objectId, element_name: el.element_name, dimension: el.dimension,
          records_checked: el.records_checked, records_failed: el.records_failed,
          score_pct: el.score_pct,
        },
      });
    } catch { /* a demo-seeded metric has no real rule behind it */ }
  }

  useEffect(() => {
    setData(null);
    api.drilldown(objectId, source).then(setData).catch((e) => setError(String(e.message || e)));
  }, [objectId, source]);

  if (error) return <p className="mini">{error}</p>;
  if (!data) return <p className="mini">Loading…</p>;

  const dims = Object.entries(data.dimension_scores).sort((a, b) => b[1] - a[1]);
  const totalFailed = data.elements.reduce((a, e) => a + e.records_failed, 0);

  return (
    <>
      <div className="crumbbar">
        <button className="backbtn" onClick={onBack}>◂ Back to overview</button>
        <span className="crumb">All objects&nbsp;›&nbsp;<b>{data.object_name}</b></span>
        {/* switch object in place -- no trip back to the overview to pick another */}
        {objects.length > 1 && (
          <div className="crumbjump">
            <span className="fl">Object</span>
            <select value={objectId} onChange={(e) => onSelectObject(e.target.value)}>
              {objects.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          </div>
        )}
      </div>

      <div className="kpis k4">
        <Kpi label="Overall DQ Score" value={`${data.overall_score}%`} strip={ragStrip(data.overall_score)} sub={data.object_name} />
        <Kpi label="Elements Checked" value={data.elements_checked} strip="acc" sub={`${data.checks_run} checks run`} />
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
          <h4>{data.object_name} · Data Elements <span className="hint">{data.checks_run} checks across {data.elements_checked} element{data.elements_checked === 1 ? "" : "s"} · click a row to see the rule behind it</span></h4>
          <table className="cde">
            <thead>
              <tr><th>Element</th><th>Dimension</th><th>Score</th><th>Failed</th><th>Severity</th></tr>
            </thead>
            <tbody>
              {data.elements.map((el, i) => (
                <tr key={`${el.element_name}-${el.dimension}-${i}`}
                    className="clickable" onClick={() => showRule(el)}>
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

      {ruleDetail && (
        <RuleDetail rule={ruleDetail.rule} result={ruleDetail.result}
                    onClose={() => setRuleDetail(null)} />
      )}
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
      <div className="kpis k6">
        {Array.from({ length: 6 }).map((_, i) => <div key={i} className="kpi sk" style={{ height: 86 }} />)}
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
