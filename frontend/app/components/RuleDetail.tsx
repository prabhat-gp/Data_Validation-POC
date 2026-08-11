"use client";

import React, { useState } from "react";
import { Rule, ViolationQuery, api } from "@/lib/api";

/** Result of this rule in the run being viewed -- only passed from the drilldown. */
export interface RuleRunResult {
  entity_name: string;
  element_name: string;
  dimension: string;
  records_checked: number;
  records_failed: number;
  score_pct: number;
}

/**
 * Full rule detail, shared by the Rules page and the dashboard drilldown.
 *
 * A single table row cannot express a multi-field UNIQUENESS or an AGGREGATION
 * config, so the definition is decoded into plain language here, with the raw
 * JSON underneath for anyone who wants it. When opened from the drilldown it
 * also shows what this rule actually did in that run -- the number the user
 * just clicked on.
 */
export default function RuleDetail({
  rule, result, onClose,
}: { rule: Rule; result?: RuleRunResult; onClose: () => void }) {
  let d: any = {};
  try { d = JSON.parse(rule.rule_definition || "{}"); } catch {}

  const rows: [string, any][] = [];
  const t = rule.rule_type;
  if (t === "VALIDITY") rows.push(["Pattern", <code key="p">{d.pattern}</code>]);
  if (t === "RANGE") {
    rows.push(["Allowed range", `${d.min ?? "−∞"} to ${d.max ?? "∞"}`]);
    rows.push(["Non-numeric", d.onNonNumeric === "flag" ? "flagged as a violation" : "skipped"]);
  }
  if (t === "UNIQUENESS")
    rows.push(["Unique on", (d.fields?.length ? d.fields : [rule.field_name]).join("  +  ")]);
  if (t === "REFERENTIAL_INTEGRITY")
    rows.push(["Must exist in", `${d.lookupTable} . ${d.lookupField}`]);
  if (t === "AGGREGATION") {
    rows.push(["Measure", `${d.aggregateFunction}(${d.aggregateField})`]);
    rows.push(["Grouped by", (d.groupBy || []).join("  +  ")]);
    rows.push(["Flags when", `${d.aggregateFunction} ${d.operator} ${d.threshold}`]);
  }
  if (t === "ALLOWED_VALUES") rows.push(["Allowed", (d.allowedValues || []).join(", ")]);
  if (t === "CROSS_FIELD_SIMPLE" || t === "CUSTOM_SQL")
    rows.push(["Expression", <code key="e">{d.expression}</code>]);
  if (d.filter?.conditions?.length)
    rows.push(["Only where",
      d.filter.conditions.map((c: any) => `${c.field} ${c.operator} ${c.value ?? ""}`)
        .join(`  ${d.filter.logic}  `)]);

  const tone = (s: number) => (s > 90 ? "good" : s >= 80 ? "warn" : "crit");

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="rulecard" onClick={(e) => e.stopPropagation()}>
        <div className="rc-head">
          <span className="rc-id">#{rule.rule_id}</span>
          <b>{rule.rule_name}</b>
          <span className={`badge ${statusBadge(rule.status)}`}>{rule.status}</span>
          <span className={`badge ${sevBadge(rule.severity)}`}>{rule.severity}</span>
          <button className="rc-x" onClick={onClose} title="Close">×</button>
        </div>

        {/* what this rule DID in the run the user is looking at */}
        {result && (
          <div className="rc-result">
            <div className="rcr-head">In this run</div>
            <div className="rcr-stats">
              <div><span className={`v tone-${tone(result.score_pct)}`}>{result.score_pct}%</span><span className="l">Score</span></div>
              <div><span className="v tone-crit">{result.records_failed.toLocaleString()}</span><span className="l">Failed</span></div>
              <div><span className="v">{result.records_checked.toLocaleString()}</span><span className="l">Checked</span></div>
            </div>
          </div>
        )}

        <div className="rc-grid">
          <span>Object</span><b>{rule.entity_name}</b>
          <span>Element</span><b>{rule.field_name || "— multi-element —"}</b>
          <span>Rule Type</span><b>{rule.rule_type.replace(/_/g, " ")}</b>
          {/* Read-only. Dimension is decided by the backend from rule_type, so
              it is shown next to the type rather than offered as a choice. */}
          <span>Dimension</span>
          <b>
            {rule.dimension || result?.dimension || "—"}
            <span className="rc-derived">set by rule type</span>
          </b>
          <span>Key</span><b>{rule.primary_key_field}</b>
          {rows.map(([k, v], i) => (
            <React.Fragment key={i}><span>{k}</span><b>{v}</b></React.Fragment>
          ))}
        </div>

        <div className="rc-foot">
          <div>Created by <b>{rule.created_by}</b> · {new Date(rule.created_date).toLocaleDateString()}</div>
          {rule.approved_by && <div>Approved by <b>{rule.approved_by}</b></div>}
          {rule.error_message && <div>Message: “{rule.error_message}”</div>}
        </div>

        {/* Only from the dashboard drilldown. On the Rules page there is no
            run behind the rule, so "the failing rows" would have no meaning --
            the rule may never have executed. `result` is the run context. */}
        {result && (
          <ViolationQueryBlock ruleId={rule.rule_id} failed={result.records_failed} />
        )}

        <details className="rc-json">
          <summary>Raw definition</summary>
          <pre>{JSON.stringify(d, null, 2)}</pre>
        </details>
      </div>
    </div>
  );
}

