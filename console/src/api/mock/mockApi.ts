// src/api/mock/mockApi.ts
// Implements ForgeApi over the seed, with 200–600ms artificial latency and an SSE-like live
// ticker. The ticker emits a new event every 1.5–3s, cycling through a scripted tick so the UI
// feels alive: heartbeat → pr.opened → critique → pr.merged → tick.completed → tick.started …
// (Faithful to the prototype's store.jsx `liveScript`.) The ticker always runs; the UI's "pause"
// only freezes rendering (see lib/ui.tsx), so no events are lost.
import type { ForgeApi } from "../client";
import type { EventEnvelope, EventKind, EventQuery, EventPage } from "../../domain/events";
import type { LoopStatus } from "../../domain/models";
import * as seed from "./seed";

const latency = <T>(value: T) =>
  new Promise<T>((r) => setTimeout(() => r(value), 200 + Math.random() * 400));

// Scripted live cycle. Each step's payload() is evaluated at emit time.
type Step = { kind: EventKind; saga_id?: string; causal?: boolean; merge?: boolean; payload?: () => Record<string, unknown> };
const LIVE_SCRIPT: Step[] = [
  { kind: "task.heartbeat", saga_id: "sg_412", payload: () => ({ cost_usd: +(1.9 + Math.random() * 0.3).toFixed(2) }) },
  { kind: "worker.observation", saga_id: "sg_412", payload: () => ({ note: "Full saga suite green — 41 passed. Opening PR." }) },
  { kind: "pr.opened", saga_id: "sg_412", payload: () => ({ pr: 1289, title: "Recover stale leases without dropping in-flight sagas", additions: 52, deletions: 6 }) },
  { kind: "critique.issued", saga_id: "sg_412", causal: true, payload: () => ({ pr: 1289, round: 1, verdict: "approved", sev2: 0 }) },
  { kind: "decision.made", saga_id: "sg_412", payload: () => ({ decision: "Auto-merge: approved, 0 sev1/sev2." }) },
  { kind: "pr.merged", saga_id: "sg_412", causal: true, merge: true, payload: () => ({ pr: 1289, branch: "loop/412-stale-leases", cost_usd: 1.98 }) },
  { kind: "task.completed", saga_id: "sg_412", causal: true, payload: () => ({ issue: 412 }) },
  { kind: "memory.promoted", saga_id: "sg_412", payload: () => ({ memory: "procedural", title: "Carry repair_rounds across lease recovery" }) },
  { kind: "worktree.reaped", saga_id: "sg_412", payload: () => ({ worktree: ".forge/wt/412-stale-leases" }) },
  { kind: "task.heartbeat", saga_id: "sg_305", payload: () => ({ cost_usd: +(2.4 + Math.random() * 0.2).toFixed(2) }) },
  { kind: "critique.issued", saga_id: "sg_305", payload: () => ({ pr: 1284, round: 2, verdict: "approved", sev2: 0 }) },
  { kind: "task.heartbeat", saga_id: "sg_602", payload: () => ({ cost_usd: +(3.0 + Math.random() * 0.2).toFixed(2) }) },
  { kind: "frontier.advanced", payload: () => ({ version: 48, next_expansion: "Measure first-pass acceptance over next 20 merges." }) },
  { kind: "tick.completed", payload: () => ({ tick: 42, merged: 1 }) },
  { kind: "tick.started", payload: () => ({ tick: 43 }) },
  { kind: "task.planned", saga_id: "sg_430", payload: () => ({ issue: 430, title: "Crash-recovery doctor", axis: "durable-control-plane" }) },
  { kind: "task.dispatched", saga_id: "sg_430", payload: () => ({ issue: 430, worker: "wkr_b22f", worktree: ".forge/wt/430-doctor" }) },
];

