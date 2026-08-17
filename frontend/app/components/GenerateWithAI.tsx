"use client";

import React, { useMemo, useState } from "react";
import { Entity, DIMENSION_ORDER } from "@/lib/api";

/**
 * Rule generation from a written instruction.
 *
 * NOT WIRED TO A MODEL. There is no inference service behind this yet, so the
 * Generate button produces a PLAN, never rules: it says what would be created,
 * counted from the real catalog, and states plainly that nothing was written.
 *
 * That is deliberate. A panel that returned invented rule text would be
 * indistinguishable from a real result, and those rules are one click from
 * APPROVED and then from scoring production data. Counting is arithmetic over
 * the selected object's declared elements -- it is true whether or not a model
 * is ever connected.
 */

const MODELS = [
  { id: "claude-opus-5",   name: "Claude Opus 5",   note: "Best judgement on ambiguous columns" },
  { id: "claude-sonnet-5", name: "Claude Sonnet 5", note: "Balanced — good default for bulk work" },
  { id: "claude-haiku-4-5", name: "Claude Haiku 4.5", note: "Fastest, for simple one-per-element passes" },
];

/** The boring, repetitive jobs this is meant to remove. */
const PRESETS = [
  {
    label: "Completeness for every element",
    dims: ["Completeness"],
    text: "Create one COMPLETENESS rule for every element on this object. "
        + "Severity CRITICAL for identifiers and anything a downstream system "
        + "keys on, WARNING for the rest.",
  },
  {
    label: "Format rules from the data",
    dims: ["Validity"],
    text: "Look at the values actually present in each element and write a "
        + "VALIDITY rule for any column with a consistent shape — codes, ids, "
        + "dates, phone numbers, postal codes. Skip free text.",
  },
  {
    label: "Uniqueness on identifiers",
    dims: ["Uniqueness"],
    text: "Add a UNIQUENESS rule to every element that looks like an "
        + "identifier or an external system key.",
  },
  {
    label: "Allowed values from distinct values",
    dims: ["Validity"],
    text: "For every element with fewer than 25 distinct values, write an "
        + "ALLOWED_VALUES rule listing the values that are actually present.",
  },
];

export default function GenerateWithAI({
  entity, onClose,
}: { entity?: Entity; onClose: () => void }) {
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState(MODELS[1].id);
  const [dims, setDims] = useState<string[]>(["Completeness"]);
  const [severity, setSeverity] = useState("WARNING");
  const [plan, setPlan] = useState<null | { count: number; per: string }>(null);

  const elements = entity?.columns ?? [];
  const chosen = MODELS.find((m) => m.id === model)!;

  // One rule per element per selected dimension. Plain arithmetic over the
  // real catalog -- no model involved, so the number is honest.
  const estimate = useMemo(
    () => elements.length * Math.max(dims.length, 1),
    [elements.length, dims.length],
  );

  function applyPreset(p: typeof PRESETS[number]) {
    setPrompt(p.text);
    setDims(p.dims);
    setPlan(null);
  }

  function toggleDim(d: string) {
    setDims((s) => (s.includes(d) ? s.filter((x) => x !== d) : [...s, d]));
    setPlan(null);
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="aicard" onClick={(e) => e.stopPropagation()}>
        <div className="ai-head">
          <span className="ai-glyph" aria-hidden>✦</span>
          <b>Generate rules</b>
          <span className="ai-chip">PREVIEW · NOT CONNECTED</span>
          <button className="rc-x" onClick={onClose} aria-label="Close">×</button>
        </div>

        <p className="ai-sub">
          Describe the rules you want for <b>{entity?.entity_name ?? "this object"}</b> and
          they will be drafted for review. Nothing is saved or approved automatically.
        </p>

        <div className="ai-presets">
          {PRESETS.map((p) => (
            <button key={p.label} className="ai-preset" onClick={() => applyPreset(p)}>
              {p.label}
            </button>
          ))}
        </div>

        <textarea
          className="ai-prompt"
          rows={5}
          value={prompt}
          onChange={(e) => { setPrompt(e.target.value); setPlan(null); }}
          placeholder={
            "e.g. Create a completeness rule for all 20 elements on this object.\n"
            + "Make the identifier columns CRITICAL and everything else WARNING."
          }
        />

        <div className="ai-scope">
          <div className="ai-field">
            <label>Object</label>
            <div className="ai-static">
              {entity?.entity_name ?? "—"}
              <span>{elements.length} elements</span>
            </div>
          </div>
          <div className="ai-field">
            <label>Default severity</label>
            <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
              {["INFO", "WARNING", "ERROR", "CRITICAL"].map((s) => <option key={s}>{s}</option>)}
            </select>
          </div>
          <div className="ai-field">
            <label>Model</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {MODELS.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </div>
        </div>
        <p className="ai-modelnote">{chosen.note}</p>

        <div className="ai-field">
          <label>Dimensions to cover</label>
          <div className="ai-dims">
            {DIMENSION_ORDER.map((d) => (
              <button
                key={d}
                className={`ai-dim${dims.includes(d) ? " on" : ""}`}
                onClick={() => toggleDim(d)}
              >
                {d}
              </button>
            ))}
          </div>
        </div>

        {plan && (
          <div className="ai-plan">
            <b>Nothing was created.</b> No model is connected to this environment
            yet, so this is the plan only.
            <div className="ai-plan-n">
              <span>{plan.count}</span> rules would be drafted — {plan.per}
            </div>
            <div className="ai-plan-el">
              {elements.slice(0, 10).join(" · ")}
              {elements.length > 10 ? ` · +${elements.length - 10} more` : ""}
            </div>
            <p>
              Every rule would arrive as <b>DRAFT</b> for you to review, edit and
              approve. None of them can score anything until you approve it.
            </p>
          </div>
        )}

        <div className="ai-actions">
          <span className="ai-est">
            {estimate > 0
              ? `~${estimate} rules across ${dims.length || 1} dimension${dims.length === 1 ? "" : "s"}`
              : "Pick an object first"}
          </span>
          <button className="btn-mini" onClick={onClose}>Cancel</button>
          <button
            className="btn-primary"
            disabled={!entity || !dims.length}
            onClick={() =>
              setPlan({
                count: estimate,
                per: `one per element × ${dims.length} dimension${dims.length === 1 ? "" : "s"}`,
              })
            }
          >
            Generate
          </button>
        </div>
      </div>
    </div>
  );
}
