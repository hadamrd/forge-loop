// src/domain/events.ts
// The live event stream is the spine of the app. These types mirror the real backend
// event-log envelopes 1:1 so the real SSE feed drops in unchanged.

export type EventKind =
  | "tick.started" | "tick.completed"
  | "task.planned" | "task.dispatched" | "task.heartbeat" | "task.completed" | "task.failed" | "task.compensated"
  | "pr.opened" | "pr.merged" | "merge.blocked"
  | "critique.issued"
  | "worktree.reaped"
  | "frontier.advanced" | "idea.rejected" | "decision.made"
  | "memory.promoted" | "memory.superseded"
  | "compaction.performed"
  | "vision.updated"
  | "worker.observation"
  | "loop.halted";

/** Per-kind payloads. Loose by design — the UI reads optional fields defensively. */
export interface EventPayloads {
  "tick.started": { tick: number };
  "tick.completed": { tick: number; merged: number };
  "task.planned": { issue: number; title?: string; axis?: string };
  "task.dispatched": { issue: number; worker: string; worktree: string };
  "task.heartbeat": { cost_usd: number };
  "task.completed": { issue: number };
  "task.failed": { issue?: number; reason: string; worker?: string };
  "task.compensated": { issue?: number; reason: string };
  "pr.opened": { pr: number; title?: string; additions?: number; deletions?: number };
  "pr.merged": { pr: number; branch: string; cost_usd: number };
  "merge.blocked": { pr: number; reason: string };
  "critique.issued": { pr: number; round: number; verdict: CriticVerdict; sev2: number };
  "worktree.reaped": { worktree: string };
  "frontier.advanced": { version: number; next_expansion?: string };
  "idea.rejected": { idea: string; reason: string };
  "decision.made": { decision: string };
  "memory.promoted": { memory: string; title: string };
  "memory.superseded": { old: string; by: string };
  "compaction.performed": { from_seq?: number; to_seq?: number; freed: string };
  "vision.updated": { version: number; note?: string };
  "worker.observation": { note: string };
  "loop.halted": { reason: string };
}

export type CriticVerdict = "approved" | "changes_requested" | "error";

export interface EventEnvelope<K extends EventKind = EventKind> {
  sequence: number;
  event_id: string;
  kind: K;
  occurred_at: string;          // ISO 8601
  task_id?: string;
  saga_id?: string;
  causal_event_id?: string;     // walk this back to build the causal chain
  payload: K extends keyof EventPayloads ? EventPayloads[K] : Record<string, unknown>;
}

export interface EventPage {
  events: EventEnvelope[];
  cursor: number | null;        // pass back as `since` for the next page
  has_more: boolean;
}

export interface EventQuery {
  kinds?: EventKind[];
  taskId?: string;
  sagaId?: string;
  limit?: number;
  cursor?: number;
}
