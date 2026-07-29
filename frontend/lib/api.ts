/**
 * Thin fetch wrapper pointing at the FastAPI backend. Every dashboard page
 * reads from these functions -- nothing computes numbers client-side, the
 * backend's DQ_METRIC-backed endpoints are the single source of truth.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

export interface Kpis {
  overall_dq_score: number | null;
  objects_checked: number;
  critical_failed_checks: number;
  records_scanned: number;
  rule_coverage_pct: number;
}

export interface HeatmapRow {
  object_id: number;
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
  severity: string;
}

export interface Drilldown {
  run_id: number;
  object_id: number;
  object_name: string;
  overall_score: number;
  elements_checked: number;
  records_scanned: number;
  dimension_scores: Record<string, number>;
  elements: DrilldownElement[];
}

export interface CriticalByDimension {
  total: number;
  breakdown: { dimension: string; count: number; pct: number }[];
}

export interface DqObject {
  object_id: number;
  object_name: string;
  source_system: string;
  record_key_column: string;
  active_flag: boolean;
}

export interface DashboardSummary {
  kpis: Kpis;
  heatmap: HeatmapRow[];
  top_failing: TopFailingItem[];
  trend: TrendPoint[];
  fix_profile: FixProfile;
  critical_by_dimension: CriticalByDimension;
}

export const api = {
  /** One round trip for the whole overview page (replaces 6 separate calls). */
  summary: () => get<DashboardSummary>("/api/dashboard/summary"),
  kpis: () => get<Kpis>("/api/dashboard/kpis"),
  heatmap: () => get<HeatmapRow[]>("/api/dashboard/heatmap"),
  topFailing: (limit = 5) => get<TopFailingItem[]>(`/api/dashboard/top-failing?limit=${limit}`),
  trend: () => get<TrendPoint[]>("/api/dashboard/trend"),
  fixProfile: () => get<FixProfile>("/api/dashboard/fix-profile"),
  criticalByDimension: () => get<CriticalByDimension>("/api/dashboard/critical-by-dimension"),
  drilldown: (objectId: number) => get<Drilldown>(`/api/dashboard/object/${objectId}/drilldown`),
  objects: () => get<DqObject[]>("/api/objects"),
  rules: () => get<any[]>("/api/rules"),
  runs: () => get<any[]>("/api/runs"),
};

export const DIMENSION_ORDER = [
  "Completeness", "Validity", "Format", "Uniqueness", "Ref Integrity", "Relationship",
];

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
