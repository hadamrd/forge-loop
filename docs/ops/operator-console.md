# Operator console — serving it against a live loop

The React operator console (`console/`) talks to a JSON + SSE read API
(`forge_loop.console_api`) that exposes the durable control plane — the same
`.forge` event log / status / frontier / memory the runner and `forge-loop status`
use. The API is **read-only** (a `mode=ro` SQLite reader, so it never contends with
the running loop) and reconstructs Sagas / Workers / PRs from the event log.

## Run (same-origin: API serves the built console)

```bash
# 1. build the console in real-API mode (points realApi at /api)
cd console && VITE_FORGE_API=real pnpm install && VITE_FORGE_API=real pnpm build

# 2. serve API + console from one process, pointed at the repo whose .forge to read
cd ..
LOOP_REPO="$(pwd)" \
LOOP_CONSOLE_DIST="$(pwd)/console/dist" \
LOOP_CONSOLE_TOKEN="$(openssl rand -hex 16)" \
  uv run --extra experimental uvicorn forge_loop.console_api:app --host 0.0.0.0 --port 8790
```

Open `http://localhost:8790`. Same-origin means no CORS and no build-time URL
baking — the console fetches relative `/api/*` and the SSE stream at
`/api/events/stream`.

| env | meaning |
|-----|---------|
| `LOOP_REPO` | repo whose `.forge/` to read (default: cwd) |
| `LOOP_CONSOLE_DIST` | built console dir to serve at `/` (omit to run API-only) |
| `LOOP_CONSOLE_TOKEN` | bearer token; omit for open local access. `/healthz` always open |

Dev (mock data, no backend): `cd console && pnpm dev` → http://localhost:5217.

## What's real vs. honest-empty today

Real (reconstructed/derived from the live event log + stores): **status, live
event stream (SSE), sagas, workers (in-flight), PRs + critic verdict/trajectory,
frontier cursor, memory, budget, pipeline.**

Honest-empty / "not yet measured" until the loop wires them:
- **Scorecard / OKR trend** — the scorecard projection isn't registered on the
  loop yet, so metrics render as the designed "not yet measured" state (truthful).
- **$/merged-PR** — event payloads carry no per-task cost signal (a known open
  question in the frontier), so spend shows `$0`.
- **Critic findings detail** — verdict + sev2 trajectory are in the event log;
  per-file findings are not, so the findings table is empty.
- **Backlog / Manifestos** — external sources (GitHub issues / rule files); the
  endpoints return `[]` until wired.
- **Kill worker** — read-only console; the kill button is a no-op for now.

Endpoint ⇄ console method mapping lives in `console/PORTING.md`.
