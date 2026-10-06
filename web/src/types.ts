export type Source = "sim" | "live";

export interface AppConfig {
  app_name: string;
  assistant_name: string;
  brand_name: string;
  tagline: string;
  oauth_required: boolean;
  llm: string;
  llm_route?: string;
  live_configured: boolean;
  live_host: string | null;
  data_source_override: Source | null;
  version: string;
}

export interface ScenarioView {
  id: string;
  title: string;
  demo_order?: number;
  description: string;
  intent: string;
  search_query: string;
  suggested_replies: string[];
  data_sources: Record<string, Source>;
  persona?: { first_name: string; plan: string };
}

export interface SessionInfo {
  session_id: string;
  scenario: ScenarioView;
  search_query: string;
  customer: { first_name: string; plan: string };
  access?: { connected: boolean; can_make_changes: boolean };
}

export type Json = Record<string, unknown>;

export type ServerEvent =
  | { type: "turn_start" }
  | { type: "text"; segment: string; delta: string }
  | { type: "segment_end"; segment: string; text: string }
  | { type: "tool_start"; call_id: string; tool: string; title: string; source: Source }
  | {
      type: "tool_result";
      call_id: string;
      tool: string;
      title: string;
      source: Source;
      fallback: boolean;
      latency_ms: number;
      is_error: boolean;
      data: Json;
    }
  | { type: "confirm_required"; action_id: string; tool: string; title: string; summary: string; details: Json }
  | { type: "action_resolved"; action_id: string; approved: boolean; tool: string; superseded?: boolean }
  | { type: "guardrail"; kind: string; segment: string; message: string }
  | { type: "notice"; message: string }
  | { type: "error"; code: string; message: string }
  | { type: "done"; pending_actions: string[] };

export type ChatItem =
  | { kind: "user"; id: string; text: string }
  | { kind: "bot"; id: string; text: string; streaming: boolean }
  | {
      kind: "tool";
      id: string;
      tool: string;
      title: string;
      source: Source;
      status: "running" | "done" | "error";
      data?: Json;
      latency?: number;
      fallback?: boolean;
    }
  | {
      kind: "confirm";
      id: string;
      tool: string;
      title: string;
      summary: string;
      details: Json;
      state: "pending" | "approved" | "declined" | "superseded";
    }
  | { kind: "notice"; id: string; text: string; tone: "info" | "warn" | "error" };

export interface Metrics {
  sessions: number;
  resolved_without_agent: number;
  containment_rate: number;
  faults_detected: number;
  truck_rolls_avoided: number;
  truck_roll_cost_assumption_usd: number;
  estimated_field_cost_avoided_usd: number;
  technician_visits_booked: number;
  credits_applied: number;
  credits_issued_usd: number;
  offers_presented: number;
  offers_accepted: number;
  offer_conversion_rate: number;
  upsell_blocked_fault_first: number;
  orders_previewed: number;
  devices_sold: number;
  device_sales_usd: number;
  trade_in_credits_usd: number;
  mobile_bundles: number;
  new_mobile_lines: number;
  storm_data_passes: number;
  checkups_run: number;
  account_issues_found: number;
  account_issues_fixed: number;
  monthly_savings_found_usd: number;
  monthly_savings_realized_usd: number;
  incremental_monthly_revenue_usd: number;
  actions_confirmed: number;
  actions_declined: number;
  guardrail_interventions: number;
  tool_calls: number;
  tool_calls_sim: number;
  tool_calls_live: number;
  tool_errors: number;
  avg_time_to_resolution_s: number | null;
  avg_tool_latency_ms: Record<string, number>;
  by_scenario: Record<string, Record<string, number>>;
  recent_events: { ts: number; kind: string; scenario: string | null; detail: string }[];
  note: string;
}
