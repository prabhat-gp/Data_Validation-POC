"use client";

import { useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export default function RunsPage() {
  const [runs, setRuns] = useState<any[]>([]);
  const [objectId, setObjectId] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = () => fetch(`${API_BASE}/api/runs`).then((r) => r.json()).then(setRuns);

  useEffect(() => {
    fetch(`${API_BASE}/api/objects`).then((r) => r.json()).then((o) => o[0] && setObjectId(o[0].object_id));
    refresh();
    // poll while any run is still 'running' -- this is how the UI reflects a
    // background job without the request itself ever blocking
    pollRef.current = setInterval(refresh, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  async function uploadAndRun() {
    const file = fileRef.current?.files?.[0];
    if (!file || !objectId) return;
    const form = new FormData();
    form.append("object_id", String(objectId));
    form.append("file", file);
    await fetch(`${API_BASE}/api/runs/upload`, { method: "POST", body: form });
    refresh();
  }

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Validation Runs</h1>
          <div className="sub">Upload a source file and watch the run complete</div>
        </div>
      </div>
      <div className="content" style={{ maxWidth: 1000 }}>
      <section className="card" style={{ marginBottom: 16 }}>
        <h2>Trigger a run (CSV upload)</h2>
        <input ref={fileRef} type="file" accept=".csv" style={{ fontSize: 13, marginBottom: 12 }} />
        <button onClick={uploadAndRun} className="btn-primary">
          Upload &amp; Validate
        </button>
        <p className="mini" style={{marginTop:12}}>
          DB-fetch source is available via POST /api/runs/db-fetch (connection string + query) -- no UI form for it yet.
        </p>
      </section>

      <section className="card">
        <h2>Run History</h2>
        <table className="cde">
          <thead>
            <tr><th>Run</th><th>Status</th><th>Records</th><th>Started</th></tr>
          </thead>
          <tbody>
            {runs.length === 0 && (
              <tr><td colSpan={4} className="mini" style={{ padding: "14px 10px" }}>No runs yet.</td></tr>
            )}
            {runs.map((r) => (
              <tr key={r.run_id}>
                <td style={{ fontWeight: 600 }}>#{r.run_id} {r.run_name}</td>
                <td><span className={`badge ${statusBadge(r.status)}`}>{r.status}</span></td>
                <td className="tnum">{r.records_scanned?.toLocaleString?.() ?? r.records_scanned}</td>
                <td className="mini">{new Date(r.started_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      </div>
    </>
  );
}

function statusBadge(status: string) {
  return { running: "b-sub", completed: "b-appr", failed: "b-rej" }[status] || "b-draft";
}
