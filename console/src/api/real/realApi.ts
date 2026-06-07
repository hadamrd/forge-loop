// src/api/real/realApi.ts
// STUB real implementation — same ForgeApi interface, hitting the documented REST/SSE routes.
// Implement these fetch() bodies against the live backend, then flip createApi('real'). That's the swap.
//
// Base URL + auth come from env. Every method maps to exactly one documented route/tool (see PORTING.md).

import type { ForgeApi } from "../client";
import type { EventEnvelope, EventPage, EventQuery } from "../../domain/events";
import type {
  LoopStatus, Worker, PullRequest, CriticReview, Saga, Scorecard,
  Frontier, Memory, Issue, ManifestoRule, Budget, PipelineStage, MonologueLine,
} from "../../domain/models";

const BASE = (import.meta as any).env?.VITE_FORGE_BASE_URL ?? "/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json() as Promise<T>;
}
async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json().catch(() => undefined) as Promise<T>;
}

export function createRealApi(): ForgeApi {
  return {
    getLoopStatus: () => get<LoopStatus>("/status"),
    getPipeline: () => get<PipelineStage[]>("/pipeline"),
    getRoles: () => get<PipelineStage[]>("/roles"),
    getBudget: () => get<Budget>("/budget"),

    // SSE — connect to /events/stream?since=<seq>, parse EventEnvelope per message, return unsubscribe.
    streamEvents(sinceSeq, onEvent) {
      const es = new EventSource(`${BASE}/events/stream?since=${sinceSeq}`);
      es.onmessage = (msg) => { try { onEvent(JSON.parse(msg.data) as EventEnvelope); } catch { /* ignore */ } };
      return () => es.close();
    },
    getEvents: (q: EventQuery) => {
      const p = new URLSearchParams();
      if (q.kinds?.length) p.set("kinds", q.kinds.join(","));
      if (q.taskId) p.set("task_id", q.taskId);
      if (q.sagaId) p.set("saga_id", q.sagaId);
      if (q.limit) p.set("limit", String(q.limit));
      if (q.cursor != null) p.set("cursor", String(q.cursor));
      return get<EventPage>(`/events?${p.toString()}`);
    },

    getWorkers: () => get<Worker[]>("/workers"),
    getWorkerLog: (id) => get<MonologueLine[]>(`/workers/${id}/logs`),
    killWorker: (id) => post<void>(`/workers/${id}/kill`),

    getPRs: () => get<PullRequest[]>("/prs"),
    getCriticReview: (pr) => get<CriticReview>(`/prs/${pr}/critic`),

    getSagas: () => get<Saga[]>("/sagas"),
    getAttempts: (issue) => get<EventEnvelope[]>(`/issues/${issue}/attempts`),

    getScorecard: () => get<Scorecard>("/scorecard"),
    getFrontier: () => get<Frontier>("/frontier"),
    getMemory: () => get<Memory[]>("/memory"),

    getBacklog: () => get<Issue[]>("/backlog"),
    getManifestos: () => get<ManifestoRule[]>("/manifestos"),
  };
}
