"use client";

/**
 * The 5-field rule authoring form, matching the "what users should NOT see"
 * constraint: no SQL, no condition_expr, no JSON textbox, no internal IDs
 * beyond simple dropdowns. Backend generates everything else.
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
];

export default function RulesPage() {
  const [elements, setElements] = useState<any[]>([]);
  const [rules, setRules] = useState<any[]>([]);
  const [objectId, setObjectId] = useState<number | null>(null);

  const [elementId, setElementId] = useState<number | "">("");
  const [ruleType, setRuleType] = useState("required");
  const [severity, setSeverity] = useState("Warning");
  const [configText, setConfigText] = useState(""); // e.g. "US, IN, GB" for allowed_values

  const refresh = () => fetch(`${API_BASE}/api/rules`).then((r) => r.json()).then(setRules);

  useEffect(() => {
    fetch(`${API_BASE}/api/objects`).then((r) => r.json()).then((objs) => {
      if (objs[0]) {
        setObjectId(objs[0].object_id);
        fetch(`${API_BASE}/api/objects/${objs[0].object_id}/elements`).then((r) => r.json()).then(setElements);
      }
    });
    refresh();
  }, []);

  function buildConfig(): Record<string, any> {
    if (ruleType === "allowed_values") return { values: configText.split(",").map((v) => v.trim()).filter(Boolean) };
    if (ruleType === "max_length") return { max_length: Number(configText) };
    if (ruleType === "format_pattern") return { pattern: configText };
    if (ruleType === "conditional_required") {
      const [ifField, ifValue] = configText.split(":").map((v) => v.trim());
      return { if_field: ifField, if_value: ifValue };
    }
    return {};
  }

  async function createRule() {
    if (!elementId || !objectId) return;
    await fetch(`${API_BASE}/api/rules`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        object_id: objectId,
        element_id: elementId,
        rule_name: "",
        rule_type: ruleType,
        dimension: "Validity",
        severity,
        rule_config: buildConfig(),
        created_by: "web-ui",
      }),
    });
    setConfigText("");
    refresh();
  }

  async function transition(ruleId: number, action: "submit" | "approve" | "reject") {
    await fetch(`${API_BASE}/api/rules/${ruleId}/${action}`, { method: "POST" });
    refresh();
  }

  return (
    <main className="max-w-4xl mx-auto py-10 px-6 space-y-8">
      <h1 className="text-2xl font-bold">Manage Rules</h1>

      <section className="bg-white rounded-xl border border-slate-200 p-5 space-y-4">
        <h2 className="font-semibold">New Rule</h2>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Field">
            <select className="input" value={elementId} onChange={(e) => setElementId(Number(e.target.value))}>
              <option value="">Select a field…</option>
              {elements.map((el) => (
                <option key={el.element_id} value={el.element_id}>{el.element_name}</option>
              ))}
            </select>
          </Field>
          <Field label="Rule Type">
            <select className="input" value={ruleType} onChange={(e) => setRuleType(e.target.value)}>
              {RULE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </Field>
          {ruleType !== "required" && ruleType !== "unique" && (
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
        <div className="flex gap-3">
          <button onClick={createRule} className="px-4 py-2 rounded-lg bg-blue-600 text-white font-medium">
            Save Draft
          </button>
        </div>
      </section>

      <section className="bg-white rounded-xl border border-slate-200 p-5">
        <h2 className="font-semibold mb-3">All Rules</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-slate-500 border-b">
              <th className="py-2">Name</th><th>Type</th><th>Severity</th><th>Status</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.rule_id} className="border-b last:border-0">
                <td className="py-2">{r.rule_name}</td>
                <td>{r.rule_type}</td>
                <td>{r.severity}</td>
                <td>
                  <span className={`px-2 py-0.5 rounded text-xs ${statusColor(r.status)}`}>{r.status}</span>
                </td>
                <td className="text-right space-x-2">
                  {r.status === "draft" && (
                    <button onClick={() => transition(r.rule_id, "submit")} className="text-blue-600 text-xs">Submit</button>
                  )}
                  {r.status === "submitted" && (
                    <>
                      <button onClick={() => transition(r.rule_id, "approve")} className="text-green-600 text-xs">Approve</button>
                      <button onClick={() => transition(r.rule_id, "reject")} className="text-red-600 text-xs">Reject</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <style jsx global>{`
        .input { border: 1px solid #cbd5e1; border-radius: 0.5rem; padding: 0.5rem 0.75rem; width: 100%; }
      `}</style>
    </main>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-slate-500 uppercase tracking-wide">{label}</span>
      <div className="mt-1">{children}</div>
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

function statusColor(status: string) {
  return {
    draft: "bg-slate-100 text-slate-600",
    submitted: "bg-blue-100 text-blue-700",
    approved: "bg-green-100 text-green-700",
    rejected: "bg-red-100 text-red-700",
  }[status] || "bg-slate-100";
}
