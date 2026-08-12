"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE, SOURCE_SYSTEMS, api, Batch, Entity, SourceCheck, SourceObject, getActor } from "@/lib/api";

interface FileSlot { file: File | null; entity: string }

export default function RunsPage() {
  const [entities, setEntities] = useState<Entity[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [mode, setMode] = useState<"upload" | "db">("db");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [slots, setSlots] = useState<FileSlot[]>([{ file: null, entity: "" }]);
  const [dbSelected, setDbSelected] = useState<string[]>([]);
  const pollRef = useRef<any>(null);
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const [check, setCheck] = useState<SourceCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [dbSource, setDbSource] = useState("MySQL");
  // Tables actually present in the selected source. Loaded from the server,
  // not derived from `entities` -- see api.sourceObjects.
  const [srcObjects, setSrcObjects] = useState<SourceObject[] | null>(null);
  const [srcError, setSrcError] = useState<string | null>(null);

  useEffect(() => {
    api.entities().then((e) => {
      setEntities(e);
      if (e.length) setSlots([{ file: null, entity: e[0].entity_name }]);
    }).catch((e) => setError(String(e.message || e)));
    refresh();
    return () => clearInterval(pollRef.current);
  }, []);

  // re-introspect whenever the source changes
  useEffect(() => {
    let stale = false;
    setSrcObjects(null); setSrcError(null);
    api.sourceObjects(dbSource)
      .then((o) => { if (!stale) setSrcObjects(o); })
      .catch((e) => { if (!stale) { setSrcObjects([]); setSrcError(String(e.message || e)); } });
    return () => { stale = true; };
  }, [dbSource]);

  function refresh() {
    api.batches().then((b) => {
      setBatches(b);
      // keep polling only while something is actually running
      const running = b.some((x) => x.status === "running");
      clearInterval(pollRef.current);
      if (running) pollRef.current = setInterval(() => api.batches().then(setBatches).catch(() => {}), 1200);
    }).catch(() => {});
  }

  async function submitUpload() {
    setError(null); setNotice(null);
    const valid = slots.filter((s) => s.file && s.entity);
    if (!valid.length) { setError("Add at least one file and pick its object."); return; }
    const names = valid.map((s) => s.entity);
    if (new Set(names).size !== names.length) {
      setError("The same object cannot appear twice in one run.");
      return;
    }
    const fd = new FormData();
    fd.append("entity_names", names.join(","));
    valid.forEach((s) => fd.append("files", s.file as File));
    fd.append("batch_name", `Upload · ${new Date().toLocaleString()}`);
    fd.append("triggered_by", "prabhat");

    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/api/runs/upload`, { method: "POST", body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      const b = await res.json();
      setNotice(`Started Run #${b.batch_id} across ${b.entity_count} object${b.entity_count === 1 ? "" : "s"}.`);
      refresh();
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  }

  async function submitDbRun() {
    setError(null); setNotice(null);
    if (!dbSelected.length) { setError("Select at least one object."); return; }
    setBusy(true);
    try {
      const b = await api.runFromDb({
        entity_names: dbSelected, source_system: dbSource,
        batch_name: `DB fetch · ${new Date().toLocaleString()}`,
        triggered_by: "prabhat",
      });
      setNotice(`Started Run #${b.batch_id} across ${b.entity_count} object${b.entity_count === 1 ? "" : "s"}.`);
      // show the batch (and its progress bars) straight away, then poll
      setBatches((prev) => [b, ...prev.filter((x) => x.batch_id !== b.batch_id)]);
      setOpen((o) => ({ ...o, [b.batch_id]: true }));
      refresh();
    } catch (e: any) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  }

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Validation Runs</h1>
          <div className="sub">One run covers one or more objects — each is validated independently</div>
        </div>
      </div>

      <div className="content">
        {error && <div className="banner b-err">{error}</div>}
        {notice && <div className="banner b-ok">{notice}</div>}

        <section className="card" style={{ marginBottom: 16 }}>
          <div className="tabs">
            <button className={mode === "db" ? "tab on" : "tab"} onClick={() => setMode("db")}>
              Run from Database
            </button>
            <button className={mode === "upload" ? "tab on" : "tab"} onClick={() => setMode("upload")}>
              Upload Files
            </button>
          </div>

          {mode === "upload" ? (
            <>
              <p className="mini dim" style={{ marginBottom: 12 }}>
                One file per object. Only the object’s declared columns are staged — extra
                columns in the file are ignored, missing ones fail fast.
              </p>
              {slots.map((s, i) => (
                <div key={i} className="condrow">
                  <input type="file" accept=".csv"
                         onChange={(e) => setSlots(slots.map((x, j) => j === i ? { ...x, file: e.target.files?.[0] || null } : x))} />
                  <select value={s.entity}
                          onChange={(e) => setSlots(slots.map((x, j) => j === i ? { ...x, entity: e.target.value } : x))}>
                    {entities.map((en) => <option key={en.entity_name}>{en.entity_name}</option>)}
                  </select>
                  {slots.length > 1 && (
                    <button className="btn-mini no" onClick={() => setSlots(slots.filter((_, j) => j !== i))}>×</button>
                  )}
                </div>
              ))}
              <button className="btn-mini" style={{ marginTop: 8 }}
                      onClick={() => setSlots([...slots, { file: null, entity: entities[0]?.entity_name || "" }])}>
                + Add another file
              </button>
              <div className="form-actions">
                <button className="btn-primary" onClick={submitUpload} disabled={busy}>
                  {busy ? "Starting…" : "Run All"}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="fld" style={{ maxWidth: 260 }}>
                <label>Source</label>
                <select value={dbSource}
                        onChange={(e) => { setDbSource(e.target.value); setCheck(null); setDbSelected([]); }}>
                  {SOURCE_SYSTEMS.filter((s) => s !== "File Dump").map((s) => <option key={s}>{s}</option>)}
                </select>
              </div>
              <p className="mini dim" style={{ margin: "10px 0 12px" }}>
                Pick objects — the query is generated from the catalog. No connection details
                or SQL are entered here.
              </p>
              <div className="checkrow">
                <button className="btn-mini" disabled={checking} onClick={async () => {
                  setChecking(true); setCheck(null);
                  try { setCheck(await api.checkSource(dbSource)); }
                  catch (e: any) { setCheck({ ok: false, detail: String(e.message || e) }); }
                  finally { setChecking(false); }
                }}>
                  {checking ? "Checking…" : "Check connection"}
                </button>
                {check && (
                  <span className={`connpill ${check.ok ? "ok" : "bad"}`}>
                    <span className="dot" /> {check.detail}
                  </span>
                )}
                {!check && <span className="mini dim">Test the source before running.</span>}
              </div>
              {srcObjects === null && <p className="mini dim">Reading tables from {dbSource}…</p>}
              {srcObjects?.map((o) => {
                const disabled = o.status !== "runnable";
                const label = o.entity_name ?? o.table_name;
                return (
                  <label key={o.table_name} className={`entrow${disabled ? " off" : ""}`}>
                    {/* An undeclared table gets no checkbox at all -- there is
                        nothing to select, so offering a disabled box just
                        invites clicking it. The row stays visible so the table
                        is accounted for rather than mysteriously absent. */}
                    {o.status === "undeclared"
                      ? <span className="box-none" aria-hidden />
                      : <input type="checkbox" disabled={disabled}
                               checked={!!o.entity_name && dbSelected.includes(o.entity_name)}
                               onChange={(ev) => o.entity_name && setDbSelected(ev.target.checked
                                 ? [...dbSelected, o.entity_name]
                                 : dbSelected.filter((x) => x !== o.entity_name))} />}
                    <span className="en">{label}</span>
                    <span className="mini dim">
                      {o.status === "runnable"
                        && `${o.table_name} · ${o.element_count} elements · ${o.approved_rule_count} approved rule${o.approved_rule_count === 1 ? "" : "s"}`}
                      {o.status === "no_rules"
                        && `${o.table_name} · ${o.element_count} elements · no approved rules — nothing to run`}
                      {o.status === "undeclared"
                        && `${o.table_name} · this object is not part of our system`}
                    </span>
                  </label>
                );
              })}
              {srcObjects?.length === 0 && (
                <p className="mini dim">
                  {srcError ? `Could not read ${dbSource}: ${srcError}`
                            : `${dbSource} has no tables.`}
                </p>
              )}
              <div className="form-actions">
                <button className="btn-primary" onClick={submitDbRun}
                        disabled={busy || !dbSelected.length || !check?.ok}
                        title={!check?.ok ? "Check the connection first" : ""}>
                  {busy ? "Starting…" : "Run Selected"}
                </button>
              </div>
            </>
          )}
        </section>

        <section className="card">
          <div className="sec-head">
            <h2>Run History</h2>
            <span className="count">{batches.length} runs</span>
          </div>
          {batches.length === 0 && <p className="mini">No runs yet.</p>}
          {batches.map((b) => (
            <div key={b.batch_id} className="batch">
              <div className="bhead">
                <b>Run #{b.batch_id}</b>
                <span className={`badge ${batchBadge(b.status)}`}>{b.status.replace(/_/g, " ")}</span>
                <span className="mini dim">
                  {b.entity_count} object{b.entity_count === 1 ? "" : "s"} · {b.run_type.replace("_", " ")}
                  {b.triggered_by ? ` · ${b.triggered_by}` : ""}
                </span>
                <span className="spacer" />
                <span className="mini dim">{new Date(b.started_at).toLocaleString()}</span>
                <button className="btn-mini" onClick={() => setOpen({ ...open, [b.batch_id]: !open[b.batch_id] })}>
                  {open[b.batch_id] ? "Hide" : "Show more"}
                </button>
              </div>
              {(b.status === "running" || b.runs.some((r) => r.phase && r.phase !== "done")) && (
                <div className="prog-list">
                  {b.runs.map((r) => {
                    // staging is measured in rows, validating in rules -- show
                    // whichever phase this entity is actually in.
                    const staged = r.total_records
                      ? Math.min(100, Math.round((r.records_scanned / r.total_records) * 100))
                      : 0;
                    const validated = r.rules_total
                      ? Math.round((r.rules_done / r.rules_total) * 100)
                      : 0;
                    const done = r.status === "completed" || r.status === "failed";
                    const pct = done ? 100 : r.phase === "validating" ? validated : staged;
                    const label = done
                      ? (r.status === "failed" ? "failed" : "done")
                      : r.phase === "validating"
                        ? `validating · ${r.rules_done}/${r.rules_total} rules`
                        : r.phase === "staging"
                          ? `loading · ${r.records_scanned.toLocaleString()}${r.total_records ? " / " + r.total_records.toLocaleString() : ""} rows`
                          : "queued";
                    return (
                      <div key={r.run_id} className="prog-row">
                        <span className="pr-ent">{r.entity_name}</span>
                        <div className="pr-track">
                          <div className={`pr-fill${done ? (r.status === "failed" ? " bad" : " ok") : ""}`}
                               style={{ width: `${pct}%` }} />
                        </div>
                        <span className="pr-pct">{pct}%</span>
                        <span className="pr-lab">{label}</span>
                      </div>
                    );
                  })}
                </div>
              )}
              {open[b.batch_id] && b.runs.map((r) => (
                <div key={r.run_id} className="brow">
                  <span className="ent">{r.entity_name}</span>
                  <span className={`badge ${runBadge(r.status)}`}>{r.status}</span>
                  <span className="mini dim">
                    {r.records_scanned.toLocaleString()} records · {r.rules_executed} rules
                  </span>
                  {r.error_message && <span className="err mini">{r.error_message}</span>}
                </div>
              ))}
            </div>
          ))}
        </section>
      </div>
    </>
  );
}

function batchBadge(s: string) {
  return ({
    completed: "b-good", running: "b-acc", completed_with_errors: "b-warn", empty: "b-acc",
  } as Record<string, string>)[s] || "b-acc";
}

function runBadge(s: string) {
  return ({
    completed: "b-good", running: "b-acc", pending: "b-acc", failed: "b-crit",
  } as Record<string, string>)[s] || "b-acc";
}
