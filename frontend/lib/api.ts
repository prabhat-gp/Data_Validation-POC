/**
 * Thin fetch wrapper pointing at the FastAPI backend. Every page reads from
 * these functions -- nothing computes numbers client-side, the backend's
 * val_metrics-backed endpoints are the single source of truth.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function post<T>(path: string, body: any): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `${path} -> ${res.status}`);
  }
  return res.json();
}

async function put<T>(path: string, body: any): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `${path} -> ${res.status}`);
  }
  return res.json();
}

export { API_BASE };

/* ----------------------------------------------------------------- catalog */
export interface Entity {
  entity_name: string;
  source_system: string;
  source_object_name: string;
  primary_key_field: string;
  columns: string[];
  approved_rule_count: number;
}

export interface RuleType {
  code: string;
  description: string;
  dimension: string;
  execution_type: string;
}

/* ------------------------------------------------------------------- rules */
export interface Rule {
  rule_id: number;
  rule_name: string;
  source_system: string;
  rule_type: string;
  dimension: string | null;
  entity_name: string;
  field_name: string;
  primary_key_field: string;
  execution_type: string;
  rule_definition: string | null;
  error_message: string | null;
  severity: string;
  status: string;
  active: boolean;
  created_by: string;
  created_date: string;
  approved_by: string | null;
  approved_date: string | null;
}

/* -------------------------------------------------------------------- runs */
export interface Run {
  run_id: number;
  batch_id: number;
  entity_name: string;
  run_type: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  records_scanned: number;
  total_records: number | null;
  phase: string | null;
  rules_total: number;
  rules_done: number;
  rules_executed: number;
  source_file_name: string | null;
  error_message: string | null;
}

export interface Batch {
  batch_id: number;
  batch_name: string | null;
  run_type: string;
  triggered_by: string | null;
  started_at: string;
  status: string;          // running | completed | completed_with_errors | empty
  entity_count: number;
  runs: Run[];
}

/* --------------------------------------------------------------- dashboard */
export interface Kpis {
  overall_dq_score: number | null;
  objects_checked: number;
  cdes_checked: number;
  records_scanned: number;
  records_affected: number;   // rows with >=1 violation
  critical_failed_checks: number;
  checks_run: number;         // the DQ score's denominator
  checks_failed: number;
  rule_coverage_pct: number;
}

export interface HeatmapRow {
  object_id: string;        // entity name is the identifier now
  object_name: string;
  run_id: number;
  dimensions: Record<string, number>;
  overall: number;
}

export interface TopFailingItem {
  element_name: string;
  object_name: string;
  score_pct: number;
  records_failed: number;
  severity: string;
}

export interface TrendPoint {
  run_name: string;
  dq_score: number | null;
  critical_failed_checks: number;
  entity_count: number;     // composition changed? the chart must say so
}

export interface FixProfile {
  critical_pct: number;
  warning_pct: number;
  critical_count: number;
  warning_count: number;
}

export interface DrilldownElement {
  element_name: string;
  dimension: string;
  score_pct: number;
  records_failed: number;
  records_checked: number;
  severity: string;
  rule_id: number;
}

export interface Drilldown {
  run_id: number;
  object_id: string;
  object_name: string;
  overall_score: number;
  elements_checked: number;   // distinct elements covered
  checks_run: number;         // rules executed -- several can hit one element
  records_scanned: number;
  dimension_scores: Record<string, number>;
  elements: DrilldownElement[];
}

export interface CriticalByDimension {
  total: number;
  breakdown: { dimension: string; count: number; pct: number }[];
}

export interface DashboardSummary {
  kpis: Kpis;
  heatmap: HeatmapRow[];
  top_failing: TopFailingItem[];
  trend: TrendPoint[];
  fix_profile: FixProfile;
  critical_by_dimension: CriticalByDimension;
}

export type Role = "viewer" | "owner" | "admin";

/** Placeholder until Entra ID lands -- then the role comes from the token. */
export function getRole(): Role {
  if (typeof window === "undefined") return "viewer";
  return (localStorage.getItem("role") as Role) || "admin";
}
export function setRole(r: Role) { localStorage.setItem("role", r); }
export function getActor(): string {
  if (typeof window === "undefined") return "system";
  return localStorage.getItem("actor") || "prabhat";
}
export function setActor(a: string) { localStorage.setItem("actor", a); }

