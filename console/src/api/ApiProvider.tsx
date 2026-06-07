// src/api/ApiProvider.tsx — provides the ONE ForgeApi instance + a QueryClient.
// Swapping mock→real happens here (or via VITE_FORGE_API). Components/hooks read the api from context.

import React, { createContext, useContext, useMemo } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createApi, type ForgeApi, type ApiMode } from "./client";

const ApiContext = createContext<ForgeApi | null>(null);
export const useApi = (): ForgeApi => {
  const api = useContext(ApiContext);
  if (!api) throw new Error("useApi must be used within <ApiProvider>");
  return api;
};

export function ApiProvider({ mode, children }: { mode?: ApiMode; children: React.ReactNode }) {
  const api = useMemo(() => createApi(mode), [mode]);
  const client = useMemo(
    () => new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 } } }),
    []
  );
  return (
    <ApiContext.Provider value={api}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ApiContext.Provider>
  );
}
