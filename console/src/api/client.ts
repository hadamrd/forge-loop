// src/api/client.ts
// THE single interface the whole UI depends on. Hooks talk only to this; components never see it.
// Swapping mock → real is one line: change createApi()'s branch (or the VITE_FORGE_API env flag).

import type { EventEnvelope, EventPage, EventQuery } from "../domain/events";
import type {
  LoopStatus, Worker, PullRequest, CriticReview, Saga, Scorecard,
  Frontier, Memory, Issue, ManifestoRule, Budget, PipelineStage, MonologueLine,
} from "../domain/models";

export interface ForgeApi {
  // status / health
  getLoopStatus(): Promise<LoopStatus>;                              // GET /status · mcp loop_status
  getPipeline(): Promise<PipelineStage[]>;                           // GET /pipeline
  getRoles(): Promise<PipelineStage[]>;                              // GET /roles
  getBudget(): Promise<Budget>;                                      // GET /budget

  // events — the spine
  streamEvents(sinceSeq: number, onEvent: (e: EventEnvelope) => void): () => void; // GET /events/stream (SSE) → returns unsubscribe
  getEvents(query: EventQuery): Promise<EventPage>;                  // mcp events_query / events_recent

  // workers
  getWorkers(): Promise<Worker[]>;                                   // GET /workers
  getWorkerLog(id: string): Promise<MonologueLine[]>;                // mcp worker_logs
  killWorker(id: string): Promise<void>;                             // POST /workers/{id}/kill

  // PRs + critic
  getPRs(): Promise<PullRequest[]>;
  getCriticReview(pr: number): Promise<CriticReview>;                // mcp critic_review_pr

  // sagas
  getSagas(): Promise<Saga[]>;
  getAttempts(issue: number): Promise<EventEnvelope[]>;              // mcp attempts_history

  // self-improvement
  getScorecard(): Promise<Scorecard>;
  getFrontier(): Promise<Frontier>;
  getMemory(): Promise<Memory[]>;

  // planning
  getBacklog(): Promise<Issue[]>;
  getManifestos(): Promise<ManifestoRule[]>;
}

export type ApiMode = "mock" | "real";

import { createMockApi } from "./mock/mockApi";
import { createRealApi } from "./real/realApi";

export function createApi(mode: ApiMode = (import.meta as any).env?.VITE_FORGE_API ?? "mock"): ForgeApi {
  return mode === "real" ? createRealApi() : createMockApi();
}
