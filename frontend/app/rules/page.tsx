"use client";

import { useEffect, useState } from "react";
import { api, Entity, Role, Rule, RuleType, getActor, getRole } from "@/lib/api";

interface Condition { field: string; operator: string; value: string }

const OPERATORS = [
  { code: "=", label: "equals" },
  { code: "!=", label: "not equals" },
  { code: "in", label: "is one of" },
  { code: "is_null", label: "is empty" },
  { code: "is_not_null", label: "is not empty" },
];

/** One worked example per rule type, shown for the CURRENT selection only. */
interface Guide { plain: string; example: string; sql: string }

const RULE_GUIDE: Record<string, Guide> = {
  required: {
    plain: "The field must not be empty. Any blank value is a violation.",
    example: "Every Account must have a Name.",
    sql: "WHERE Name IS NULL OR TRIM(Name) = ''",
  },
  allowed_values: {
    plain: "The value must be one of a fixed list you provide.",
    example: "BillingCountry may only be USA, India or UK.",
    sql: "WHERE BillingCountry NOT IN ('USA','India','UK')",
  },
  format_pattern: {
    plain: "The value must match a pattern (regex). Used for emails, URLs, codes.",
    example: "Website must start with http:// or https://",
    sql: "WHERE NOT REGEXP('^https?://', Website)",
  },
  max_length: {
    plain: "The value must not be longer than the limit you set.",
    example: "BillingPostalCode must be 20 characters or fewer.",
    sql: "WHERE LENGTH(BillingPostalCode) > 20",
  },
  unique: {
    plain:
      "No two records may share the same value. This is the only rule type that has to compare rows against each other — it runs as GROUP BY / HAVING.",
    example: "Two Accounts must not have the same Name.",
    sql: "GROUP BY Name HAVING COUNT(*) > 1",
  },
  conditional_required: {
    plain: "A field becomes mandatory only when another field has a specific value.",
    example: "If Type is 'Owner/Operator', then Phone must be filled in.",
    sql: "WHERE Type = 'Owner/Operator' AND (Phone IS NULL OR TRIM(Phone) = '')",
  },
  ref_integrity: {
    plain:
      "The value must already exist in another entity's field — the classic foreign-key check. It only sees values captured the last time that entity was validated, so run the referenced entity first.",
    example: "Every Account.BillingCountry must already appear in Contact.MailingCountry.",
    sql: "WHERE BillingCountry NOT IN (SELECT value FROM val_reference_values WHERE …)",
  },
  multi_condition: {
    plain:
      "Chain several conditions on the SAME record with AND/OR, then either require a field or just flag the record.",
    example: "If Type = 'Owner/Operator' AND BillingCountry = 'USA', then BillingCity must be filled in.",
    sql: "WHERE (Type='Owner/Operator' AND BillingCountry='USA') AND (BillingCity IS NULL OR …)",
  },
};

