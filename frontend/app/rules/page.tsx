"use client";

/**
 * The rule authoring form. Simple rule types (Required, Allowed Values,
 * Format Pattern, Max Length, Unique, Conditional Required) keep the
 * original 5-field pattern: no SQL, no condition_expr, no JSON, no internal
 * IDs beyond simple dropdowns -- backend generates everything else.
 *
 * Two rule types need MORE than that, and their UI reflects it honestly:
 *   - Referential Integrity: adds a "Reference Object" + "Reference Field"
 *     pair, since the check spans two objects.
 *   - Multi-Condition: replaces the single Field dropdown entirely with a
 *     repeatable condition builder (field/operator/value rows, AND/OR,
 *     then require-a-field OR flag-the-record) -- because the rule spans
 *     multiple fields on the same row, not one.
 */

import { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

const RULE_TYPES = [
  { value: "required", label: "Required" },
  { value: "allowed_values", label: "Allowed Values" },
  { value: "format_pattern", label: "Format Pattern" },
  { value: "max_length", label: "Max Length" },
  { value: "unique", label: "Unique" },
  { value: "conditional_required", label: "Conditional Required" },
  { value: "ref_integrity", label: "Referential Integrity" },
  { value: "multi_condition", label: "Multi-Condition (Advanced)" },
];

const OPERATORS = [
  { value: "=", label: "equals" },
  { value: "!=", label: "not equals" },
  { value: "in", label: "is one of (comma-separated)" },
  { value: "is_null", label: "is empty" },
  { value: "is_not_null", label: "is not empty" },
];

interface Condition { field: string; operator: string; value: string; }

/**
 * Worked examples for the two newest rule types -- these are the hardest to
 * imagine from an empty form, so clicking one pre-fills the builder above.
 */
/**
 * One worked example per rule type. The card for the CURRENTLY SELECTED rule
 * type is shown under the form -- so picking "Unique" shows the Unique
 * example, not the Multi-Condition one.
 */
interface RuleGuide {
  title: string;
  plain: string;          // what it does, in one plain sentence
  example: string;        // a concrete example on real Account fields
  sql: string;            // the SQL it compiles to
  prefill?: {
    fieldName?: string; configText?: string; refFieldName?: string;
    conditions?: Condition[]; logic?: "AND" | "OR";
    thenType?: "require" | "flag"; thenField?: string;
  };
}

const RULE_GUIDE: Record<string, RuleGuide> = {
  required: {
    title: "Required",
    plain: "The field must not be empty. Any blank value is a violation.",
    example: "Every Account must have a Name.",
    sql: "WHERE Name IS NULL OR TRIM(Name) = ''",
    prefill: { fieldName: "Name" },
  },
  allowed_values: {
    title: "Allowed Values",
    plain: "The value must be one of a fixed list you provide. Anything else is a violation.",
    example: "BillingCountry may only be US, IN or GB.",
    sql: "WHERE BillingCountry NOT IN ('US','IN','GB')",
    prefill: { fieldName: "BillingCountry", configText: "US, IN, GB" },
  },
  format_pattern: {
    title: "Format Pattern",
    plain: "The value must match a pattern (regex). Used for emails, URLs, codes.",
    example: "Website must start with http:// or https://",
    sql: "WHERE NOT REGEXP('^https?://', Website)",
    prefill: { fieldName: "Website", configText: "^https?://" },
  },
  max_length: {
    title: "Max Length",
    plain: "The value must not be longer than the limit you set.",
    example: "BillingPostalCode must be 20 characters or fewer.",
    sql: "WHERE LENGTH(BillingPostalCode) > 20",
    prefill: { fieldName: "BillingPostalCode", configText: "20" },
  },
  unique: {
    title: "Unique",
    plain:
      "No two records may share the same value. Unlike the others, this one has to compare every row against every other row.",
    example: "Two Accounts must not have the same Name.",
    sql: "GROUP BY Name HAVING COUNT(*) > 1",
    prefill: { fieldName: "Name" },
  },
  conditional_required: {
    title: "Conditional Required",
    plain: "A field becomes mandatory only when another field has a specific value.",
    example: "If Type is 'Owner/Operator', then Phone must be filled in.",
    sql: "WHERE Type = 'Owner/Operator' AND (Phone IS NULL OR TRIM(Phone) = '')",
    prefill: { fieldName: "Phone", configText: "Type:Owner/Operator" },
  },
  ref_integrity: {
    title: "Referential Integrity",
    plain:
      "The value must already exist in another object's field -- the classic foreign-key check. Catches typos and values invented outside the approved set.",
    example:
      "Every Account.BillingCountry must be a country that already appears in Contact.BillingCountry.",
    sql: "WHERE BillingCountry NOT IN (SELECT value FROM dq_reference_value WHERE …)",
    prefill: { fieldName: "BillingCountry", refFieldName: "BillingCountry" },
  },
  multi_condition: {
    title: "Multi-Condition (Advanced)",
    plain:
      "Chain several conditions on the SAME record with AND/OR, then either require a field or just flag the record. Use when one field alone can't express the rule.",
    example:
      "If Type = 'Owner/Operator' AND BillingCountry = 'USA', then BillingCity must be filled in.",
    sql: "WHERE (Type='Owner/Operator' AND BillingCountry='USA') AND (BillingCity IS NULL OR TRIM(BillingCity)='')",
    prefill: {
      conditions: [
        { field: "Type", operator: "=", value: "Owner/Operator" },
        { field: "BillingCountry", operator: "=", value: "USA" },
      ],
      logic: "AND", thenType: "require", thenField: "BillingCity",
    },
  },
};

export default function RulesPage() {
  const [objects, setObjects] = useState<any[]>([]);
  const [elements, setElements] = useState<any[]>([]);
  const [rules, setRules] = useState<any[]>([]);
  const [objectId, setObjectId] = useState<number | "">("");

  const [elementId, setElementId] = useState<number | "">("");
  const [ruleType, setRuleType] = useState("required");
  const [severity, setSeverity] = useState("Warning");
  const [configText, setConfigText] = useState(""); // e.g. "US, IN, GB" for allowed_values

  // -- Referential Integrity: a second object/field pair --
  const [refObjectId, setRefObjectId] = useState<number | "">("");
  const [refElements, setRefElements] = useState<any[]>([]);
  const [refElementId, setRefElementId] = useState<number | "">("");

  // -- Multi-Condition: condition builder state --
  const [conditions, setConditions] = useState<Condition[]>([{ field: "", operator: "=", value: "" }]);
  const [logic, setLogic] = useState<"AND" | "OR">("AND");
  const [thenType, setThenType] = useState<"require" | "flag">("require");
  const [thenField, setThenField] = useState("");

  // "Generate with AI" -- NOT wired to any real LLM (no AI usage approved yet).
  const [showAiPanel, setShowAiPanel] = useState(false);
  const [aiCopied, setAiCopied] = useState(false);

  const refresh = () => fetch(`${API_BASE}/api/rules`).then((r) => r.json()).then(setRules);

  useEffect(() => {
    fetch(`${API_BASE}/api/objects`).then((r) => r.json()).then((objs) => {
      setObjects(objs);
      if (objs[0]) { setObjectId(objs[0].object_id); setRefObjectId(objs[0].object_id); }
    });
    refresh();
  }, []);

  useEffect(() => {
    if (!objectId) { setElements([]); return; }
    setElementId("");
    fetch(`${API_BASE}/api/objects/${objectId}/elements`).then((r) => r.json()).then(setElements);
  }, [objectId]);

  // Reference Object's own field list, independent of the main Field dropdown
  useEffect(() => {
    if (!refObjectId) { setRefElements([]); return; }
    setRefElementId("");
    fetch(`${API_BASE}/api/objects/${refObjectId}/elements`).then((r) => r.json()).then(setRefElements);
  }, [refObjectId]);

  function buildConfig(): Record<string, any> {
    if (ruleType === "allowed_values") return { values: configText.split(",").map((v) => v.trim()).filter(Boolean) };
    if (ruleType === "max_length") return { max_length: Number(configText) };
    if (ruleType === "format_pattern") return { pattern: configText };
    if (ruleType === "conditional_required") {
      const [ifField, ifValue] = configText.split(":").map((v) => v.trim());
      return { if_field: ifField, if_value: ifValue };
    }
    if (ruleType === "ref_integrity") {
      return { ref_object_id: refObjectId, ref_element_id: refElementId };
    }
    if (ruleType === "multi_condition") {
      return {
        conditions: conditions
          .filter((c) => c.field && c.operator)
          .map((c) => ({
            field: c.field,
            operator: c.operator,
            value: c.operator === "in" ? c.value.split(",").map((v) => v.trim()).filter(Boolean) : c.value,
          })),
        logic,
        then: thenType === "require" ? { type: "require", field: thenField } : { type: "flag" },
      };
    }
    return {};
  }

  // multi_condition has no single "Field" selection -- DQRule.element_id is
  // still required (NOT NULL FK), so we attach it to whichever field the
  // rule is most naturally "about": the require-target, or the first
  // condition's field if it's a pure flag rule.
  function resolveElementId(): number | "" {
    if (ruleType !== "multi_condition") return elementId;
    const name = thenType === "require" ? thenField : conditions[0]?.field;
    return elements.find((e) => e.element_name === name)?.element_id ?? "";
  }

  async function createRule() {
    const effectiveElementId = resolveElementId();
    if (!effectiveElementId || !objectId) return;
    if (ruleType === "ref_integrity" && (!refObjectId || !refElementId)) return;

    await fetch(`${API_BASE}/api/rules`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        object_id: objectId,
        element_id: effectiveElementId,
        rule_name: "",
        rule_type: ruleType,
        dimension: "Validity",
        severity,
        rule_config: buildConfig(),
        created_by: "web-ui",
      }),
    });
    setConfigText("");
    setConditions([{ field: "", operator: "=", value: "" }]);
    setThenField("");
    refresh();
  }

  async function transition(ruleId: number, action: "submit" | "approve" | "reject") {
    await fetch(`${API_BASE}/api/rules/${ruleId}/${action}`, { method: "POST" });
    refresh();
  }

  function buildAiPrompt(): string {
    const obj = objects.find((o) => o.object_id === objectId);
    const el = elements.find((e) => e.element_id === elementId);
    return [
      `I'm building a data validation rule for a Salesforce object.`,
      `Object: ${obj?.object_name || "(choose an object above)"}`,
      `Field: ${el?.element_name || "(choose a field above)"} (data type: ${el?.data_type || "string"})`,
      ``,
      `Supported rule types: Required, Allowed Values, Format Pattern, Max Length, Conditional Required,`,
      `Unique, Referential Integrity, Multi-Condition.`,
      `Based on typical real-world data for this field, suggest ONE rule type and its exact configuration.`,
      `Return only: rule type + configuration value(s) + a one-line reason.`,
    ].join("\n");
  }

  async function copyAiPrompt() {
    await navigator.clipboard.writeText(buildAiPrompt());
    setAiCopied(true);
    setTimeout(() => setAiCopied(false), 2000);
  }

  function updateCondition(i: number, patch: Partial<Condition>) {
    setConditions((cs) => cs.map((c, idx) => (idx === i ? { ...c, ...patch } : c)));
  }

  const isMultiCondition = ruleType === "multi_condition";
  const isRefIntegrity = ruleType === "ref_integrity";

  function applyGuideExample() {
    const g = RULE_GUIDE[ruleType];
    if (!g?.prefill) return;
    const p = g.prefill;
    if (p.configText !== undefined) setConfigText(p.configText);
    if (p.fieldName) {
      const el = elements.find((e) => e.element_name === p.fieldName);
      if (el) setElementId(el.element_id);
    }
    if (p.refFieldName) {
      const refEl = refElements.find((e) => e.element_name === p.refFieldName);
      if (refEl) setRefElementId(refEl.element_id);
    }
    if (p.conditions) setConditions(p.conditions);
    if (p.logic) setLogic(p.logic);
    if (p.thenType) setThenType(p.thenType);
    if (p.thenField) setThenField(p.thenField);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return (
    <>
      <div className="topbar">
        <div>
          <h1>Manage Rules</h1>
          <div className="sub">Author, review and approve validation rules — no code, no deployment</div>
        </div>
      </div>

      <div className="content" style={{ maxWidth: 1000 }}>
      <section className="card" style={{ marginBottom: 16 }}>
        <h2>New Rule</h2>

        <div className="grid grid-cols-2 gap-4">
          <Field label="Object">
            <select className="input" value={objectId} onChange={(e) => setObjectId(Number(e.target.value))}>
              {objects.map((o) => (
                <option key={o.object_id} value={o.object_id}>{o.object_name}</option>
              ))}
            </select>
          </Field>

          {/* Standard single Field dropdown -- hidden for Multi-Condition,
              which replaces it with the condition builder below */}
          {!isMultiCondition && (
            <Field label="Field">
              <select className="input" value={elementId} onChange={(e) => setElementId(Number(e.target.value))}>
                <option value="">Select a field…</option>
                {elements.map((el) => (
                  <option key={el.element_id} value={el.element_id}>{el.element_name}</option>
                ))}
              </select>
            </Field>
          )}

          <Field label="Rule Type">
            <select className="input" value={ruleType} onChange={(e) => setRuleType(e.target.value)}>
              {RULE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </Field>

          {!isMultiCondition && !isRefIntegrity && ruleType !== "required" && ruleType !== "unique" && (
            <Field label={configHint(ruleType)}>
              <input className="input" value={configText} onChange={(e) => setConfigText(e.target.value)} />
            </Field>
          )}

          <Field label="Severity">
            <select className="input" value={severity} onChange={(e) => setSeverity(e.target.value)}>
              <option>Critical</option>
              <option>Warning</option>
            </select>
          </Field>
        </div>

        {/* ---- Referential Integrity: the two extra fields ---- */}
        {isRefIntegrity && (
          <div style={{border:"1px solid var(--accent)",background:"var(--accent-soft)",borderRadius:10,padding:14,marginTop:16}}>
            <p className="mini" style={{color:"var(--accent-ink)",marginBottom:12}}>
              The <b>Field</b> above is the column on THIS object being checked. Below, choose which object/field it
              must exist in.
            </p>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Reference Object">
                <select className="input" value={refObjectId} onChange={(e) => setRefObjectId(Number(e.target.value))}>
                  {objects.map((o) => (
                    <option key={o.object_id} value={o.object_id}>{o.object_name}</option>
                  ))}
                </select>
              </Field>
              <Field label="Reference Field">
                <select className="input" value={refElementId} onChange={(e) => setRefElementId(Number(e.target.value))}>
                  <option value="">Select a field…</option>
                  {refElements.map((el) => (
                    <option key={el.element_id} value={el.element_id}>{el.element_name}</option>
                  ))}
                </select>
              </Field>
            </div>
            <p className="mini" style={{color:"var(--accent-ink)",marginTop:8}}>
              Note: this only has values to check against once the reference object has been validated at least once.
            </p>
          </div>
        )}

        {/* ---- Multi-Condition: the full condition builder ---- */}
        {isMultiCondition && (
          <div style={{border:"1px solid var(--violet)",background:"var(--violet-bg)",borderRadius:10,padding:14,marginTop:16,display:"flex",flexDirection:"column",gap:12}}>
            <p className="mini" style={{color:"var(--violet)"}}>
              Build a rule across multiple fields of the same record (e.g. "if Type = X and Country = Y, then
              require Website").
            </p>

            {conditions.map((c, i) => (
              <div key={i} className="flex gap-2 items-end">
                <div className="flex-1">
                  <span style={{fontSize:9.5,textTransform:"uppercase",letterSpacing:".07em",color:"var(--ink-faint)",fontWeight:700}}>Field</span>
                  <select
                    className="input"
                    value={c.field}
                    onChange={(e) => updateCondition(i, { field: e.target.value })}
                  >
                    <option value="">Select a field…</option>
                    {elements.map((el) => (
                      <option key={el.element_id} value={el.element_name}>{el.element_name}</option>
                    ))}
                  </select>
                </div>
                <div className="flex-1">
                  <span style={{fontSize:9.5,textTransform:"uppercase",letterSpacing:".07em",color:"var(--ink-faint)",fontWeight:700}}>Operator</span>
                  <select
                    className="input"
                    value={c.operator}
                    onChange={(e) => updateCondition(i, { operator: e.target.value })}
                  >
                    {OPERATORS.map((op) => <option key={op.value} value={op.value}>{op.label}</option>)}
                  </select>
                </div>
                {c.operator !== "is_null" && c.operator !== "is_not_null" && (
                  <div className="flex-1">
                    <span style={{fontSize:9.5,textTransform:"uppercase",letterSpacing:".07em",color:"var(--ink-faint)",fontWeight:700}}>Value</span>
                    <input
                      className="input"
                      value={c.value}
                      onChange={(e) => updateCondition(i, { value: e.target.value })}
                    />
                  </div>
                )}
                {conditions.length > 1 && (
                  <button
                    onClick={() => setConditions((cs) => cs.filter((_, idx) => idx !== i))}
                    className="text-red-500 text-xs px-2 py-2"
                  >
                    Remove
                  </button>
                )}
              </div>
            ))}

            <div className="flex gap-3 items-center">
              <button
                onClick={() => setConditions((cs) => [...cs, { field: "", operator: "=", value: "" }])}
                style={{color:"var(--violet)",fontSize:11.5,fontWeight:700,border:"1px solid var(--violet)",borderRadius:7,padding:"5px 10px",background:"none",cursor:"pointer"}}
              >
                + Add Condition
              </button>
              {conditions.length > 1 && (
                <label className="mini" style={{display:"flex",alignItems:"center",gap:8}}>
                  Match:
                  <select className="input !w-auto" value={logic} onChange={(e) => setLogic(e.target.value as "AND" | "OR")}>
                    <option value="AND">ALL conditions (AND)</option>
                    <option value="OR">ANY condition (OR)</option>
                  </select>
                </label>
              )}
            </div>

            <div style={{borderTop:"1px solid var(--violet)",paddingTop:12}}>
              <span style={{fontSize:9.5,textTransform:"uppercase",letterSpacing:".07em",color:"var(--ink-faint)",fontWeight:700}}>Then</span>
              <div className="flex gap-4 items-center mt-1">
                <label className="text-sm flex items-center gap-1">
                  <input type="radio" checked={thenType === "require"} onChange={() => setThenType("require")} />
                  Require a field
                </label>
                <label className="text-sm flex items-center gap-1">
                  <input type="radio" checked={thenType === "flag"} onChange={() => setThenType("flag")} />
                  Just flag the record
                </label>
                {thenType === "require" && (
                  <select className="input !w-48" value={thenField} onChange={(e) => setThenField(e.target.value)}>
                    <option value="">Select a field…</option>
                    {elements.map((el) => (
                      <option key={el.element_id} value={el.element_name}>{el.element_name}</option>
                    ))}
                  </select>
                )}
              </div>
            </div>
          </div>
        )}

        <div className="form-actions">
          <button onClick={createRule} className="btn-primary">Save Draft</button>
          <button onClick={() => setShowAiPanel((v) => !v)} className="btn-ghost">✨ Generate with AI</button>
        </div>

        {showAiPanel && (
          <div style={{border:"1px solid var(--violet)",background:"var(--violet-bg)",borderRadius:10,padding:14,marginTop:16,display:"flex",flexDirection:"column",gap:12}}>
            <p className="mini" style={{color:"var(--violet)"}}>
              <b>Not connected to an AI yet</b> — LLM data usage isn't approved for this project. For now this just
              builds a ready-made prompt: copy it into Claude, ChatGPT, or Copilot Chat yourself, then type its
              suggested rule type + config into the fields above by hand. Once AI is approved, this button will call
              it directly instead.
            </p>
            <textarea
              readOnly
              value={buildAiPrompt()}
              rows={6}
              style={{width:"100%",fontSize:11,fontFamily:"ui-monospace,monospace",border:"1px solid var(--violet)",borderRadius:8,padding:9,background:"var(--panel)",color:"var(--ink)"}}
            />
            <button onClick={copyAiPrompt} style={{padding:"7px 13px",borderRadius:8,background:"var(--violet)",color:"#fff",fontSize:11.5,fontWeight:700,border:"none",cursor:"pointer",alignSelf:"flex-start"}}>
              {aiCopied ? "Copied!" : "Copy Prompt"}
            </button>
          </div>
        )}
      </section>

      {RULE_GUIDE[ruleType] && (
        <section className="card guide-card" style={{ marginBottom: 16 }}>
          <div className="guide-head">
            <span className="guide-tag">{RULE_GUIDE[ruleType].title}</span>
            <h2 style={{ margin: 0 }}>How this rule type works</h2>
            {RULE_GUIDE[ruleType].prefill && (
              <button className="guide-fill" onClick={applyGuideExample}>Fill form with this example</button>
            )}
          </div>
          <p className="guide-plain">{RULE_GUIDE[ruleType].plain}</p>
          <div className="guide-row">
            <span className="guide-lbl">Example</span>
            <span className="guide-val">{RULE_GUIDE[ruleType].example}</span>
          </div>
          <div className="guide-row">
            <span className="guide-lbl">Becomes</span>
            <code className="guide-sql">{RULE_GUIDE[ruleType].sql}</code>
          </div>
        </section>
      )}

      <section className="card">
        <h2>All Rules</h2>
        <table className="cde">
          <thead>
            <tr>
              <th>Name</th><th>Object</th><th>Type</th><th>Severity</th><th>Status</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rules.length === 0 && (
              <tr><td colSpan={6} className="mini" style={{ padding: "14px 10px" }}>No rules yet — create one above.</td></tr>
            )}
            {rules.map((r) => (
              <tr key={r.rule_id}>
                <td style={{ fontWeight: 600 }}>{r.rule_name}</td>
                <td className="mini">{objects.find((o) => o.object_id === r.object_id)?.object_name || r.object_id}</td>
                <td className="el">{r.rule_type}</td>
                <td>{r.severity}</td>
                <td>
                  <span className={`badge ${statusBadge(r.status)}`}>{r.status}</span>
                </td>
                <td style={{ textAlign: "right" }}>
                  {r.status === "draft" && (
                    <button onClick={() => transition(r.rule_id, "submit")} style={linkBtn("var(--accent)")}>Submit</button>
                  )}
                  {r.status === "submitted" && (
                    <>
                      <button onClick={() => transition(r.rule_id, "approve")} style={linkBtn("var(--good)")}>Approve</button>
                      <button onClick={() => transition(r.rule_id, "reject")} style={linkBtn("var(--crit)")}>Reject</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      </div>
    </>
  );
}

function linkBtn(color: string): React.CSSProperties {
  return {
    color, background: "none", border: "none", cursor: "pointer",
    fontSize: 11.5, fontWeight: 700, padding: "0 6px",
  };
}

function statusBadge(status: string) {
  return { draft: "b-draft", submitted: "b-sub", approved: "b-appr", rejected: "b-rej" }[status] || "b-draft";
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "block" }}>
      <span className="fl" style={{ fontSize: 9.5, textTransform: "uppercase", letterSpacing: ".07em", color: "var(--ink-faint)", fontWeight: 700 }}>
        {label}
      </span>
      <div style={{ marginTop: 5 }}>{children}</div>
    </label>
  );
}

function configHint(ruleType: string) {
  if (ruleType === "allowed_values") return "Allowed Values (comma-separated)";
  if (ruleType === "max_length") return "Max Length";
  if (ruleType === "format_pattern") return "Pattern (regex)";
  if (ruleType === "conditional_required") return "If Field: If Value (e.g. Type:Active)";
  return "Config";
}