export interface BatchOption {
  batch_id: number;
  batch_name: string;
  run_type: string;
  source_system: string | null;
  entity_count: number;
  started_at: string;
}

export interface SourceCheck { ok: boolean; detail: string }

export const SOURCE_SYSTEMS = ["SFDC", "Hybris", "MySQL", "File Dump"];

export const SEVERITIES = ["INFO", "WARNING", "ERROR", "CRITICAL"];
export const STATUSES = ["DRAFT", "PENDING", "APPROVED", "REJECTED", "UPDATED", "RETIRED"];

export const api = {
  /** One round trip for the whole overview page. */
  summary: (batchId?: number | null, sourceSystem?: string) => {
    const q = new URLSearchParams();
    if (batchId) q.set("batch_id", String(batchId));
    if (sourceSystem) q.set("source_system", sourceSystem);
    return get<DashboardSummary>(`/api/dashboard/summary${q.toString() ? "?" + q : ""}`);
  },
  batchOptions: (sourceSystem?: string) =>
    get<BatchOption[]>(`/api/dashboard/batch-options${sourceSystem ? `?source_system=${encodeURIComponent(sourceSystem)}` : ""}`),
  checkSource: (sourceSystem: string) =>
    get<SourceCheck>(`/api/runs/source/check?source_system=${encodeURIComponent(sourceSystem)}`),
  drilldown: (entityName: string, sourceSystem?: string) =>
    get<Drilldown>(`/api/dashboard/object/${encodeURIComponent(entityName)}/drilldown` +
      (sourceSystem ? `?source_system=${encodeURIComponent(sourceSystem)}` : "")),

  entities: () => get<Entity[]>("/api/entities"),
  ruleTypes: () => get<RuleType[]>("/api/entities/meta/rule-types"),

  rules: () => get<Rule[]>("/api/rules"),
  rule: (ruleId: number) => get<Rule>(`/api/rules/${ruleId}`),
  createRule: (body: any) => post<Rule>("/api/rules", { ...body, role: getRole() }),
  transitionRule: (ruleId: number, action: "submit" | "approve" | "reject", actor: string) =>
    post<Rule>(`/api/rules/${ruleId}/${action}`, { actor, role: getRole() }),
  ruleSql: (ruleId: number) => get<any>(`/api/rules/${ruleId}/sql`),
  updateRule: (ruleId: number, body: any) =>
    put<Rule>(`/api/rules/${ruleId}`, { ...body, role: getRole() }),
  retireRule: (ruleId: number, actor: string) =>
    post<Rule>(`/api/rules/${ruleId}/retire`, { actor, role: getRole() }),
  reactivateRule: (ruleId: number, actor: string) =>
    post<Rule>(`/api/rules/${ruleId}/reactivate`, { actor, role: getRole() }),

  batches: () => get<Batch[]>("/api/runs/batches"),
  batch: (batchId: number) => get<Batch>(`/api/runs/batches/${batchId}`),
  runFromDb: (body: any) => post<Batch>("/api/runs/db-fetch", { ...body, role: getRole() }),
};

/**
 * The six quality dimensions, in heatmap column order. Must stay identical to
 * DIMENSIONS in backend/app/rule_compiler.py -- a name here that the engine
 * never emits becomes a column of permanent dashes, and one the engine emits
 * that is missing here silently hides real failures from the heatmap.
 */
export const DIMENSION_ORDER = [
  "Completeness", "Validity", "Uniqueness", "Consistency", "Integrity", "Accuracy",
];

// No DIMENSION_FOR_TYPE map here on purpose. Dimension is decided by the
// backend from rule_type and returned on the rule, so the frontend never has a
// second copy of the classification that could drift out of step.

export function ragClass(score: number): "good" | "warn" | "crit" {
  if (score > 90) return "good";
  if (score >= 80) return "warn";
  return "crit";
}

export function fmtNum(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return Math.round(n / 1000) + "K";
  return String(n);
}
