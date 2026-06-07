# Console real-data plan — making every screen reflect the live loop

The operator console (`console/` + `forge_loop.console_api`) is wired to the real
durable control plane. Most screens are live; the gaps are **not UI bugs** — they are
places where the loop does not yet *record* the data, so the console honestly shows
"not yet measured" rather than faking it. This plan closes the gaps in priority order.

## Status matrix (per screen)

| Screen | State today | Gap | Closes in |
|--------|-------------|-----|-----------|
| Live Event Stream | ✅ real (events.db + SSE) | — | done |
| Mission Control | ✅ real KPIs/ticker/frontier | KR chart + $/PR + first-pass blank | B1, B2 |
| Workers | ✅ real (lease-reconciled) | monologue / capability / tokens empty in drawer | B4 |
| Sagas | ✅ real (reconstructed) | — | done |
| PRs & Critic | ✅ real list + verdict/sev2 trajectory | per-finding `file:line` table empty | B3 |
| Frontier | ✅ real cursor | OKR/KR numbers (cursor has no typed KR yet) | A3 + B1 |
| Memory | ✅ real (memory.db) | — | done |
| Control-plane health | ✅ real projections/pipeline | budget trend `$0` | B2 |
| Backlog & Axes | ⚪ empty | not wired (GitHub issues) | **A1** |
| Manifestos | ⚪ empty | not wired (rule .md files) | **A2** |
| Scorecard | ⚪ honest nulls | scorecard projection unregistered | **B1** |

Legend: ✅ live · ⚪ honest-empty (data not yet produced).

## Phase A — API-only wins (data already exists; no loop change)

Each is one endpoint in `console_api.py` reading a source the loop already produces.

- **A1 — `/api/backlog`**: list GitHub issues (githubkit), map `axis` from the
  `axis:<name>` label, `epic`/`loop:ready` from labels. Powers Backlog & Axes +
  the Overview backlog count. (Empty now post-cleanup; fills as work is filed.)
- **A2 — `/api/manifestos`**: parse `.forge/quality-manifesto.md` +
  `testing-manifesto.md` rule sections (`### Q1.` / `### T1.` … rationale, source
  issue) into `ManifestoRule[]`. Powers Manifestos.
- **A3 — `/api/frontier` OKR passthrough**: surface `objective` / `key_result` now
  that the cursor carries them (numeric `kr_current/target` stays blank until B1).
- **A4 — operational-entropy metric** (serves the new convergence axis): a small
  `/api/health` addition counting open branches / live worktrees / open epics /
  backlog age, rendered on Health + Overview so the loop's *own* divergence is visible.

## Phase B — loop must record the data first, then the API reads it

These are blocked on loop instrumentation (each is also a forge-loop frontier item).

- **B1 — Scorecard** *(biggest UX win; the loop's #1 frontier task)*: register the
  Scorecard projection on the replay framework so it derives first-pass acceptance,
  repair-rounds, sev2-regeneration, lead time, abandonment as a trend. Then
  `/api/scorecard` returns real series → the hero KR chart fills + Overview
  first-pass KPI + Frontier KR bar all light up.
- **B2 — cost signal**: record per-task `cost_usd` in event payloads (open question:
  where cost attaches). Then `/api/budget` + `$/merged-PR` become real.
- **B3 — durable critic findings**: persist critic findings (`severity, file, line,
  message, category`) as first-class events. Then the PR drawer's findings table +
  minimal-path-to-green populate.
- **B4 — worker introspection**: surface monologue (from the worker log path),
  capability policy (`worker_policy_enforced` events), and token counts. Fills the
  worker drawer.

## Phase C — coherence & honesty

- Per-screen **"awaiting loop instrumentation" badge** on ⚪ screens, so an empty
  Scorecard reads as *designed* (it already does on Scorecard via the nulls map;
  extend to Backlog/Manifestos) rather than broken.
- **Wire kill**: `/api/workers/{id}/kill` currently no-ops; write the kill marker the
  runner consumes (once the read-only console is allowed one mutation).

## Sequencing

Phase A is independent and shippable now (no loop dependency) — do A1/A2/A3/A4 in one
pass. Phase B items ride along with their forge-loop frontier tickets (B1 first — it
unblocks three console surfaces at once). Phase C is polish after A.

The console will be "fully real" exactly when the loop's own outer-loop return arcs
(measurement B1, cost B2, evidence B3) are wired — i.e. the console's completeness is
itself a live readout of how closed the outer loop is.
