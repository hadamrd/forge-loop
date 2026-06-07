// DrawerHost — renders the active right-side detail drawer.
// PLACEHOLDER for the foundation build; Wave 2 replaces this with the real
// Event / Worker / PR / Saga drawer bodies (ported from prototype/drawers.jsx).
import { useDrawerState } from "@/lib/drawer";

export function DrawerHost() {
  const { current } = useDrawerState();
  void current;
  return null;
}
