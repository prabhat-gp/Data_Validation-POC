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
    <main className="max-w-4xl mx-auto py-10 px-6 space-y-8">
      <h1 className="text-2xl font-bold">Validation Runs</h1>

      <section className="bg-white rounded-xl border border-slate-200 p-5 space-y-3">
        <h2 className="font-semibold">Trigger a run (CSV upload)</h2>
        <input ref={fileRef} type="file" accept=".csv" className="text-sm" />
        <button onClick={uploadAndRun} className="block px-4 py-2 rounded-lg bg-blue-600 text-white font-medium">
          Upload &amp; Validate
        </button>
        <p className="text-xs text-slate-400">
          DB-fetch source is available via POST /api/runs/db-fetch (connection string + query) -- no UI form for it yet.
        </p>
      </section>

      <section className="bg-white rounded-xl border border-slate-200 p-5">
        <h2 className="font-semibold mb-3">Run History</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500 border-b">
              <th className="py-2">Run</th><th>Status</th><th>Records</th><th>Started</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id} className="border-b last:border-0">
                <td className="py-2">#{r.run_id} {r.run_name}</td>
                <td><span className={`px-2 py-0.5 rounded text-xs ${statusColor(r.status)}`}>{r.status}</span></td>
                <td>{r.records_scanned}</td>
                <td>{new Date(r.started_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

function statusColor(status: string) {
  return {
    running: "bg-blue-100 text-blue-700",
    completed: "bg-green-100 text-green-700",
    failed: "bg-red-100 text-red-700",
  }[status] || "bg-slate-100";
}
