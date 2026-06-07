# forge-loop · operator console

A real-time operator console for the forge-loop autonomous engineering loop — a
single human watching a machine that turns GitHub issues into merged, evidence-backed
PRs unattended. Dense, calm, real-time: think Vercel × Linear × Grafana × flight control.

Built from the Claude-Design handoff (`prototype/forge-loop.html`) as a production
React app. Every screen runs **today** on a seeded, in-memory mock API with a live
event ticker, and is wired so swapping to the real Python backend is a **one-file change**
(see [`PORTING.md`](./PORTING.md)).

## Run

```bash
pnpm install
pnpm dev        # http://localhost:5217
```

```bash
pnpm build      # tsc -b (strict) + vite build → dist/
pnpm typecheck  # tsc -b --noEmit
pnpm preview    # serve the production build
```

Requires Node ≥ 20.19 and pnpm 10.

## Stack

Vite 6 · React 19 · TypeScript 5 (strict) · TanStack Router (code-based) ·
TanStack Query (all server state) · TanStack Table · lucide-react · Tailwind
(available; tokens mapped) · hand-tuned SVG charts.

## Architecture — built for the port

```
UI components  →  hooks/ (TanStack Query)  →  ForgeApi interface  →  { mock | real } impl
   (dumb,           (the only fetch layer)      (the contract)         (swap target)
   prop-driven)
```

```
src/
  domain/          pure TS types mirroring the backend (events.ts, models.ts) — NO React
  api/
    client.ts      the ONE ForgeApi interface + createApi() factory
    ApiProvider.tsx provides the api instance + a QueryClient
    mock/          seeded dataset (seed.ts) + impl with latency + live ticker (mockApi.ts)
    real/          STUB real impl hitting the documented REST/SSE routes (realApi.ts)
  hooks/           one TanStack Query hook per resource — components NEVER call the api
  lib/             theme (the single semantic system), format, queryKeys, drawer/ui context
  components/
    Icon.tsx       lucide wrapper — every icon referenced by name from lib/theme
    primitives/    Panel, Kpi, Banner, DataTable, Drawer, pills/badges, … (prop-driven)
    charts/        Sparkline, KrTrendChart (hero), StepTrajectory, AreaTrend, DistBars
    drawers/       Event (causal chain) · Worker (monologue + capability) · PR (critic) · Saga
    layout/        AppShell (sidebar + topbar + outlet + drawer host)
  routes/          one screen component per route (11 screens)
  router.tsx       code-based TanStack Router tree
```

**Hard rules honored:** components are pure and never fetch; all server state flows
through Query hooks; hooks depend only on the `ForgeApi` interface; the semantic
system (every EventKind / Severity / SagaState / PR label → one icon + color + label)
is defined once in `lib/theme.ts`; null scorecard metrics render as a designed
"not yet measured" state, never a fake 0.

## Screens

Mission Control (hero) · Live Event Stream · Workers · PRs & Critic · Sagas ·
Scorecard · Frontier · Memory · Backlog & Axes · Manifestos · Control-plane health.

## Notes on fidelity (deliberate calls vs the original brief)

The design medium was HTML/CSS/JS; the brief said *recreate pixel-perfectly in whatever
tech fits — match the output, don't copy the prototype's internals*. Two calls follow
from that:

- **CSS design system + SVG charts ported verbatim** (`src/index.css`, `components/charts`)
  rather than rebuilt in Tailwind/Recharts. The KR-target hero chart (hatched
  "not yet measured" zone, dashed target line, pulsing current marker) was the single
  most important visual in the spec; re-deriving it in a chart library would have been
  lossy. Tailwind is wired (preflight off, tokens mapped) and Recharts is available for
  future standard charts.
- **`lucide-react` + `@tanstack/react-table`** used for icons/grids (the stack's intent),
  since the prototype's icon names and column shapes already lined up.

## Live feed & "pause"

`useEvents()` seeds from `getEvents()` then subscribes via `streamEvents()`, pushing new
envelopes into the Query cache (deduped by `event_id`, bounded to 600). The mock emits a
scripted tick cycle every 1.5–3s; the real `EventSource` feeds the same path. The topbar
**Live/Paused** toggle freezes the *view* (the ticker keeps running, so no events are
lost) — see `lib/ui.tsx`.
