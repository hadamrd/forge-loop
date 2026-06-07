// src/lib/queryKeys.ts — one place for every query key, so invalidation is greppable.
export const qk = {
  loopStatus: ["loopStatus"] as const,
  pipeline: ["pipeline"] as const,
  roles: ["roles"] as const,
  budget: ["budget"] as const,
  events: (q?: object) => ["events", q ?? {}] as const,
  workers: ["workers"] as const,
  workerLog: (id: string) => ["workerLog", id] as const,
  prs: ["prs"] as const,
  criticReview: (pr: number) => ["criticReview", pr] as const,
  sagas: ["sagas"] as const,
  attempts: (issue: number) => ["attempts", issue] as const,
  scorecard: ["scorecard"] as const,
  frontier: ["frontier"] as const,
  memory: ["memory"] as const,
  backlog: ["backlog"] as const,
  manifestos: ["manifestos"] as const,
};
