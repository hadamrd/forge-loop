// src/domain/models.ts
// Resource models — names match the real backend status/MCP contracts so real/ is a 1:1 swap.

import type { CriticVerdict } from "./events";

export type ValueAxis =
  | "durable-control-plane"
  | "project-cognition-memory"
  | "frontier-generation"
  | "sandboxed-worker-execution"
  | "self-dogfood-operability"
  | "quality-and-evidence-gates";

export type SagaState =
  | "DISPATCHED" | "RUNNING" | "AWAITING_CRITIC" | "REVISING"
  | "MERGED" | "ABANDONED" | "COMPENSATED" | "QUARANTINED";

export type Severity = "sev1" | "sev2" | "sev3";

// ── Loop status (the real status contract) ───────────────────────────
export interface ProjectionStatus { name: string; sequence: number; lag: number; }
export interface LoopStatus {
  summary: string;
  available: boolean;
  sequence: number;
  last_sequence: number;
  lag: number;
  active_count: number;
  in_flight_count: number;
  stale_lease_count: number;
  rejected_count: number;
  halted: boolean;
  boot: { booted_at: string; doctor: "ok" | "degraded" | "error"; version: string; env: "ok" | "degraded" };
  event_log: { path: string; sequence: number; size_mb: number };
  projections: ProjectionStatus[];
  frontier: { current_problem: string; next_expansion: string; version: number };
  memory: { total: number; promoted_today: number; superseded: number };
  tasks: Saga[];
}

// ── Saga / Task ──────────────────────────────────────────────────────
export interface Saga {
  saga_id: string;
  issue: { number: number; title: string; axis: ValueAxis };
  worker_id: string;
  worktree_path: string;
  state: SagaState;
  repair_rounds: number;
  lease_expires_at: string | null;
  heartbeat_at: string;
  cost_usd: number;
  branch: string;
  started_at: string;
  ended_at?: string;
}

// ── Worker ───────────────────────────────────────────────────────────
export interface CapabilityPolicy { secret_names: string[]; mcp: string[]; network_egress: string[]; }
export interface MonologueLine { t: string; level: "plan" | "info" | "tool" | "warn" | "error"; text: string; }
export interface Worker {
  id: string;
  issue_number: number;
  model: string;
  worktree_path: string;
  state: SagaState;
  started_at: string;
  last_event_at: string;
  cost_usd: number;
  tokens: number;
  capability_policy: CapabilityPolicy;
  withheld_secrets: string[];
  monologue_log_path: string;
  monologue?: MonologueLine[];      // hydrated by getWorkerLog
}

// ── PR + critic ──────────────────────────────────────────────────────
export type PrLabel =
  | "critic:blocking" | "critic:suspicious" | "loop:auto-rescued"
  | "loop:ready" | "clean" | "epic";

export interface Finding { severity: Severity; category: string; file: string; line: number; message: string; }
export interface PathStep { text: string; done: boolean; }
export interface RepairRound { round: number; sev2: number; }
export interface CriticReview {
  verdict: CriticVerdict;
  round: number;
  suspicious: boolean;
  suspicious_reason?: string;
  sev_counts: { sev1: number; sev2: number; sev3: number };
  findings: Finding[];
  minimal_path_to_green: PathStep[];
  history: RepairRound[];           // sev2 per round → the convergence sparkline
}
export interface PullRequest {
  number: number;
  title: string;
  branch: string;
  additions: number;
  deletions: number;
  mergeable: boolean;
  state: "open" | "merged" | "closed";
  saga_id: string | null;
  labels: PrLabel[];
  review: CriticReview;
}

// ── Scorecard (self-improvement) ─────────────────────────────────────
export interface ScorecardPoint {
  t: string; idx: number;
  first_pass: number | null;        // null until enough merges accrue
  repair_rounds: number;
  lead_time: number | null;
  cost_per_merge: number;
}
export interface Scorecard {
  first_pass_critic_acceptance_rate: number | null;
  mean_repair_rounds_to_converge: number | null;
  sev2_regeneration_rate: null;     // structurally null — not yet instrumented
  mean_lead_time_seconds: number | null;
  abandonment_rate: number | null;
  cost_per_merged_pr: number | null;
  history: ScorecardPoint[];
  /** Why a metric is null — render as a designed state, never a fake 0. */
  nulls: Record<string, { reason: string; detail: string; needs?: number }>;
}

// ── Frontier cursor (.forge/frontier.yaml) ───────────────────────────
export interface Frontier {
  version: number;
  product_goal: string;
  current_problem: string;
  next_expansion: string;
  why_now: string;
  objective: string;
  key_result: string;
  kr_current: number;
  kr_target: number;
  kr_merges_window: number;
  kr_merges_observed: number;
  active_decisions: { id: string; text: string; at: string }[];
  rejected_paths: { idea: string; reason: string; revisit_if: string }[];
  hot_files: { ref: string; why_hot: string }[];
  open_questions: string[];
}

// ── Memory ───────────────────────────────────────────────────────────
export type MemoryKind = "episodic" | "procedural" | "rejected_path";
export interface Memory {
  id: string;
  kind: MemoryKind;
  title: string;
  body: string;
  evidence_refs: string[];
  superseded_by?: string | null;
  created_at: string;
  confidence: number;
}

// ── Backlog / issues ─────────────────────────────────────────────────
export interface Issue {
  number: number;
  title: string;
  axis: ValueAxis;
  labels: PrLabel[];
  state: "open" | "in_progress" | "closed";
  epic?: number;
}

// ── Manifesto rules (gates) ──────────────────────────────────────────
export interface ManifestoRule {
  id: string;
  manifesto: "quality" | "testing";
  rule: string;
  severity: Severity;
  rationale: string;
  source_pr?: string | null;
}

// ── Budget / pipeline ────────────────────────────────────────────────
export interface BudgetPoint { t: string; hourly: number; cumulative: number; tokens: number; }
export interface Budget {
  points: BudgetPoint[];
  spend_today: number;
  cumulative: number;
  tokens_today: number;
  cost_per_merged_pr: number;
}
export interface PipelineStage { stage: string; role: string; model: string; status: "ok" | "degraded" | "error"; }
