"use client";

import React, { useEffect, useState } from "react";
import {
  Entity, Role, Rule, RuleType, SEVERITIES, STATUSES, api, getActor, getRole,
} from "@/lib/api";
import RuleDetail, { sevBadge, statusBadge } from "@/app/components/RuleDetail";

/** Plain-English guide + the SQL each type becomes, shown for the current selection only. */
const GUIDE: Record<string, { plain: string; example: string; sql: string }> = {
  COMPLETENESS: {
    plain: "The element must not be empty. Any blank or NULL value is a violation.",
    example: "Every Account must have a Name.",
    sql: "WHERE Name IS NULL OR TRIM(Name) = ''",
  },
  VALIDITY: {
    plain: "The value must match a pattern (regex). Used for emails, URLs and codes.",
    example: "Website must start with http:// or https://",
    sql: "WHERE NOT REGEXP('^https?://', Website)",
  },
  RANGE: {
    plain:
      "A numeric value must fall between a minimum and maximum. Non-numeric text is skipped by default — that is a Validity problem, not a Range one.",
    example: "ORDER_AMOUNT must be between 0 and 100000.",
    sql: "WHERE CAST(ORDER_AMOUNT AS REAL) < 0 OR CAST(ORDER_AMOUNT AS REAL) > 100000",
  },
  UNIQUENESS: {
    plain:
      "No two records may share the same value. Leave Element empty and list several elements to check a combination. Runs as one query — GROUP BY/HAVING in a subquery joined back, so BOTH sides of a duplicate are reported.",
    example: "No two Customers may share FIRST_NAME + LAST_NAME + DOB.",
    sql: "JOIN (SELECT x FROM t GROUP BY x HAVING COUNT(*) > 1) D ON t.x = D.x",
  },
  REFERENTIAL_INTEGRITY: {
    plain:
      "The value must exist in another object — the classic foreign-key check. The lookup object must be included in the same run so the join has data.",
    example: "Every ORDERS.PART_NUMBER must exist in Part Master.",
    sql: "LEFT JOIN PART_MASTER P ON O.PART_NUMBER = P.PART_NUMBER WHERE P.PART_NUMBER IS NULL",
  },
  AGGREGATION: {
    plain:
      "Group the records and test a measure per group. The violation is a GROUP, not a single record, so the score is groups-passing over groups-checked.",
    example: "Flag any customer with more than 3 orders.",
    sql: "GROUP BY CUSTOMER_ID HAVING COUNT(*) > 3",
  },
  ALLOWED_VALUES: {
    plain: "The value must be one of a fixed list you provide.",
    example: "COUNTRY may only be US, IN, CA or UK.",
    sql: "WHERE COUNTRY NOT IN ('US','IN','CA','UK')",
  },
  CROSS_FIELD_SIMPLE: {
    plain:
      "A condition across several elements of the SAME record. Only this object's own columns may be used.",
    example: "If COUNTRY = 'US' then STATE must not be empty.",
    sql: "WHERE COUNTRY='US' AND STATE IS NULL",
  },
  CUSTOM_SQL: {
    plain:
      "A custom expression over this object's columns. Statements, comments and DDL/DML are rejected — it is not an arbitrary-SQL backdoor.",
    example: "Flag phone numbers shorter than 7 characters.",
    sql: "WHERE LENGTH(TRIM(PHONE)) < 7",
  },
};

const AGG_FUNCS = ["COUNT", "SUM", "AVG", "MIN", "MAX"];
const OPS = [">", ">=", "<", "<=", "=", "!="];

