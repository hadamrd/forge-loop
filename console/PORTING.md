# Porting the console: mock → real

The app is built so swapping the mock backend for the real forge-loop backend is a
**one-file change**. Components are pure and never fetch; all server state flows through
TanStack Query hooks in `src/hooks/`; those hooks depend only on the `ForgeApi`
**interface** (`src/api/client.ts`), never a concrete impl. Mock and real both satisfy it.

```
UI components  →  hooks/ (TanStack Query)  →  ForgeApi interface  →  { mock | real } impl
```

## The swap

1. Implement the `fetch()` bodies in **`src/api/real/realApi.ts`** against the backend.
   The stubs are already written to the documented routes — fill in base URL + auth via
   `VITE_FORGE_BASE_URL`.
2. Flip the factory — either set `VITE_FORGE_API=real` (read in `src/api/client.ts →
   createApi()`), or pass `<ApiProvider mode="real">` in `src/main.tsx`.
3. Delete `src/api/mock/` if you want. Nothing else imports it.

No component, hook, route, or type changes. If the real responses match the `domain/`
types (they're modelled to), every screen lights up unchanged.

## Route ↔ method mapping

| `ForgeApi` method        | Real route / MCP tool                | Notes |
|--------------------------|--------------------------------------|-------|
| `getLoopStatus()`        | `GET /status` · `mcp loop_status`    | Overview KPIs + Health |
| `streamEvents(since,cb)` | `GET /events/stream` (SSE)           | Spine of the app; `since` = last seq |
| `getEvents(query)`       | `mcp events_query` / `events_recent` | Paged; `cursor` for backfill |
| `getWorkers()`           | `GET /workers`                       | Refetched every 5s |
| `getWorkerLog(id)`       | `mcp worker_logs`                    | Monologue tail (worker drawer) |
| `killWorker(id)`         | `POST /workers/{id}/kill`            | Mutation → invalidates `workers` |
| `getPRs()`               | (list PRs)                           | Includes embedded `review` |
| `getCriticReview(pr)`    | `mcp critic_review_pr`               | Findings + minimal-path + trajectory |
| `getSagas()`             | (list sagas)                         | Lifecycle board / table |
| `getAttempts(issue)`     | `mcp attempts_history`               | Per-issue event history |
| `getScorecard()`         | (scorecard + history)                | Null metrics are intentional — see below |
| `getFrontier()`          | reads `.forge/frontier.yaml`         | Cursor: objective, KR, rejected paths… |
| `getMemory()`            | (memory store)                       | episodic / procedural / rejected_path |
| `getBacklog()`           | (issues)                             | Grouped by the 6 value axes |
| `getManifestos()`        | (quality + testing rules)            | The gates |
| `getBudget()`            | `GET /budget`                        | Cost & tokens over time |
| `getPipeline()`/`getRoles()` | `GET /pipeline` · `GET /roles`   | Health screen |

## Contracts that must hold

- **Event envelopes** carry `sequence`, `event_id`, `kind`, `occurred_at`, optional
  `saga_id` / `task_id` / `causal_event_id`, and a per-kind `payload`. The event drawer
  walks `causal_event_id` to build the causal chain — keep it populated server-side or
  the chain view degrades gracefully to a single node. `event_id` must be unique
  (the live feed dedupes on it).
- **Null scorecard metrics are a feature, not missing data.** `sev2_regeneration_rate`
  is structurally `null` (not instrumented); `abandonment_rate` is `null` until enough
  in-window merges. The UI renders these via `Scorecard.nulls[key]` as a designed
  "not yet measured" state. **Never** send `0` to fake them.
- **Semantic theme is centralized** in `src/lib/theme.ts`. Every `EventKind`, `Severity`,
  `SagaState`, and PR label resolves to exactly one icon + color + label there. Add a new
  event kind → add one entry.

## Where the visual spec lives

The original interactive reference is the design handoff `prototype/forge-loop.html`
(same data, same theme, live ticker, drawers, causal chains). When a screen looks
ambiguous, that file is ground truth for layout, density, and states.