export default function RulesPage() {
  const [entities, setEntities] = useState<Entity[]>([]);
  const [ruleTypes, setRuleTypes] = useState<RuleType[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // form
  const [entityName, setEntityName] = useState("");
  const [fieldName, setFieldName] = useState("");
  const [ruleType, setRuleType] = useState("required");
  const [severity, setSeverity] = useState("Warning");
  const [configText, setConfigText] = useState("");
  const [actor, setActorLocal] = useState("prabhat");
  const [role, setRoleLocal] = useState<Role>("admin");
  const [filterEntity, setFilterEntity] = useState("");
  const [filterStatus, setFilterStatus] = useState("");

  // ref-integrity
  const [refEntity, setRefEntity] = useState("");
  const [refField, setRefField] = useState("");

  // multi-condition
  const [conditions, setConditions] = useState<Condition[]>([{ field: "", operator: "=", value: "" }]);
  const [logic, setLogic] = useState<"AND" | "OR">("AND");
  const [thenType, setThenType] = useState<"require" | "flag">("require");
  const [thenField, setThenField] = useState("");

  // optional scope filter (available on EVERY rule type)
  const [useFilter, setUseFilter] = useState(false);
  const [filterConds, setFilterConds] = useState<Condition[]>([{ field: "", operator: "=", value: "" }]);
  const [filterLogic, setFilterLogic] = useState<"AND" | "OR">("AND");

  const isMulti = ruleType === "multi_condition";
  const isRef = ruleType === "ref_integrity";

  const entity = entities.find((e) => e.entity_name === entityName);
  const columns = entity?.columns || [];
  const refColumns = entities.find((e) => e.entity_name === refEntity)?.columns || [];

  useEffect(() => { setActorLocal(getActor()); setRoleLocal(getRole()); }, []);

  useEffect(() => {
    Promise.all([api.entities(), api.ruleTypes(), api.rules()])
      .then(([ents, types, rs]) => {
        setEntities(ents);
        setRuleTypes(types);
        setRules(rs);
        if (ents.length) {
          setEntityName(ents[0].entity_name);
          setRefEntity(ents[0].entity_name);
        }
      })
      .catch((e) => setError(String(e.message || e)));
  }, []);

  useEffect(() => { setFieldName(""); }, [entityName]);
  useEffect(() => { setRefField(""); }, [refEntity]);

  function refresh() { api.rules().then(setRules).catch(() => {}); }

  function buildDefinition(): any {
    const def: any = {};
    if (ruleType === "allowed_values") {
      def.values = configText.split(",").map((s) => s.trim()).filter(Boolean);
    } else if (ruleType === "format_pattern") {
      def.pattern = configText.trim();
    } else if (ruleType === "max_length") {
      def.max_length = parseInt(configText, 10);
    } else if (ruleType === "conditional_required") {
      const [f, v] = configText.split(":");
      def.if_field = (f || "").trim();
      def.if_value = (v || "").trim();
    } else if (isRef) {
      def.ref_entity_name = refEntity;
      def.ref_field_name = refField;
    } else if (isMulti) {
      def.conditions = conditions.filter((c) => c.field);
      def.logic = logic;
      def.then = thenType === "require" ? { type: "require", field: thenField } : { type: "flag" };
    }
    if (useFilter) {
      const conds = filterConds.filter((c) => c.field);
      if (conds.length) def.filter = { conditions: conds, logic: filterLogic };
    }
    return def;
  }

  async function createRule() {
    setError(null); setNotice(null);
    const effectiveField = isMulti
      ? (thenType === "require" ? thenField : conditions[0]?.field)
      : fieldName;
    if (!entityName || !effectiveField) {
      setError("Pick an entity and a field first.");
      return;
    }
    try {
      const r = await api.createRule({
        entity_name: entityName,
        field_name: effectiveField,
        rule_type: ruleType,
        severity,
        rule_definition: buildDefinition(),
        created_by: actor,
      });
      setNotice(`Created rule #${r.rule_id} as a draft.`);
      setConfigText("");
      refresh();
    } catch (e: any) { setError(String(e.message || e)); }
  }

  async function transition(ruleId: number, action: "submit" | "approve" | "reject") {
    setError(null); setNotice(null);
    try {
      await api.transitionRule(ruleId, action, actor);
      refresh();
    } catch (e: any) {
      setError(String(e.message || e));
      window.scrollTo({ top: 0, behavior: "smooth" });   // the banner is up top
    }
  }

  const shown = rules.filter(
    (r) => (!filterEntity || r.entity_name === filterEntity) &&
           (!filterStatus || r.status === filterStatus)
  );
  const isAdmin = role === "admin";
  const guide = RULE_GUIDE[ruleType];

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Manage Rules</h1>
          <div className="sub">Author, review and approve validation rules — no code, no deployment</div>
        </div>
        <div className="spacer" />
        <div className="flt">
          <span className="fl">Acting as</span>
          <span className="idchip">{actor} <b>{role}</b></span>
        </div>
      </div>

      <div className="content">
        {error && <div className="banner b-err">{error}</div>}
        {notice && <div className="banner b-ok">{notice}</div>}

        <section className="card" style={{ marginBottom: 16 }}>
          <h2>New Rule</h2>

          <div className="grid2">
            <Field label="Entity">
              <select value={entityName} onChange={(e) => setEntityName(e.target.value)}>
                {entities.map((e) => <option key={e.entity_name}>{e.entity_name}</option>)}
              </select>
            </Field>

            {!isMulti && (
              <Field label="Field">
                <select value={fieldName} onChange={(e) => setFieldName(e.target.value)}>
                  <option value="">Select a field…</option>
                  {columns.map((c) => <option key={c}>{c}</option>)}
                </select>
              </Field>
            )}

            <Field label="Rule Type">
              <select value={ruleType} onChange={(e) => setRuleType(e.target.value)}>
                {ruleTypes.map((t) => <option key={t.code} value={t.code}>{labelFor(t.code)}</option>)}
              </select>
            </Field>

            <Field label="Severity">
              <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
                <option>Critical</option>
                <option>Warning</option>
              </select>
            </Field>

            {["allowed_values", "format_pattern", "max_length", "conditional_required"].includes(ruleType) && (
              <Field label={configLabel(ruleType)}>
                <input className="txt" value={configText} placeholder={configPlaceholder(ruleType)}
                       onChange={(e) => setConfigText(e.target.value)} />
              </Field>
            )}
          </div>

          {isRef && (
            <div className="subpanel accent">
              <p className="mini">
                The <b>Field</b> above is the column on <b>{entityName}</b> being checked.
                Below, choose which entity/field it must exist in.
              </p>
              <div className="grid2">
                <Field label="Reference Entity">
                  <select value={refEntity} onChange={(e) => setRefEntity(e.target.value)}>
                    {entities.map((e) => <option key={e.entity_name}>{e.entity_name}</option>)}
                  </select>
                </Field>
                <Field label="Reference Field">
                  <select value={refField} onChange={(e) => setRefField(e.target.value)}>
                    <option value="">Select a field…</option>
                    {refColumns.map((c) => <option key={c}>{c}</option>)}
                  </select>
                </Field>
              </div>
              <p className="mini dim">
                Only has values to check against once the reference entity has been validated at least once.
              </p>
            </div>
          )}

          {isMulti && (
            <div className="subpanel violet">
              <ConditionList
                title="If the record matches"
                conds={conditions} setConds={setConditions}
                logic={logic} setLogic={setLogic} columns={columns}
              />
              <div className="thenrow" style={{ marginTop: 12 }}>
                <span className="lbl">THEN</span>
                <label className="radio">
                  <input type="radio" checked={thenType === "require"} onChange={() => setThenType("require")} />
                  Require a field
                </label>
                <label className="radio">
                  <input type="radio" checked={thenType === "flag"} onChange={() => setThenType("flag")} />
                  Just flag the record
                </label>
                {thenType === "require" && (
                  <select value={thenField} onChange={(e) => setThenField(e.target.value)} style={{ width: 190 }}>
                    <option value="">Select a field…</option>
                    {columns.map((c) => <option key={c}>{c}</option>)}
                  </select>
                )}
              </div>
            </div>
          )}

          {/* Optional scope filter -- available on EVERY rule type, including
              Required and Unique, which previously had no configuration. */}
          <div className="subpanel plain">
            <label className="radio" style={{ fontWeight: 700 }}>
              <input type="checkbox" checked={useFilter} onChange={(e) => setUseFilter(e.target.checked)} />
              Only check some records (optional scope filter)
            </label>
            {useFilter && (
              <div style={{ marginTop: 12 }}>
                <ConditionList
                  title="Only where"
                  conds={filterConds} setConds={setFilterConds}
                  logic={filterLogic} setLogic={setFilterLogic} columns={columns}
                />
              </div>
            )}
          </div>

          <div className="form-actions">
            <button onClick={createRule} className="btn-primary">Save Draft</button>
          </div>
        </section>

        {guide && (
          <section className="card guide-card" style={{ marginBottom: 16 }}>
            <div className="guide-head">
              <span className="guide-tag">{labelFor(ruleType)}</span>
              <h2 style={{ margin: 0 }}>How this rule type works</h2>
            </div>
            <p className="guide-plain">{guide.plain}</p>
            <div className="guide-row"><span className="guide-lbl">Example</span><span className="guide-val">{guide.example}</span></div>
            <div className="guide-row"><span className="guide-lbl">Becomes</span><code className="guide-sql">{guide.sql}</code></div>
          </section>
        )}

        <section className="card">
          <div className="sec-head">
            <h2>All Rules</h2>
            <span className="count">{shown.length} of {rules.length}</span>
            <span className="spacer" />
            <select className="sm" value={filterEntity} onChange={(e) => setFilterEntity(e.target.value)}>
              <option value="">All objects</option>
              {entities.map((e) => <option key={e.entity_name}>{e.entity_name}</option>)}
            </select>
            <select className="sm" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
              <option value="">All statuses</option>
              <option value="draft">Draft</option>
              <option value="submitted">Submitted</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
            </select>
          </div>
          <table className="cde">
            <thead>
              <tr>
                <th>Rule ID</th><th>Entity</th><th>Field</th><th>Type</th>
                <th>Exec</th><th>Severity</th><th>Status</th><th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.rule_id}>
                  <td className="el mono">{r.rule_id}</td>
                  <td>{r.entity_name}</td>
                  <td>{r.field_name}</td>
                  <td>{labelFor(r.rule_type)}</td>
                  <td><span className={`badge ${r.execution_type === "RECORD" ? "b-violet" : "b-acc"}`}>{r.execution_type}</span></td>
                  <td><span className={`badge ${r.severity === "Critical" ? "b-crit" : "b-warn"}`}>{r.severity}</span></td>
                  <td>
                    <span className={`badge ${statusBadge(r.status)}`}>{r.status}</span>
                    {r.approved_by && <div className="mini dim">by {r.approved_by}</div>}
                  </td>
                  <td className="acts"><div className="btns">
                    {r.status === "draft" && <button className="btn-mini" onClick={() => transition(r.rule_id, "submit")}>Submit</button>}
                    {r.status === "submitted" && (isAdmin ? (
                      <>
                        <button className="btn-mini ok" onClick={() => transition(r.rule_id, "approve")}>Approve</button>
                        <button className="btn-mini no" onClick={() => transition(r.rule_id, "reject")}>Reject</button>
                      </>
                    ) : (
                      <span className="mini dim">admin only</span>
                    ))}
                    {r.status === "rejected" && <button className="btn-mini" onClick={() => transition(r.rule_id, "submit")}>Resubmit</button>}
                  </div></td>
                </tr>
              ))}
              {shown.length === 0 && (
                <tr><td colSpan={8} className="mini">No rules yet — create one above.</td></tr>
              )}
            </tbody>
          </table>
          <p className="mini dim" style={{ marginTop: 10 }}>
            Only <b>approved</b> rules are executed. Approving needs the <b>admin</b> role.
            Separation of duties (an author may not approve their own rule) is off in
            development — set <code>REQUIRE_SEPARATE_APPROVER=true</code> on the server to
            enforce it in production.
          </p>
        </section>
      </div>
    </>
  );
}