export default function RulesPage() {
  const [entities, setEntities] = useState<Entity[]>([]);
  const [ruleTypes, setRuleTypes] = useState<RuleType[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [actor, setActorLocal] = useState("prabhat");
  const [role, setRoleLocal] = useState<Role>("admin");
  const [fEntity, setFEntity] = useState("");
  const [fStatus, setFStatus] = useState("");

  // form
  const [entityName, setEntityName] = useState("");
  const [fieldName, setFieldName] = useState("");
  const [ruleType, setRuleType] = useState("COMPLETENESS");
  const [severity, setSeverity] = useState("WARNING");
  const [ruleName, setRuleName] = useState("");
  const [errMsg, setErrMsg] = useState("");

  // per-type config
  const [pattern, setPattern] = useState("");
  const [minV, setMinV] = useState("");
  const [maxV, setMaxV] = useState("");
  const [flagBadNum, setFlagBadNum] = useState(false);
  const [multiFields, setMultiFields] = useState<string[]>([]);
  const [lookupEntity, setLookupEntity] = useState("");
  const [lookupField, setLookupField] = useState("");
  const [aggFn, setAggFn] = useState("COUNT");
  const [aggField, setAggField] = useState("*");
  const [groupBy, setGroupBy] = useState<string[]>([]);
  const [aggOp, setAggOp] = useState(">");
  const [threshold, setThreshold] = useState("");
  const [allowed, setAllowed] = useState("");
  const [expression, setExpression] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [menuFor, setMenuFor] = useState<number | null>(null);
  const [detail, setDetail] = useState<Rule | null>(null);

  const entity = entities.find((e) => e.entity_name === entityName);
  const columns = entity?.columns || [];
  const lookupEnt = entities.find((e) => e.entity_name === lookupEntity);
  const lookupChoices = lookupEnt
    ? [lookupEnt.primary_key_field, ...lookupEnt.columns]
    : [];

  useEffect(() => { setActorLocal(getActor()); setRoleLocal(getRole()); }, []);

  useEffect(() => {
    Promise.all([api.entities(), api.ruleTypes(), api.rules()])
      .then(([ents, types, rs]) => {
        setEntities(ents); setRuleTypes(types); setRules(rs);
        if (ents.length) { setEntityName(ents[0].entity_name); setLookupEntity(ents[0].entity_name); }
      })
      .catch((e) => setError(String(e.message || e)));
  }, []);

  useEffect(() => { setFieldName(""); setMultiFields([]); setGroupBy([]); }, [entityName]);
  useEffect(() => { setLookupField(""); }, [lookupEntity]);

  const refresh = () => api.rules().then(setRules).catch(() => {});

  function definition(): any {
    switch (ruleType) {
      case "VALIDITY":  return { pattern };
      case "RANGE": {
        const d: any = {};
        if (minV !== "") d.min = Number(minV);
        if (maxV !== "") d.max = Number(maxV);
        if (flagBadNum) d.onNonNumeric = "flag";
        return d;
      }
      case "UNIQUENESS": return multiFields.length > 1 ? { fields: multiFields } : {};
      case "REFERENTIAL_INTEGRITY": return { lookupTable: lookupEntity, lookupField };
      case "AGGREGATION": return {
        aggregateFunction: aggFn, aggregateField: aggField,
        groupBy, operator: aggOp, threshold: Number(threshold),
      };
      case "ALLOWED_VALUES": return {
        allowedValues: allowed.split(",").map((v) => v.trim()).filter(Boolean),
      };
      case "CROSS_FIELD_SIMPLE":
      case "CUSTOM_SQL": return { expression };
      default: return {};
    }
  }

  function loadForEdit(r: Rule) {
    const d = JSON.parse(r.rule_definition || "{}");
    setEditing(r.rule_id);
    setEntityName(r.entity_name); setRuleType(r.rule_type);
    setFieldName(r.field_name || ""); setSeverity(r.severity);
    setRuleName(r.rule_name || ""); setErrMsg(r.error_message || "");
    setPattern(d.pattern || ""); setMinV(d.min ?? ""); setMaxV(d.max ?? "");
    setFlagBadNum(d.onNonNumeric === "flag");
    setMultiFields(d.fields || []);
    setLookupEntity(d.lookupTable || ""); setLookupField(d.lookupField || "");
    setAggFn(d.aggregateFunction || "COUNT"); setAggField(d.aggregateField || "*");
    setGroupBy(d.groupBy || []); setAggOp(d.operator || ">");
    setThreshold(d.threshold ?? "");
    setAllowed((d.allowedValues || []).join(", "));
    setExpression(d.expression || "");
    setMenuFor(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function cancelEdit() { setEditing(null); clearForm(); }

  /** Reset every field the form collects, back to defaults. */
  function clearForm() {
    setFieldName(""); setRuleType("COMPLETENESS"); setSeverity("WARNING");
    setRuleName(""); setErrMsg("");
    setPattern(""); setMinV(""); setMaxV(""); setFlagBadNum(false);
    setMultiFields([]); setLookupField(""); setGroupBy([]);
    setAggFn("COUNT"); setAggField("*"); setAggOp(">"); setThreshold("");
    setAllowed(""); setExpression("");
    setError(null); setNotice(null);
  }

  async function create() {
    setError(null); setNotice(null);
    const effField = ruleType === "UNIQUENESS" && multiFields.length > 1 ? "" : fieldName;
    try {
      const body = {
        entity_name: entityName, field_name: effField, rule_type: ruleType,
        severity, rule_name: ruleName || undefined,
        error_message: errMsg || undefined,
        rule_definition: definition(), created_by: actor,
      };
      if (editing) {
        const r = await api.updateRule(editing, body);
        setNotice(`Rule #${r.rule_id} saved — status is now ${r.status}.`);
        setEditing(null);
      } else {
        const r = await api.createRule(body);
        setNotice(`Created rule #${r.rule_id} as DRAFT.`);
      }
      refresh();
    } catch (e: any) {
      setError(String(e.message || e));
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  async function act(id: number, what: string) {
    setError(null); setNotice(null);
    try {
      if (what === "retire") await api.retireRule(id, actor);
      else if (what === "reactivate") await api.reactivateRule(id, actor);
      else await api.transitionRule(id, what as any, actor);
      refresh();
    } catch (e: any) {
      setError(String(e.message || e));
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  const shown = rules.filter(
    (r) => (!fEntity || r.entity_name === fEntity) && (!fStatus || r.status === fStatus)
  );
  const isAdmin = role === "admin";
  const guide = GUIDE[ruleType];
  const needsField = !(ruleType === "UNIQUENESS" && multiFields.length > 1);

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
          <div className="sec-head">
            <h2>{editing ? `Edit Rule #${editing}` : "New Rule"}</h2>
            {editing && <span className="count">editing</span>}
            <span className="spacer" />
            <button className="chip-link" onClick={editing ? cancelEdit : clearForm}>
              {editing ? "Cancel edit" : "Clear"}
            </button>
          </div>
          <div className="grid2">
            <Fld label="Object">
              <select value={entityName} onChange={(e) => setEntityName(e.target.value)}>
                {entities.map((e) => <option key={e.entity_name}>{e.entity_name}</option>)}
              </select>
            </Fld>
            <Fld label="Rule Type">
              <select value={ruleType} onChange={(e) => setRuleType(e.target.value)}>
                {ruleTypes.map((t) => <option key={t.code} value={t.code}>{t.code.replace(/_/g, " ")}</option>)}
              </select>
            </Fld>
            {needsField && (
              <Fld label="Element">
                <select value={fieldName} onChange={(e) => setFieldName(e.target.value)}>
                  <option value="">Select an element…</option>
                  {columns.map((c) => <option key={c}>{c}</option>)}
                </select>
              </Fld>
            )}
            <Fld label="Severity">
              <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
                {SEVERITIES.map((s) => <option key={s}>{s}</option>)}
              </select>
            </Fld>
          </div>

          {/* ---- per-type configuration ---- */}
          {ruleType === "VALIDITY" && (
            <Cfg><Fld label="Pattern (regex)">
              <input className="txt" value={pattern} placeholder="^https?://"
                     onChange={(e) => setPattern(e.target.value)} />
            </Fld></Cfg>
          )}

          {ruleType === "RANGE" && (
            <Cfg>
              <div className="grid2">
                <Fld label="Minimum"><input className="txt" value={minV} placeholder="0"
                     onChange={(e) => setMinV(e.target.value)} /></Fld>
                <Fld label="Maximum"><input className="txt" value={maxV} placeholder="100000"
                     onChange={(e) => setMaxV(e.target.value)} /></Fld>
              </div>
              <label className="radio" style={{ marginTop: 10 }}>
                <input type="checkbox" checked={flagBadNum} onChange={(e) => setFlagBadNum(e.target.checked)} />
                Also flag values that are not numbers (default: skip them)
              </label>
            </Cfg>
          )}

          {ruleType === "UNIQUENESS" && (
            <Cfg>
              <p className="cfg-help">
                Pick a single <b>Element</b> above, <b>or</b> tick two or more columns here to
                check a combination — the Element selector then disappears.
              </p>
              <ChipPick options={columns} value={multiFields} onChange={setMultiFields} />
            </Cfg>
          )}

          {ruleType === "REFERENTIAL_INTEGRITY" && (
            <Cfg>
              <p className="cfg-help">
                The <b>Element</b> above is the column on {entityName}. Choose where it must exist —
                the lookup object must be in the same run.
              </p>
              <div className="grid2">
                <Fld label="Lookup Object">
                  <select value={lookupEntity} onChange={(e) => setLookupEntity(e.target.value)}>
                    {entities.map((e) => <option key={e.entity_name}>{e.entity_name}</option>)}
                  </select>
                </Fld>
                <Fld label="Lookup Element">
                  <select value={lookupField} onChange={(e) => setLookupField(e.target.value)}>
                    <option value="">Select…</option>
                    {lookupChoices.map((c) => (
                      <option key={c} value={c}>
                        {c}{c === lookupEnt?.primary_key_field ? "  (primary key)" : ""}
                      </option>
                    ))}
                  </select>
                </Fld>
              </div>
            </Cfg>
          )}

          {ruleType === "AGGREGATION" && (
            <Cfg>
              <div className="grid2">
                <Fld label="Function">
                  <select value={aggFn} onChange={(e) => setAggFn(e.target.value)}>
                    {AGG_FUNCS.map((f) => <option key={f}>{f}</option>)}
                  </select>
                </Fld>
                <Fld label="Measure element">
                  <select value={aggField} onChange={(e) => setAggField(e.target.value)}>
                    <option value="*">* (row count)</option>
                    {columns.map((c) => <option key={c}>{c}</option>)}
                  </select>
                </Fld>
              </div>
              <div style={{ marginTop: 16 }}>
                <p className="cfg-help">Group the rows by one or more columns.</p>
                <ChipPick options={columns} value={groupBy} onChange={setGroupBy} />
              </div>
              <div className="grid2" style={{ marginTop: 12 }}>
                <Fld label="Operator">
                  <select value={aggOp} onChange={(e) => setAggOp(e.target.value)}>
                    {OPS.map((o) => <option key={o}>{o}</option>)}
                  </select>
                </Fld>
                <Fld label="Threshold">
                  <input className="txt" value={threshold} placeholder="3"
                         onChange={(e) => setThreshold(e.target.value)} />
                </Fld>
              </div>
            </Cfg>
          )}

          {ruleType === "ALLOWED_VALUES" && (
            <Cfg><Fld label="Allowed values (comma separated)">
              <input className="txt" value={allowed} placeholder="US, IN, CA, UK"
                     onChange={(e) => setAllowed(e.target.value)} />
            </Fld></Cfg>
          )}

          {(ruleType === "CROSS_FIELD_SIMPLE" || ruleType === "CUSTOM_SQL") && (
            <Cfg>
              <Fld label="Expression (this object's columns only)">
                <input className="txt" value={expression}
                       placeholder="BillingCountry='USA' AND BillingState IS NULL"
                       onChange={(e) => setExpression(e.target.value)} />
              </Fld>
              <p className="cfg-note">
                Available columns: {columns.slice(0, 8).join(", ")}{columns.length > 8 ? "…" : ""}
              </p>
            </Cfg>
          )}

          <div className="grid2" style={{ marginTop: 14 }}>
            <Fld label="Rule name (optional)">
              <input className="txt" value={ruleName} placeholder="auto-generated if blank"
                     onChange={(e) => setRuleName(e.target.value)} />
            </Fld>
            <Fld label="Error message (optional)">
              <input className="txt" value={errMsg} placeholder="shown on each violation"
                     onChange={(e) => setErrMsg(e.target.value)} />
            </Fld>
          </div>

          <div className="form-actions">
            <button onClick={create} className="btn-primary">
              {editing ? "Save Changes" : "Save Draft"}
            </button>

          </div>
        </section>

        {guide && (
          <section className="card guide-card" style={{ marginBottom: 16 }}>
            <div className="guide-head">
              <span className="guide-tag">{ruleType.replace(/_/g, " ")}</span>
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
            <select className="sm" value={fEntity} onChange={(e) => setFEntity(e.target.value)}>
              <option value="">All objects</option>
              {entities.map((e) => <option key={e.entity_name}>{e.entity_name}</option>)}
            </select>
            <select className="sm" value={fStatus} onChange={(e) => setFStatus(e.target.value)}>
              <option value="">All status</option>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </div>
          <table className="cde">
            <thead>
              <tr>
                <th>ID</th><th>Rule Name</th><th>Object</th><th>Element</th>
                <th>Type</th><th>Severity</th><th>Status</th><th className="th-acts" />
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.rule_id} className="clickable"
                    onClick={(e) => {
                      // let the ⋮ menu handle its own clicks
                      if ((e.target as HTMLElement).closest(".kebab-wrap")) return;
                      setDetail(r);
                    }}>
                  <td className="el mono">{r.rule_id}</td>
                  <td className="rname">{r.rule_name}</td>
                  <td>{r.entity_name}</td>
                  <td>{r.field_name || <span className="tag-multi">multi-element</span>}</td>
                  {/* Dimension is not a column here -- it is derived from the
                      rule type, so the two would always say the same thing.
                      Still shown in the rule detail modal. */}
                  <td className="mini">{r.rule_type.replace(/_/g, " ")}</td>
                  <td><span className={`badge ${sevBadge(r.severity)}`}>{r.severity}</span></td>
                  <td><span className={`badge ${statusBadge(r.status)}`}>{r.status}</span></td>
                  <td className="acts-end">
                    <div className="kebab-wrap">
                      <button className="kebab" title="Actions"
                              onClick={() => setMenuFor(menuFor === r.rule_id ? null : r.rule_id)}>⋮</button>
                      {menuFor === r.rule_id && (
                        <>
                          <div className="menu-scrim" onClick={() => setMenuFor(null)} />
                          <div className="menu">
                            <button onClick={() => { setDetail(r); setMenuFor(null); }}>View details</button>
                            <button onClick={() => loadForEdit(r)}>Edit</button>
                            {(r.status === "DRAFT" || r.status === "UPDATED" || r.status === "REJECTED") && (
                              <button onClick={() => act(r.rule_id, "submit")}>Submit for approval</button>
                            )}
                            {r.status === "PENDING" && isAdmin && (
                              <>
                                <button className="ok" onClick={() => act(r.rule_id, "approve")}>Approve</button>
                                <button className="no" onClick={() => act(r.rule_id, "reject")}>Reject</button>
                              </>
                            )}
                            {r.status === "PENDING" && !isAdmin && (
                              <span className="menu-note">Approval needs the admin role</span>
                            )}
                            {r.status === "APPROVED" && isAdmin && (
                              <button className="no" onClick={() => act(r.rule_id, "retire")}>Retire</button>
                            )}
                            {r.status === "RETIRED" && isAdmin && (
                              <button className="ok" onClick={() => act(r.rule_id, "reactivate")}>Reactivate</button>
                            )}
                            {(r.status === "APPROVED" || r.status === "RETIRED") && !isAdmin && (
                              <span className="menu-note">Retire / reactivate needs admin</span>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {shown.length === 0 && <tr><td colSpan={8} className="mini">No rules yet — create one above.</td></tr>}
            </tbody>
          </table>
          <ul className="legend-notes">
            <li>Click a rule to see its full definition.</li>
            <li>Use <b>⋮</b> for actions.</li>
            <li>Only <b>admins</b> can approve or retire.</li>
          </ul>
        </section>

        {detail && <RuleDetail rule={detail} onClose={() => setDetail(null)} />}
      </div>
    </>
  );
}

function Fld({ label, children }: any) {
  return <div className="fld"><label>{label}</label>{children}</div>;
}
function Cfg({ children }: any) {
  return <div className="subpanel plain">{children}</div>;
}

/** Multi-select as toggle chips -- used for UNIQUENESS fields and AGGREGATION groupBy. */
function ChipPick({ options, value, onChange }: any) {
  const allOn = value.length === options.length && options.length > 0;
  return (
    <>
    <div className="chip-bar">
      <button type="button" className="chip-link"
              onClick={() => onChange(allOn ? [] : [...options])}>
        {allOn ? "Clear all" : "Select all"}
      </button>
      <span className="chip-count">{value.length} selected</span>
    </div>
    <div className="chips">
      {options.map((c: string) => {
        const on = value.includes(c);
        return (
          <button key={c} type="button" className={on ? "chip on" : "chip"}
                  onClick={() => onChange(on ? value.filter((v: string) => v !== c) : [...value, c])}>
            {c}
          </button>
        );
      })}
    </div>
    </>
  );
}

