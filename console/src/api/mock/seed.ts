// src/api/mock/seed.ts
// TEMPORARY scaffold seed — replaced by the full faithful dataset in Wave 1.
// Export surface is FROZEN: mockApi.ts imports exactly these names. Keep the names + types.
import type {
  Frontier, Scorecard, ScorecardPoint, Worker, Saga, PullRequest, Memory,
  Issue, ManifestoRule, Budget, BudgetPoint, PipelineStage,
} from "../../domain/models";
import type { EventEnvelope } from "../../domain/events";

const NOW = Date.now();
export const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();
const hrs = (h: number) => h * 3_600_000;
const mins = (m: number) => m * 60_000;

export const frontier: Frontier = {
  version: 47,
  product_goal: "Turn the GitHub backlog into merged, evidence-backed PRs with zero human dispatch.",
  current_problem: "First-pass critic acceptance plateaued at ~70%.",
  next_expansion: "Promote 'write the failing test first' into the dispatch prompt.",
  why_now: "Repair-rounds-to-converge fell 3.4→1.8 over 3 ticks.",
  objective: "Converge faster with fewer interventions — without lowering the critic's bar.",
  key_result: "First-pass critic acceptance ≥ 80%, sustained across 20 consecutive merges.",
  kr_current: 0.71, kr_target: 0.8, kr_merges_window: 20, kr_merges_observed: 14,
  active_decisions: [],
  rejected_paths: [],
  hot_files: [],
  open_questions: [],
};

const scHistory: ScorecardPoint[] = Array.from({ length: 20 }, (_, i) => {
  const measured = i >= 4;
  return {
    t: iso(hrs(40) - i * mins(120)), idx: i,
    first_pass: measured ? +(0.44 + (i - 4) * (0.27 / 15)).toFixed(3) : null,
    repair_rounds: +(3.4 - i * (1.6 / 19)).toFixed(2),
    lead_time: measured ? Math.round(10200 - (i - 4) * 300) : null,
    cost_per_merge: +(5.9 - i * (2.7 / 19)).toFixed(2),
  };
});
export const scorecard: Scorecard = {
  first_pass_critic_acceptance_rate: 0.71,
  mean_repair_rounds_to_converge: 1.82,
  sev2_regeneration_rate: null,
  mean_lead_time_seconds: 5460,
  abandonment_rate: null,
  cost_per_merged_pr: 3.18,
  history: scHistory,
  nulls: {
    sev2_regeneration_rate: { reason: "not instrumented", detail: "Requires the critic to tag regressions across rounds." },
    abandonment_rate: { reason: "not yet measured", detail: "Needs 6 more terminal sagas in-window (14 of 20).", needs: 6 },
  },
};

export const workers: Worker[] = [];
export const sagas: Saga[] = [];
export const prs: PullRequest[] = [];
export const memory: Memory[] = [];
export const backlog: Issue[] = [];
export const manifestos: ManifestoRule[] = [];

export const pipeline: PipelineStage[] = [
  { stage: "plan", role: "planner", model: "claude-opus-4-8", status: "ok" },
  { stage: "dispatch", role: "dispatcher", model: "—", status: "ok" },
  { stage: "execute", role: "worker", model: "claude-opus-4-8", status: "ok" },
  { stage: "critique", role: "critic", model: "claude-opus-4-8", status: "ok" },
  { stage: "merge", role: "merger", model: "—", status: "ok" },
  { stage: "learn", role: "librarian", model: "claude-opus-4-8", status: "ok" },
];

const budgetPoints: BudgetPoint[] = Array.from({ length: 24 }, (_, k) => {
  const i = 23 - k;
  const hourly = +(2.1 + Math.sin(i / 3) * 1.4).toFixed(2);
  return { t: iso(hrs(i)), hourly, cumulative: 0, tokens: Math.round(hourly * 95000) };
});
let cum = 184.2;
for (const p of budgetPoints) {
  cum += p.hourly;
  p.cumulative = +cum.toFixed(2);
}
export const budget: Budget = {
  points: budgetPoints,
  spend_today: 48.9,
  cumulative: budgetPoints[budgetPoints.length - 1].cumulative,
  tokens_today: 4_640_000,
  cost_per_merged_pr: 3.18,
};

export function seedEvents(): EventEnvelope[] {
  return [];
}