function ConditionList({ title, conds, setConds, logic, setLogic, columns }: any) {
  return (
    <>
      <div className="matchrow">
        <span className="lbl">{title}</span>
        <div className="seg">
          <button type="button" className={logic === "AND" ? "on" : ""} onClick={() => setLogic("AND")}>
            Match all
          </button>
          <button type="button" className={logic === "OR" ? "on" : ""} onClick={() => setLogic("OR")}>
            Match any
          </button>
        </div>
      </div>
      {conds.map((c: Condition, i: number) => (
        <div key={i} className="condrow">
          <select value={c.field} onChange={(e) => setConds(conds.map((x: Condition, j: number) => j === i ? { ...x, field: e.target.value } : x))}>
            <option value="">Field…</option>
            {columns.map((col: string) => <option key={col}>{col}</option>)}
          </select>
          <select value={c.operator} onChange={(e) => setConds(conds.map((x: Condition, j: number) => j === i ? { ...x, operator: e.target.value } : x))}>
            {OPERATORS.map((o) => <option key={o.code} value={o.code}>{o.label}</option>)}
          </select>
          {!["is_null", "is_not_null"].includes(c.operator) && (
            <input className="txt" value={c.value} placeholder="value"
                   onChange={(e) => setConds(conds.map((x: Condition, j: number) => j === i ? { ...x, value: e.target.value } : x))} />
          )}
          {conds.length > 1 && (
            <button className="btn-mini no" onClick={() => setConds(conds.filter((_: any, j: number) => j !== i))}>×</button>
          )}
        </div>
      ))}
      <button className="btn-mini" style={{ marginTop: 8 }}
              onClick={() => setConds([...conds, { field: "", operator: "=", value: "" }])}>
        + Add condition
      </button>
    </>
  );
}

function Field({ label, children }: { label: string; children: any }) {
  return <div className="fld"><label>{label}</label>{children}</div>;
}

function labelFor(code: string) {
  return ({
    required: "Required", allowed_values: "Allowed Values", format_pattern: "Format Pattern",
    max_length: "Max Length", unique: "Unique", conditional_required: "Conditional Required",
    ref_integrity: "Referential Integrity", multi_condition: "Multi-Condition (Advanced)",
  } as Record<string, string>)[code] || code;
}

function configLabel(rt: string) {
  return ({
    allowed_values: "Allowed Values (comma separated)", format_pattern: "Pattern (regex)",
    max_length: "Max Length", conditional_required: "If Field : If Value",
  } as Record<string, string>)[rt] || "Configuration";
}

function configPlaceholder(rt: string) {
  return ({
    allowed_values: "USA, India, UK", format_pattern: "^https?://",
    max_length: "20", conditional_required: "Type:Owner/Operator",
  } as Record<string, string>)[rt] || "";
}

function statusBadge(s: string) {
  return ({ approved: "b-good", submitted: "b-warn", rejected: "b-crit", draft: "b-acc" } as Record<string, string>)[s] || "b-acc";
}
