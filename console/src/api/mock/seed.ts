// src/api/mock/seed.ts
// Full faithful dataset ported from data.jsx — Wave 1 replacement.
// Export surface is FROZEN: mockApi.ts imports exactly these names.
import type {
  Frontier, Scorecard, ScorecardPoint, Worker, MonologueLine, Saga,
  PullRequest, Memory, Issue, ManifestoRule, Budget, BudgetPoint, PipelineStage,
} from "../../domain/models";
import type { EventEnvelope, EventKind } from "../../domain/events";

// ── Time helpers ─────────────────────────────────────────────────────
const NOW = Date.now();
export const iso = (msAgo: number): string => new Date(NOW - msAgo).toISOString();
const hrs = (h: number) => h * 3_600_000;
const mins = (m: number) => m * 60_000;

// ── Frontier cursor ──────────────────────────────────────────────────
export const frontier: Frontier = {
  version: 47,
  product_goal:
    "Turn the GitHub backlog into merged, evidence-backed PRs with zero human dispatch — and measurably improve at it over time.",
  current_problem:
    "First-pass critic acceptance plateaued at ~70%. Workers converge, but too many PRs need a second revision round, inflating lead time and spend.",
  next_expansion:
    "Promote the 'write the failing test first' procedural memory into the dispatch prompt and measure first-pass acceptance over the next 20 merges.",
  why_now:
    "Repair-rounds-to-converge has fallen 3.4→1.8 over the last 3 ticks; the remaining cost is concentrated in sev2 findings the worker could have anticipated.",
  objective:
    "Converge faster with fewer interventions — without lowering the critic's bar.",
  key_result:
    "First-pass critic acceptance ≥ 80%, sustained across 20 consecutive merges.",
  kr_current: 0.71,
  kr_target: 0.80,
  kr_merges_window: 20,
  kr_merges_observed: 14,
  active_decisions: [
    { id: "D-118", text: "Critic runs before any auto-merge; no PR merges on a changes_requested verdict.", at: iso(hrs(31)) },
    { id: "D-121", text: "Workers get least-privilege secrets per axis; egress default-deny.", at: iso(hrs(20)) },
    { id: "D-124", text: "Failing-test-first is mandatory for quality-axis issues.", at: iso(hrs(6)) },
  ],
  rejected_paths: [
    { idea: "Let workers self-merge clean PRs to cut lead time.", reason: "Removes the critic gate — the one mechanism that makes 'self-improving' falsifiable.", revisit_if: "First-pass acceptance ≥ 95% for 50 merges." },
    { idea: "Single shared worktree to save disk.", reason: "Cross-task contamination caused 2 quarantines in tick 41.", revisit_if: "Worktree reaping proven leak-free for 1 week." },
    { idea: "Skip heartbeats; rely on process exit.", reason: "Crashed workers held leases for 40m; stale-lease recovery depends on heartbeats.", revisit_if: "Never — heartbeats are load-bearing." },
  ],
  hot_files: [
    { ref: "control_plane/saga.py", why_hot: "Lease-expiry recovery path touched in 4 of last 6 merges." },
    { ref: "critic/reviewer.py", why_hot: "Sev2 categorisation rules under active tuning." },
    { ref: "prompts/dispatch.md", why_hot: "Failing-test-first instruction being promoted from memory." },
    { ref: ".forge/frontier.yaml", why_hot: "Cursor itself; advanced 3× this tick." },
  ],
  open_questions: [
    "Does failing-test-first help all axes, or only quality-and-evidence-gates?",
    "Is the 90s heartbeat lease too tight for large refactors?",
    "Should suspicious-but-passing PRs auto-merge after a cooldown, or always hold?",
  ],
};