export function createMockApi(): ForgeApi {
  let events = seed.seedEvents();
  let seq = events.length ? events[events.length - 1].sequence : 4200;
  let mergesToday = 4;

  const subscribers = new Set<(e: EventEnvelope) => void>();
  let lastId = events.at(-1)?.event_id;
  let si = 0;

  const emit = () => {
    const step = LIVE_SCRIPT[si++ % LIVE_SCRIPT.length];
    const e = {
      sequence: ++seq,
      event_id: "evt_live_" + seq.toString(36),
      kind: step.kind,
      occurred_at: new Date().toISOString(),
      saga_id: step.saga_id,
      task_id: step.saga_id,
      causal_event_id: step.causal ? lastId : undefined,
      payload: (step.payload ? step.payload() : {}) as never,
    } as EventEnvelope;
    lastId = e.event_id;
    if (step.merge) mergesToday += 1;
    events = events.concat(e).slice(-600);
    subscribers.forEach((fn) => fn(e));
    schedule();
  };
  let timer: ReturnType<typeof setTimeout> | undefined;
  const schedule = () => {
    timer = setTimeout(emit, 1500 + Math.random() * 1700);
  };
  schedule();
  void timer;

  return {
    getLoopStatus: () => latency(buildStatus(seq, mergesToday)),
    getPipeline: () => latency(seed.pipeline),
    getRoles: () => latency(seed.pipeline),
    getBudget: () => latency(seed.budget),

    streamEvents(sinceSeq, onEvent) {
      events.filter((e) => e.sequence > sinceSeq).forEach(onEvent);
      subscribers.add(onEvent);
      return () => subscribers.delete(onEvent);
    },
    getEvents: (q: EventQuery) => {
      let list = events;
      if (q.kinds?.length) list = list.filter((e) => q.kinds!.includes(e.kind));
      if (q.taskId) list = list.filter((e) => e.task_id === q.taskId);
      if (q.sagaId) list = list.filter((e) => e.saga_id === q.sagaId);
      const limit = q.limit ?? 400;
      const end = q.cursor ?? list.length;
      const start = Math.max(0, end - limit);
      const page: EventPage = {
        events: list.slice(start, end),
        cursor: start > 0 ? start : null,
        has_more: start > 0,
      };
      return latency(page);
    },

    getWorkers: () => latency(seed.workers),
    getWorkerLog: (id) => latency(seed.workers.find((w) => w.id === id)?.monologue ?? []),
    killWorker: (id) =>
      latency(undefined).then(() => {
        seed.workers.forEach((w) => {
          if (w.id === id) (w as { state: string }).state = "ABANDONED";
        });
      }),

    getPRs: () => latency(seed.prs),
    getCriticReview: (pr) => latency(seed.prs.find((p) => p.number === pr)!.review),

    getSagas: () => latency(seed.sagas),
    getAttempts: (issue) =>
      latency(events.filter((e) => (e.payload as { issue?: number })?.issue === issue)),

    getScorecard: () => latency(seed.scorecard),
    getFrontier: () => latency(seed.frontier),
    getMemory: () => latency(seed.memory),

    getBacklog: () => latency(seed.backlog),
    getManifestos: () => latency(seed.manifestos),
  };
}

function buildStatus(seq: number, _mergesToday: number): LoopStatus {
  return {
    summary: "Loop healthy — tick 42 in progress, 3 workers in flight.",
    available: true,
    sequence: seq,
    last_sequence: seq,
    lag: 0,
    active_count: 3,
    in_flight_count: 3,
    stale_lease_count: 0,
    rejected_count: 1,
    halted: false,
    boot: { booted_at: seed.iso(40 * 3_600_000), doctor: "ok", version: "forge 0.9.2", env: "ok" },
    event_log: { path: ".forge/events.jsonl", sequence: seq, size_mb: 14.2 },
    projections: [
      { name: "tasks", sequence: seq, lag: 0 },
      { name: "workers", sequence: seq - 1, lag: 1 },
      { name: "scorecard", sequence: seq - 4, lag: 4 },
      { name: "frontier", sequence: seq, lag: 0 },
      { name: "budget", sequence: seq - 2, lag: 2 },
      { name: "memory", sequence: seq - 9, lag: 9 },
    ],
    frontier: {
      current_problem: seed.frontier.current_problem,
      next_expansion: seed.frontier.next_expansion,
      version: seed.frontier.version,
    },
    memory: { total: seed.memory.length, promoted_today: 3, superseded: 1 },
    operational_entropy: {
      open_branches: 7,
      live_worktrees: 3,
      open_epics: 2,
      backlog_age_days: 9,
    },
    tasks: seed.sagas,
  };
}