export function sevBadge(s: string) {
  return ({ CRITICAL: "b-crit", ERROR: "b-crit", WARNING: "b-warn", INFO: "b-acc" } as any)[s] || "b-acc";
}
export function statusBadge(s: string) {
  return ({
    APPROVED: "b-good", PENDING: "b-warn", DRAFT: "b-acc",
    UPDATED: "b-violet", REJECTED: "b-crit", RETIRED: "b-crit",
  } as any)[s] || "b-acc";
}


/**
 * "Show me the rows" -- SQL the user can paste into their own client.
 *
 * Generated against the SOURCE tables, not the staging tables the engine runs
 * on, so it is actually runnable where their data lives. Row count is capped
 * at 10: this is for eyeballing what went wrong, not for extracting a
 * remediation list.
 *
 * Fetched on expand rather than with the rule -- most people open a rule to
 * read its config, not to run SQL, and this keeps the modal instant.
 */
function ViolationQueryBlock({ ruleId, failed }: { ruleId: number; failed: number }) {
  const [q, setQ] = useState<ViolationQuery | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  function load(open: boolean) {
    if (!open || q || err) return;
    api.violationQuery(ruleId, 10)
      .then(setQ)
      .catch((e) => setErr(String(e.message || e)));
  }

  async function copy() {
    if (!q) return;
    try {
      await navigator.clipboard.writeText(q.sql);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* clipboard blocked -- the text is selectable anyway */ }
  }

  if (failed === 0) return null;      // nothing failed, nothing to look at

  return (
    <details className="rc-json vq" onToggle={(e) => load((e.target as HTMLDetailsElement).open)}>
      <summary>Show the failing rows &nbsp;<span className="vq-hint">SQL for your database</span></summary>
      {err && <p className="mini" style={{ color: "var(--crit)" }}>{err}</p>}
      {!q && !err && <p className="mini">Building query…</p>}
      {q && (
        <>
          <div className="vq-head">
            <span className="mini">{q.note}</span>
            <button className="btn-mini" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
          </div>
          <pre className="vq-sql">{q.sql}</pre>
          <p className="mini vq-foot">
            Runs against <b>{q.table}</b> in your <b>source</b> database. Capped at 10 rows —
            remove the LIMIT to see all{typeof failed === "number" ? ` ${failed.toLocaleString()}` : ""}.
          </p>
        </>
      )}
    </details>
  );
}