// ── Scorecard ────────────────────────────────────────────────────────
const scHistory: ScorecardPoint[] = Array.from({ length: 20 }, (_, i) => {
  const measured = i >= 4;
  const fp = measured ? +(0.44 + (i - 4) * (0.27 / 15) + Math.sin(i) * 0.012).toFixed(3) : null;
  const rr = +(3.4 - i * (1.6 / 19) + Math.cos(i) * 0.06).toFixed(2);
  const lt = measured ? Math.round(10200 - (i - 4) * 300 + Math.sin(i) * 180) : null;
  const cpm = +(5.9 - i * (2.7 / 19) + Math.sin(i / 2) * 0.12).toFixed(2);
  return {
    t: iso(hrs(40) - i * mins(120)),
    idx: i,
    first_pass: fp,
    repair_rounds: rr,
    lead_time: lt,
    cost_per_merge: cpm,
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
    sev2_regeneration_rate: {
      reason: "Instrumentation not yet wired",
      detail: "Requires the critic to tag regressions across rounds. Ships in the evidence-gates axis.",
    },
    abandonment_rate: {
      reason: "Not yet measured",
      detail: "Needs 6 more terminal sagas in-window (14 of 20).",
      needs: 6,
    },
  },
};

// ── Backlog (30 issues) ──────────────────────────────────────────────
export const backlog: Issue[] = [
  { number: 412, title: "Recover stale leases without dropping in-flight sagas", axis: "durable-control-plane", labels: ["loop:ready", "epic"], state: "in_progress" },
  { number: 418, title: "Idempotent event replay on projection rebuild", axis: "durable-control-plane", labels: ["loop:ready"], state: "open", epic: 412 },
  { number: 421, title: "Compaction without losing causal links", axis: "durable-control-plane", labels: [], state: "open", epic: 412 },
  { number: 430, title: "Crash-recovery doctor: detect orphaned worktrees", axis: "durable-control-plane", labels: ["loop:ready"], state: "open" },
  { number: 305, title: "Promote failing-test-first into dispatch prompt", axis: "project-cognition-memory", labels: ["loop:ready"], state: "in_progress" },
  { number: 311, title: "Supersede outdated procedural memories automatically", axis: "project-cognition-memory", labels: ["epic"], state: "open" },
  { number: 314, title: "Confidence decay for episodic memory", axis: "project-cognition-memory", labels: [], state: "open", epic: 311 },
  { number: 319, title: "Evidence-ref backlinks from memory to PRs", axis: "project-cognition-memory", labels: ["loop:ready"], state: "open", epic: 311 },
  { number: 501, title: "Generate next-expansion from rejected-path reasons", axis: "frontier-generation", labels: ["loop:ready", "epic"], state: "open" },
  { number: 506, title: "Score ideas by expected KR movement", axis: "frontier-generation", labels: [], state: "open", epic: 501 },
  { number: 509, title: "Auto-revisit rejected paths when revisit_if fires", axis: "frontier-generation", labels: ["loop:ready"], state: "open" },
  { number: 515, title: "Vision drift detector vs product_goal", axis: "frontier-generation", labels: [], state: "open" },
  { number: 220, title: "Per-axis least-privilege secret scoping", axis: "sandboxed-worker-execution", labels: ["loop:ready", "epic"], state: "in_progress" },
  { number: 224, title: "Default-deny network egress with allowlist", axis: "sandboxed-worker-execution", labels: ["loop:ready"], state: "open", epic: 220 },
  { number: 229, title: "Worktree reaping leak audit", axis: "sandboxed-worker-execution", labels: [], state: "open", epic: 220 },
  { number: 233, title: "MCP capability grants per worker", axis: "sandboxed-worker-execution", labels: ["loop:ready"], state: "open" },
  { number: 238, title: "Quarantine contaminated worktrees automatically", axis: "sandboxed-worker-execution", labels: [], state: "open" },
  { number: 140, title: "Status endpoint: projections + lag + boot/doctor", axis: "self-dogfood-operability", labels: ["loop:ready", "epic"], state: "in_progress" },
  { number: 144, title: "Budget endpoint: cost & tokens over time", axis: "self-dogfood-operability", labels: ["loop:ready"], state: "open", epic: 140 },
  { number: 148, title: "SSE event stream with since-sequence cursor", axis: "self-dogfood-operability", labels: ["loop:ready"], state: "open", epic: 140 },
  { number: 151, title: "worker_logs MCP tool for live monologue tail", axis: "self-dogfood-operability", labels: [], state: "open" },
  { number: 155, title: "Halt + resume controls with reason capture", axis: "self-dogfood-operability", labels: ["loop:ready"], state: "open" },
  { number: 602, title: "Critic sev2 categorisation rule tuning", axis: "quality-and-evidence-gates", labels: ["loop:ready", "epic"], state: "in_progress" },
  { number: 606, title: "Minimal-path-to-green checklist generation", axis: "quality-and-evidence-gates", labels: ["loop:ready"], state: "open", epic: 602 },
  { number: 609, title: "Suspicious-PR detector (passes tests, smells wrong)", axis: "quality-and-evidence-gates", labels: [], state: "open", epic: 602 },
  { number: 613, title: "Sev2 regeneration-rate instrumentation", axis: "quality-and-evidence-gates", labels: ["loop:ready"], state: "open", epic: 602 },
  { number: 617, title: "Testing manifesto: forbid skipped tests in merged PRs", axis: "quality-and-evidence-gates", labels: [], state: "open" },
  { number: 621, title: "Quality manifesto: no broadened except clauses", axis: "quality-and-evidence-gates", labels: ["loop:ready"], state: "open" },
  { number: 626, title: "Repair-round trajectory chart per saga", axis: "quality-and-evidence-gates", labels: [], state: "open" },
  { number: 631, title: "Block merge on any unresolved sev1", axis: "quality-and-evidence-gates", labels: ["loop:ready"], state: "open" },
];

// ── Workers (3 active) ───────────────────────────────────────────────
const MODEL = "claude-opus-4-8";

const mono = (lines: Array<{ level: MonologueLine["level"]; text: string }>): MonologueLine[] =>
  lines.map((l, i) => ({ t: iso(mins(8) - i * 1100), level: l.level, text: l.text }));

export const workers: Worker[] = [
  {
    id: "wkr_7af3",
    issue_number: 412,
    model: MODEL,
    worktree_path: ".forge/wt/412-stale-leases",
    state: "RUNNING",
    started_at: iso(mins(8)),
    last_event_at: iso(mins(0.2)),
    cost_usd: 1.92,
    tokens: 184320,
    capability_policy: {
      secret_names: ["GITHUB_TOKEN", "FORGE_DB_URL"],
      mcp: ["github", "filesystem"],
      network_egress: ["api.github.com"],
    },
    withheld_secrets: ["AWS_ACCESS_KEY", "STRIPE_KEY", "OPENAI_KEY", "SLACK_TOKEN"],
    monologue_log_path: ".forge/logs/wkr_7af3.jsonl",
    monologue: mono([
      { level: "plan", text: "Issue 412 — recover stale leases without dropping in-flight sagas. Reading control_plane/saga.py." },
      { level: "info", text: "Lease expiry is checked in the tick loop but recovery drops the saga to DISPATCHED, losing repair_rounds." },
      { level: "plan", text: "Plan: write failing test first (D-124). test_stale_lease_preserves_progress." },
      { level: "tool", text: "filesystem.write tests/test_saga_recovery.py (+38)" },
      { level: "info", text: "Test red as expected: recovered saga lost repair_rounds=2 → 0." },
      { level: "tool", text: "filesystem.edit control_plane/saga.py: carry repair_rounds + heartbeat across recovery (+14 -3)" },
      { level: "info", text: "Test green. Running full saga suite…" },
      { level: "tool", text: "shell pytest tests/ -k saga → 41 passed" },
      { level: "plan", text: "Opening PR against main. Branch loop/412-stale-leases." },
    ]),
  },
  {
    id: "wkr_2c9d",
    issue_number: 602,
    model: MODEL,
    worktree_path: ".forge/wt/602-sev2-rules",
    state: "AWAITING_CRITIC",
    started_at: iso(mins(22)),
    last_event_at: iso(mins(1.4)),
    cost_usd: 3.07,
    tokens: 291040,
    capability_policy: {
      secret_names: ["GITHUB_TOKEN"],
      mcp: ["github", "filesystem"],
      network_egress: ["api.github.com"],
    },
    withheld_secrets: ["AWS_ACCESS_KEY", "STRIPE_KEY", "FORGE_DB_URL", "OPENAI_KEY", "SLACK_TOKEN"],
    monologue_log_path: ".forge/logs/wkr_2c9d.jsonl",
    monologue: mono([
      { level: "plan", text: "Issue 602 — tune critic sev2 categorisation. Touching critic/reviewer.py." },
      { level: "tool", text: "filesystem.edit critic/reviewer.py: split 'error-handling' into sev2/sev3 by blast radius (+52 -9)" },
      { level: "tool", text: "filesystem.write tests/test_sev_rules.py (+71)" },
      { level: "info", text: "All new rule tests green. PR opened #1287." },
      { level: "info", text: "Awaiting critic verdict…" },
    ]),
  },
  {
    id: "wkr_e10b",
    issue_number: 305,
    model: MODEL,
    worktree_path: ".forge/wt/305-test-first",
    state: "REVISING",
    started_at: iso(mins(34)),
    last_event_at: iso(mins(0.6)),
    cost_usd: 2.41,
    tokens: 226880,
    capability_policy: {
      secret_names: ["GITHUB_TOKEN"],
      mcp: ["github", "filesystem"],
      network_egress: [],
    },
    withheld_secrets: ["AWS_ACCESS_KEY", "STRIPE_KEY", "FORGE_DB_URL", "OPENAI_KEY", "SLACK_TOKEN"],
    monologue_log_path: ".forge/logs/wkr_e10b.jsonl",
    monologue: mono([
      { level: "plan", text: "Issue 305 — promote failing-test-first into dispatch prompt. PR #1284 opened." },
      { level: "warn", text: "Critic round 1: 1 sev2 — prompt change lacks a regression test proving behaviour shift." },
      { level: "plan", text: "Minimal path to green: add tests/test_dispatch_prompt.py asserting test-first ordering." },
      { level: "tool", text: "filesystem.write tests/test_dispatch_prompt.py (+44)" },
      { level: "info", text: "Pushed revision. Repair round 2. Re-requesting critic." },
    ]),
  },
];

// ── Sagas (9 total: 3 active + 6 terminal) ───────────────────────────
export const sagas: Saga[] = [
  {
    saga_id: "sg_412",
    issue: { number: 412, title: "Recover stale leases without dropping in-flight sagas", axis: "durable-control-plane" },
    worker_id: "wkr_7af3",
    worktree_path: ".forge/wt/412-stale-leases",
    state: "RUNNING",
    repair_rounds: 0,
    lease_expires_at: iso(-mins(1.5)),
    heartbeat_at: iso(mins(0.2)),
    cost_usd: 1.92,
    branch: "loop/412-stale-leases",
    started_at: iso(mins(8)),
  },
  {
    saga_id: "sg_602",
    issue: { number: 602, title: "Critic sev2 categorisation rule tuning", axis: "quality-and-evidence-gates" },
    worker_id: "wkr_2c9d",
    worktree_path: ".forge/wt/602-sev2-rules",
    state: "AWAITING_CRITIC",
    repair_rounds: 0,
    lease_expires_at: iso(-mins(0.8)),
    heartbeat_at: iso(mins(1.4)),
    cost_usd: 3.07,
    branch: "loop/602-sev2-rules",
    started_at: iso(mins(22)),
  },
  {
    saga_id: "sg_305",
    issue: { number: 305, title: "Promote failing-test-first into dispatch prompt", axis: "project-cognition-memory" },
    worker_id: "wkr_e10b",
    worktree_path: ".forge/wt/305-test-first",
    state: "REVISING",
    repair_rounds: 1,
    lease_expires_at: iso(-mins(2)),
    heartbeat_at: iso(mins(0.6)),
    cost_usd: 2.41,
    branch: "loop/305-test-first",
    started_at: iso(mins(34)),
  },
  {
    saga_id: "sg_140",
    issue: { number: 140, title: "Status endpoint: projections + lag + boot/doctor", axis: "self-dogfood-operability" },
    worker_id: "wkr_91aa",
    worktree_path: ".forge/wt/140-status",
    state: "MERGED",
    repair_rounds: 1,
    lease_expires_at: null,
    heartbeat_at: iso(hrs(2)),
    cost_usd: 2.86,
    branch: "loop/140-status",
    started_at: iso(hrs(3)),
    ended_at: iso(hrs(2)),
  },
  {
    saga_id: "sg_220",
    issue: { number: 220, title: "Per-axis least-privilege secret scoping", axis: "sandboxed-worker-execution" },
    worker_id: "wkr_55cd",
    worktree_path: ".forge/wt/220-secrets",
    state: "MERGED",
    repair_rounds: 0,
    lease_expires_at: null,
    heartbeat_at: iso(hrs(4)),
    cost_usd: 1.74,
    branch: "loop/220-secrets",
    started_at: iso(hrs(5)),
    ended_at: iso(hrs(4)),
  },
  {
    saga_id: "sg_144",
    issue: { number: 144, title: "Budget endpoint: cost & tokens over time", axis: "self-dogfood-operability" },
    worker_id: "wkr_77ef",
    worktree_path: ".forge/wt/144-budget",
    state: "MERGED",
    repair_rounds: 2,
    lease_expires_at: null,
    heartbeat_at: iso(hrs(6)),
    cost_usd: 3.42,
    branch: "loop/144-budget",
    started_at: iso(hrs(8)),
    ended_at: iso(hrs(6)),
  },
  {
    saga_id: "sg_229",
    issue: { number: 229, title: "Worktree reaping leak audit", axis: "sandboxed-worker-execution" },
    worker_id: "wkr_33bb",
    worktree_path: ".forge/wt/229-reap",
    state: "COMPENSATED",
    repair_rounds: 2,
    lease_expires_at: null,
    heartbeat_at: iso(hrs(9)),
    cost_usd: 2.13,
    branch: "loop/229-reap",
    started_at: iso(hrs(11)),
    ended_at: iso(hrs(9)),
  },
  {
    saga_id: "sg_515",
    issue: { number: 515, title: "Vision drift detector vs product_goal", axis: "frontier-generation" },
    worker_id: "wkr_4419",
    worktree_path: ".forge/wt/515-drift",
    state: "ABANDONED",
    repair_rounds: 3,
    lease_expires_at: null,
    heartbeat_at: iso(hrs(13)),
    cost_usd: 4.88,
    branch: "loop/515-drift",
    started_at: iso(hrs(16)),
    ended_at: iso(hrs(13)),
  },
  {
    saga_id: "sg_238",
    issue: { number: 238, title: "Quarantine contaminated worktrees automatically", axis: "sandboxed-worker-execution" },
    worker_id: "wkr_8c0a",
    worktree_path: ".forge/wt/238-quarantine",
    state: "QUARANTINED",
    repair_rounds: 1,
    lease_expires_at: null,
    heartbeat_at: iso(hrs(18)),
    cost_usd: 1.55,
    branch: "loop/238-quarantine",
    started_at: iso(hrs(20)),
    ended_at: iso(hrs(18)),
  },
];

// ── Pull Requests (8 total) ──────────────────────────────────────────
export const prs: PullRequest[] = [
  {
    number: 1287,
    title: "Tune critic sev2 categorisation by blast radius",
    branch: "loop/602-sev2-rules",
    additions: 123,
    deletions: 18,
    mergeable: false,
    state: "open",
    saga_id: "sg_602",
    labels: ["critic:blocking"],
    review: {
      verdict: "changes_requested",
      round: 1,
      suspicious: false,
      sev_counts: { sev1: 0, sev2: 2, sev3: 1 },
      findings: [
        { severity: "sev2", category: "test-coverage", file: "critic/reviewer.py", line: 214, message: "New sev2/sev3 split has no test for the blast-radius boundary (exactly 1 file changed)." },
        { severity: "sev2", category: "error-handling", file: "critic/reviewer.py", line: 188, message: "Broadened `except Exception` swallows categoriser errors — would silently downgrade a real sev1." },
        { severity: "sev3", category: "style", file: "tests/test_sev_rules.py", line: 12, message: "Table-driven cases would read cleaner than 9 near-identical asserts." },
      ],
      minimal_path_to_green: [
        { text: "Add boundary test at exactly 1 changed file", done: false },
        { text: "Narrow the except to CategoriserError", done: false },
        { text: "Re-run critic", done: false },
      ],
      history: [{ round: 1, sev2: 2 }],
    },
  },
  {
    number: 1284,
    title: "Promote failing-test-first into dispatch prompt",
    branch: "loop/305-test-first",
    additions: 44,
    deletions: 6,
    mergeable: false,
    state: "open",
    saga_id: "sg_305",
    labels: ["critic:blocking", "loop:auto-rescued"],
    review: {
      verdict: "changes_requested",
      round: 2,
      suspicious: false,
      sev_counts: { sev1: 0, sev2: 1, sev3: 0 },
      findings: [
        { severity: "sev2", category: "evidence", file: "prompts/dispatch.md", line: 31, message: "Prompt change asserts behaviour but ships no regression test proving the ordering shift." },
      ],
      minimal_path_to_green: [
        { text: "Add tests/test_dispatch_prompt.py asserting test-first ordering", done: true },
        { text: "Re-request critic", done: false },
      ],
      history: [{ round: 1, sev2: 3 }, { round: 2, sev2: 1 }],
    },
  },
  {
    number: 1281,
    title: "Default-deny network egress with per-axis allowlist",
    branch: "loop/224-egress",
    additions: 207,
    deletions: 41,
    mergeable: true,
    state: "open",
    saga_id: null,
    labels: ["critic:suspicious"],
    review: {
      verdict: "approved",
      round: 1,
      suspicious: true,
      suspicious_reason: "All tests pass, but coverage of the deny path is 0%. Holding for a human glance rather than auto-merging.",
      sev_counts: { sev1: 0, sev2: 0, sev3: 2 },
      findings: [
        { severity: "sev3", category: "smell", file: "sandbox/egress.py", line: 96, message: "Allowlist passes tests but is hard-coded; suspicious — should read from capability_policy, not a constant." },
        { severity: "sev3", category: "smell", file: "sandbox/egress.py", line: 140, message: "Default-deny path never exercised by a test; green run may be vacuous." },
      ],
      minimal_path_to_green: [],
      history: [{ round: 1, sev2: 0 }],
    },
  },
  {
    number: 1276,
    title: "Idempotent event replay on projection rebuild",
    branch: "loop/418-replay",
    additions: 88,
    deletions: 12,
    mergeable: true,
    state: "open",
    saga_id: null,
    labels: ["clean"],
    review: {
      verdict: "approved",
      round: 1,
      suspicious: false,
      sev_counts: { sev1: 0, sev2: 0, sev3: 0 },
      findings: [],
      minimal_path_to_green: [],
      history: [{ round: 1, sev2: 0 }],
    },
  },
  {
    number: 1272,
    title: "Confidence decay for episodic memory",
    branch: "loop/314-decay",
    additions: 61,
    deletions: 4,
    mergeable: true,
    state: "open",
    saga_id: null,
    labels: ["clean"],
    review: {
      verdict: "approved",
      round: 1,
      suspicious: false,
      sev_counts: { sev1: 0, sev2: 0, sev3: 0 },
      findings: [],
      minimal_path_to_green: [],
      history: [{ round: 1, sev2: 0 }],
    },
  },
  {
    number: 1268,
    title: "worker_logs MCP tool for live monologue tail",
    branch: "loop/151-logs",
    additions: 134,
    deletions: 9,
    mergeable: true,
    state: "open",
    saga_id: null,
    labels: ["clean"],
    review: {
      verdict: "approved",
      round: 1,
      suspicious: false,
      sev_counts: { sev1: 0, sev2: 0, sev3: 1 },
      findings: [
        { severity: "sev3", category: "style", file: "mcp/worker_logs.py", line: 22, message: "Consider streaming with a bounded buffer for very long monologues." },
      ],
      minimal_path_to_green: [],
      history: [{ round: 1, sev2: 0 }],
    },
  },
  {
    number: 1261,
    title: "Status endpoint: projections + lag + boot/doctor",
    branch: "loop/140-status",
    additions: 312,
    deletions: 47,
    mergeable: true,
    state: "merged",
    saga_id: "sg_140",
    labels: ["clean"],
    review: {
      verdict: "approved",
      round: 2,
      suspicious: false,
      sev_counts: { sev1: 0, sev2: 0, sev3: 0 },
      findings: [],
      minimal_path_to_green: [],
      history: [{ round: 1, sev2: 2 }, { round: 2, sev2: 0 }],
    },
  },
  {
    number: 1255,
    title: "Per-axis least-privilege secret scoping",
    branch: "loop/220-secrets",
    additions: 198,
    deletions: 22,
    mergeable: true,
    state: "merged",
    saga_id: "sg_220",
    labels: ["clean"],
    review: {
      verdict: "approved",
      round: 1,
      suspicious: false,
      sev_counts: { sev1: 0, sev2: 0, sev3: 0 },
      findings: [],
      minimal_path_to_green: [],
      history: [{ round: 1, sev2: 0 }],
    },
  },
];

// ── Memory (10 items) ────────────────────────────────────────────────
export const memory: Memory[] = [
  {
    id: "mem_a1",
    kind: "procedural",
    title: "Write the failing test before the fix",
    body: "For quality-axis issues, author a red test that captures the bug, then make it green. First-pass acceptance rose 12pts after adopting this on 602/305.",
    evidence_refs: ["PR #1261", "PR #1255"],
    created_at: iso(hrs(6)),
    confidence: 0.91,
  },
  {
    id: "mem_a2",
    kind: "procedural",
    title: "Carry repair_rounds across lease recovery",
    body: "When a stale lease is recovered, preserve repair_rounds and heartbeat — resetting to DISPATCHED loses convergence progress and double-counts cost.",
    evidence_refs: ["saga sg_412"],
    created_at: iso(hrs(2)),
    confidence: 0.84,
  },
  {
    id: "mem_a3",
    kind: "procedural",
    title: "Narrow excepts to typed errors",
    body: "Broad `except Exception` in the critic silently downgrades sev1s. Always catch the specific categoriser error.",
    evidence_refs: ["PR #1287"],
    created_at: iso(mins(40)),
    confidence: 0.78,
  },
  {
    id: "mem_b1",
    kind: "episodic",
    title: "Tick 41 quarantine cascade",
    body: "A shared worktree caused two tasks to contaminate each other; both quarantined. Root cause: single worktree experiment. Reverted; per-task worktrees restored.",
    evidence_refs: ["saga sg_238", "event #3981"],
    created_at: iso(hrs(18)),
    confidence: 0.95,
  },
  {
    id: "mem_b2",
    kind: "episodic",
    title: "Crashed worker held lease 40m",
    body: "Before heartbeats, a crashed worker held its lease until manual intervention. Motivated the 90s heartbeat lease and stale-lease recovery.",
    evidence_refs: ["event #2204"],
    created_at: iso(hrs(31)),
    confidence: 0.9,
  },
  {
    id: "mem_b3",
    kind: "episodic",
    title: "Vision-drift detector abandoned",
    body: "Worker spent 3 repair rounds and $4.88 without converging on a drift metric; critic kept flagging the metric as unfalsifiable. Abandoned; reframed as open question.",
    evidence_refs: ["saga sg_515"],
    created_at: iso(hrs(13)),
    confidence: 0.72,
  },
  {
    id: "mem_c1",
    kind: "rejected_path",
    title: "Worker self-merge of clean PRs",
    body: "Tempting for lead time, but removes the critic gate that makes self-improvement falsifiable. Rejected.",
    evidence_refs: ["decision D-118"],
    superseded_by: null,
    created_at: iso(hrs(20)),
    confidence: 0.88,
  },
  {
    id: "mem_c2",
    kind: "rejected_path",
    title: "Single shared worktree",
    body: "Disk savings not worth cross-task contamination. Rejected after tick 41.",
    evidence_refs: ["mem_b1"],
    created_at: iso(hrs(17)),
    confidence: 0.93,
  },
  {
    id: "mem_d1",
    kind: "procedural",
    title: "Reset saga to DISPATCHED on recovery",
    body: "Original recovery strategy. Lost convergence progress.",
    evidence_refs: ["saga sg_412"],
    superseded_by: "mem_a2",
    created_at: iso(hrs(30)),
    confidence: 0.3,
  },
  {
    id: "mem_d2",
    kind: "episodic",
    title: "Budget endpoint needed 2 rounds",
    body: "Cost time-series math was off by the compaction window; critic caught a double-count. Converged round 2.",
    evidence_refs: ["PR #1261", "saga sg_144"],
    created_at: iso(hrs(6)),
    confidence: 0.8,
  },
];

// ── Manifestos (7 rules) ─────────────────────────────────────────────
export const manifestos: ManifestoRule[] = [
  { id: "Q-1", manifesto: "quality", rule: "No broadened `except` clauses that swallow typed errors.", severity: "sev2", rationale: "Hides sev1 regressions behind a green run; directly caused a critic miss on #1287.", source_pr: "#1287" },
  { id: "Q-2", manifesto: "quality", rule: "No PR merges on a changes_requested verdict.", severity: "sev1", rationale: "The critic gate is the mechanism that makes self-improvement falsifiable.", source_pr: null },
  { id: "Q-3", manifesto: "quality", rule: "Block merge on any unresolved sev1.", severity: "sev1", rationale: "Sev1 = correctness or safety. Non-negotiable.", source_pr: null },
  { id: "Q-4", manifesto: "quality", rule: "Capability grants are least-privilege per axis; egress default-deny.", severity: "sev2", rationale: "Limits blast radius of a misbehaving worker.", source_pr: "#1255" },
  { id: "T-1", manifesto: "testing", rule: "Every behaviour change ships a regression test that was red before the fix.", severity: "sev2", rationale: "Failing-test-first; proves the change does what it claims. Raised first-pass acceptance 12pts.", source_pr: "#1261" },
  { id: "T-2", manifesto: "testing", rule: "No skipped or xfail tests in a merged PR.", severity: "sev2", rationale: "A skipped test is a silent coverage hole.", source_pr: null },
  { id: "T-3", manifesto: "testing", rule: "Default-deny / error paths must be exercised by a test.", severity: "sev3", rationale: "Untested deny paths make green runs vacuous (see suspicious #1281).", source_pr: "#1281" },
];

// ── Budget (24-point time series) ────────────────────────────────────
const budgetPoints: BudgetPoint[] = (() => {
  const pts: BudgetPoint[] = [];
  let cum = 184.2;
  for (let i = 23; i >= 0; i--) {
    // Reproduce the data.jsx formula (sin + fixed addend; Random excluded for determinism)
    const spend = +(2.1 + Math.sin(i / 3) * 1.4 + 0.3).toFixed(2); // 0.3 ≈ mid of Math.random()*0.6
    cum += spend;
    pts.push({ t: iso(hrs(i)), hourly: spend, cumulative: +cum.toFixed(2), tokens: Math.round(spend * 95000) });
  }
  return pts;
})();

export const budget: Budget = {
  points: budgetPoints,
  spend_today: 48.9,
  cumulative: budgetPoints[budgetPoints.length - 1].cumulative,
  tokens_today: 4_640_000,
  cost_per_merged_pr: 3.18,
};

// ── Pipeline stages ──────────────────────────────────────────────────
export const pipeline: PipelineStage[] = [
  { stage: "plan", role: "planner", model: MODEL, status: "ok" },
  { stage: "dispatch", role: "dispatcher", model: "—", status: "ok" },
  { stage: "execute", role: "worker", model: MODEL, status: "ok" },
  { stage: "critique", role: "critic", model: MODEL, status: "ok" },
  { stage: "merge", role: "merger", model: "—", status: "ok" },
  { stage: "learn", role: "librarian", model: MODEL, status: "ok" },
];

// ── Event-log seed (~400+ events) ────────────────────────────────────
export function seedEvents(): EventEnvelope[] {
  let _eid = 0;
  const eid = () => "evt_" + (1000 + _eid++).toString(36);

  const list: EventEnvelope[] = [];
  let seq = 3800;

  const push = (kind: EventKind, over: Partial<Omit<EventEnvelope, "sequence" | "event_id" | "kind" | "occurred_at">> & { payload?: Record<string, unknown>; occurred_at?: string } = {}): EventEnvelope => {
    const { payload = {}, occurred_at, ...rest } = over;
    const e: EventEnvelope = {
      sequence: ++seq,
      event_id: eid(),
      kind,
      occurred_at: occurred_at ?? iso(hrs(40) - (seq - 3800) * 18000),
      payload,
      ...rest,
    } as EventEnvelope;
    list.push(e);
    return e;
  };

  const sagaSeeds = [
    { sg: "sg_140", num: 140, title: "Status endpoint", axis: "self-dogfood-operability", merged: true, rounds: 2, terminal: "" },
    { sg: "sg_220", num: 220, title: "Secret scoping", axis: "sandboxed-worker-execution", merged: true, rounds: 1, terminal: "" },
    { sg: "sg_144", num: 144, title: "Budget endpoint", axis: "self-dogfood-operability", merged: true, rounds: 2, terminal: "" },
    { sg: "sg_229", num: 229, title: "Reap audit", axis: "sandboxed-worker-execution", merged: false, rounds: 2, terminal: "task.compensated" },
    { sg: "sg_515", num: 515, title: "Vision drift", axis: "frontier-generation", merged: false, rounds: 3, terminal: "task.failed" },
  ] as const;

  for (let tick = 39; tick <= 41; tick++) {
    const t0 = push("tick.started", { payload: { tick } });
    const inThisTick = sagaSeeds.slice((tick - 39), (tick - 39) + 2);

    inThisTick.forEach((s) => {
      const planned = push("task.planned", { saga_id: s.sg, task_id: s.sg, causal_event_id: t0.event_id, payload: { issue: s.num, title: s.title, axis: s.axis } });
      const disp = push("task.dispatched", { saga_id: s.sg, task_id: s.sg, causal_event_id: planned.event_id, payload: { issue: s.num, worker: "wkr_" + s.sg.slice(3), worktree: ".forge/wt/" + s.num } });

      for (let h = 0; h < 3; h++) {
        push("task.heartbeat", { saga_id: s.sg, task_id: s.sg, causal_event_id: disp.event_id, payload: { cost_usd: +(0.4 * (h + 1)).toFixed(2) } });
      }

      push("worker.observation", { saga_id: s.sg, task_id: s.sg, causal_event_id: disp.event_id, payload: { note: "Wrote failing test first per D-124." } });

      const pr = push("pr.opened", { saga_id: s.sg, task_id: s.sg, causal_event_id: disp.event_id, payload: { pr: 1200 + s.num % 100, title: s.title, additions: 120 + s.num % 90, deletions: 12 } });

      for (let r = 1; r <= s.rounds; r++) {
        const verdictForRound = r < s.rounds ? "changes_requested" : (s.merged ? "approved" : "changes_requested");
        const sev2Count = s.merged ? Math.max(0, 3 - r) : 2;
        const crit = push("critique.issued", {
          saga_id: s.sg, task_id: s.sg, causal_event_id: pr.event_id,
          payload: { pr: 1200 + s.num % 100, round: r, verdict: verdictForRound, sev2: sev2Count },
        });
        if (r < s.rounds || !s.merged) {
          push("worker.observation", { saga_id: s.sg, task_id: s.sg, causal_event_id: crit.event_id, payload: { note: "Addressing minimal path to green." } });
        }
      }

      if (s.merged) {
        const dec = push("decision.made", { saga_id: s.sg, causal_event_id: pr.event_id, payload: { decision: "Auto-merge: verdict approved, 0 sev1/sev2." } });
        push("pr.merged", { saga_id: s.sg, task_id: s.sg, causal_event_id: dec.event_id, payload: { pr: 1200 + s.num % 100, branch: "loop/" + s.num, cost_usd: +(1.5 + s.num % 3).toFixed(2) } });
        push("task.completed", { saga_id: s.sg, task_id: s.sg, payload: { issue: s.num } });
        push("memory.promoted", { saga_id: s.sg, payload: { memory: "procedural", title: "Failing-test-first" } });
        push("worktree.reaped", { saga_id: s.sg, payload: { worktree: ".forge/wt/" + s.num } });
      } else if (s.terminal === "task.failed") {
        push("merge.blocked", { saga_id: s.sg, payload: { pr: 1200 + s.num % 100, reason: "Critic could not converge: metric unfalsifiable." } });
        push("idea.rejected", { saga_id: s.sg, payload: { idea: "Vision-drift metric", reason: "Unfalsifiable." } });
        push("task.failed", { saga_id: s.sg, task_id: s.sg, payload: { issue: s.num, reason: "Abandoned after 3 rounds." } });
        push("worktree.reaped", { saga_id: s.sg, payload: { worktree: ".forge/wt/" + s.num } });
      } else {
        push("task.compensated", { saga_id: s.sg, task_id: s.sg, payload: { issue: s.num, reason: "Rolled back contaminated worktree." } });
        push("worktree.reaped", { saga_id: s.sg, payload: { worktree: ".forge/wt/" + s.num } });
      }
    });

    if (tick === 40) push("compaction.performed", { payload: { from_seq: seq - 120, to_seq: seq - 40, freed: "82 events folded" } });
    if (tick === 41) push("frontier.advanced", { payload: { version: 47, next_expansion: "Promote failing-test-first into dispatch prompt." } });
    if (tick === 40) push("vision.updated", { payload: { version: 46, note: "Sharpened KR to 80% sustained over 20 merges." } });
    if (tick === 41) push("memory.superseded", { payload: { old: "Reset saga on recovery", by: "Carry repair_rounds across recovery" } });

    push("tick.completed", { payload: { tick, merged: inThisTick.filter((x) => x.merged).length } });
  }

  // Pad with realistic filler heartbeats/observations to ~400
  while (list.length < 400) {
    const s = sagaSeeds[list.length % sagaSeeds.length];
    const kind: EventKind = list.length % 5 === 0 ? "worker.observation" : "task.heartbeat";
    if (kind === "worker.observation") {
      push("worker.observation", { saga_id: s.sg, task_id: s.sg, payload: { note: "Still working." } });
    } else {
      push("task.heartbeat", { saga_id: s.sg, task_id: s.sg, payload: { cost_usd: +(Math.random()).toFixed(2) } });
    }
  }

  // Tick 42 — current live sagas tail
  const lt = push("tick.started", { payload: { tick: 42 } });
  const liveSagas = [
    { sg: "sg_412", num: 412, wkr: workers[0] },
    { sg: "sg_602", num: 602, wkr: workers[1] },
    { sg: "sg_305", num: 305, wkr: workers[2] },
  ] as const;

  liveSagas.forEach(({ sg, num, wkr }) => {
    const d = push("task.dispatched", {
      saga_id: sg, task_id: sg, causal_event_id: lt.event_id,
      payload: { issue: num, worker: wkr.id, worktree: wkr.worktree_path },
    });
    push("task.heartbeat", { saga_id: sg, task_id: sg, causal_event_id: d.event_id, payload: { cost_usd: wkr.cost_usd } });
  });

  push("pr.opened", { saga_id: "sg_602", task_id: "sg_602", payload: { pr: 1287, title: "Tune critic sev2 categorisation", additions: 123, deletions: 18 } });
  push("critique.issued", { saga_id: "sg_602", task_id: "sg_602", payload: { pr: 1287, round: 1, verdict: "changes_requested", sev2: 2 } });
  push("critique.issued", { saga_id: "sg_305", task_id: "sg_305", payload: { pr: 1284, round: 1, verdict: "changes_requested", sev2: 3 } });

  // Make the live tail read fresh: last ~18 events span the past few minutes
  const tailN = Math.min(18, list.length);
  for (let i = 0; i < tailN; i++) {
    const e = list[list.length - tailN + i];
    e.occurred_at = iso((tailN - i) * 14000 + Math.floor(Math.random() * 4000));
  }

  // Re-number sequences sequentially
  list.forEach((e, i) => { e.sequence = 3801 + i; });

  return list;
}
