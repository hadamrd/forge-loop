// UiProvider — client-only console state that isn't server data:
//   • `now`     a ticking clock so relative timestamps stay fresh between events
//   • `paused`  freezes the live view (the topbar Live/Paused toggle + the stream tail)
//
// The mock/real event ticker always runs (data stays correct); `paused` only freezes
// what the UI renders, so no events are ever lost — you just stop the motion.
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

interface UiState {
  now: number;
  paused: boolean;
  setPaused: (p: boolean) => void;
}

const UiContext = createContext<UiState | null>(null);

export function UiProvider({ children }: { children: ReactNode }) {
  const [paused, setPaused] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (paused) return;
    const id = setInterval(() => setNow(Date.now()), 3000);
    return () => clearInterval(id);
  }, [paused]);

  const value = useMemo<UiState>(() => ({ now, paused, setPaused }), [now, paused]);
  return <UiContext.Provider value={value}>{children}</UiContext.Provider>;
}

export function useUi(): UiState {
  const ctx = useContext(UiContext);
  if (!ctx) throw new Error("useUi must be used within <UiProvider>");
  return ctx;
}

/** Convenience: just the ticking clock, for relativeTime(...) refresh. */
export const useNow = () => useUi().now;
