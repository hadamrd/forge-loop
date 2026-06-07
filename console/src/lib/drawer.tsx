// Drawer + navigation context. Mirrors the prototype's single `nav` object (open / close / go)
// so screens port over almost unchanged:
//   const nav = useNav();
//   nav.open("worker", worker);   // right-side detail drawer
//   nav.go("scorecard");          // route navigation (id → path)
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "@tanstack/react-router";

export type DrawerKind = "event" | "worker" | "pr" | "saga";
export interface DrawerState {
  kind: DrawerKind;
  data: unknown;
}

interface DrawerCtx {
  current: DrawerState | null;
  open: (kind: DrawerKind, data: unknown) => void;
  close: () => void;
}

const Ctx = createContext<DrawerCtx | null>(null);

export function DrawerProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<DrawerState | null>(null);
  const value = useMemo<DrawerCtx>(
    () => ({ current, open: (kind, data) => setCurrent({ kind, data }), close: () => setCurrent(null) }),
    [current],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDrawerState(): DrawerCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useDrawerState must be used within <DrawerProvider>");
  return c;
}

const idToPath = (id: string) => (id === "overview" ? "/" : `/${id}`);

/** Unified nav for screens: drawer controls + route navigation by screen id. */
export function useNav() {
  const drawer = useDrawerState();
  const navigate = useNavigate();
  return useMemo(
    () => ({
      ...drawer,
      go: (id: string) => {
        drawer.close();
        void navigate({ to: idToPath(id) });
        document.querySelector(".content")?.scrollTo(0, 0);
      },
    }),
    [drawer, navigate],
  );
}
