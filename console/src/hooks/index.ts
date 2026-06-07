// src/hooks/index.ts — one TanStack Query hook per resource. Components import ONLY these,
// never the api directly. The live event stream is wired into the query cache via streamEvents.

import { useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useApi } from "../api/ApiProvider";
import { qk } from "../lib/queryKeys";
import type { EventEnvelope, EventQuery } from "../domain/events";

export const useLoopStatus = () => { const api = useApi(); return useQuery({ queryKey: qk.loopStatus, queryFn: () => api.getLoopStatus(), refetchInterval: 5_000 }); };
export const usePipeline = () => { const api = useApi(); return useQuery({ queryKey: qk.pipeline, queryFn: () => api.getPipeline() }); };
export const useBudget = () => { const api = useApi(); return useQuery({ queryKey: qk.budget, queryFn: () => api.getBudget() }); };
export const useWorkers = () => { const api = useApi(); return useQuery({ queryKey: qk.workers, queryFn: () => api.getWorkers(), refetchInterval: 5_000 }); };
export const useWorkerLog = (id: string) => { const api = useApi(); return useQuery({ queryKey: qk.workerLog(id), queryFn: () => api.getWorkerLog(id), enabled: !!id }); };
export const usePRs = () => { const api = useApi(); return useQuery({ queryKey: qk.prs, queryFn: () => api.getPRs() }); };
export const useCriticReview = (pr: number) => { const api = useApi(); return useQuery({ queryKey: qk.criticReview(pr), queryFn: () => api.getCriticReview(pr), enabled: !!pr }); };
export const useSagas = () => { const api = useApi(); return useQuery({ queryKey: qk.sagas, queryFn: () => api.getSagas() }); };
export const useAttempts = (issue: number) => { const api = useApi(); return useQuery({ queryKey: qk.attempts(issue), queryFn: () => api.getAttempts(issue), enabled: !!issue }); };
export const useScorecard = () => { const api = useApi(); return useQuery({ queryKey: qk.scorecard, queryFn: () => api.getScorecard() }); };
export const useFrontier = () => { const api = useApi(); return useQuery({ queryKey: qk.frontier, queryFn: () => api.getFrontier() }); };
export const useMemory = () => { const api = useApi(); return useQuery({ queryKey: qk.memory, queryFn: () => api.getMemory() }); };
export const useBacklog = () => { const api = useApi(); return useQuery({ queryKey: qk.backlog, queryFn: () => api.getBacklog() }); };
export const useManifestos = () => { const api = useApi(); return useQuery({ queryKey: qk.manifestos, queryFn: () => api.getManifestos() }); };

export const useKillWorker = () => {
  const api = useApi(); const qc = useQueryClient();
  return useMutation({ mutationFn: (id: string) => api.killWorker(id), onSuccess: () => qc.invalidateQueries({ queryKey: qk.workers }) });
};

/**
 * Live event feed. Seeds from a paged fetch, then subscribes to streamEvents and pushes new
 * envelopes into the query cache. Returns the rolling list (bounded to `cap`).
 */
export function useEvents(query: EventQuery = { limit: 400 }, cap = 600) {
  const api = useApi();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: qk.events(query), queryFn: () => api.getEvents(query) });

  useEffect(() => {
    const since = q.data?.events.at(-1)?.sequence ?? 0;
    const unsub = api.streamEvents(since, (e: EventEnvelope) => {
      qc.setQueryData(qk.events(query), (prev: any) => {
        if (!prev) return { events: [e], cursor: null, has_more: false };
        const events = prev.events.concat(e).slice(-cap);
        return { ...prev, events };
      });
      // keep dependent views fresh
      if (e.kind === "pr.merged" || e.kind === "task.completed") qc.invalidateQueries({ queryKey: qk.loopStatus });
    });
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, q.data?.events?.length === undefined]);

  return { ...q, events: (q.data?.events ?? []) as EventEnvelope[] };
}
