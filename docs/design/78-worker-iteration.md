# Worker iteration loop (issue #78)

## Why

A single worker SDK session is one-shot. Real failures we've seen:

1. Worker edits 6 files across 60+ turns then exits without `git commit`.
2. Worker commits but never `git push`.
3. Worker pushes but doesn't `gh pr create`.
4. PR is open, the critic blocks on sev1 — worker walks away.
5. PR is open, CI is red — nobody fixes it.
6. PR has merge conflicts against trunk.

Issue #77 (auto-rescue) covers (1) by committing + pushing on the worker's
behalf. (2)–(6) still required operator intervention. The iteration loop
closes that gap: keep dispatching focused sub-sessions until the PR merges,
capped at `LOOP_WORKER_MAX_ITERATIONS` (default 3) so a broken issue
doesn't spin forever.

## State machine

After the first worker session exits with `status != "merged"`, the runner
probes the worktree + PR state and dispatches a focused follow-up session
for that state. Each follow-up session reuses the SAME worktree + branch.

```
DONE_MERGED ──── terminal ──> exit
PR_OPEN_HEALTHY ─ no-LLM ──> gh pr merge --auto, exit
PR_OPEN_BLOCKED ─ LLM ────> fix_critic brief
PR_OPEN_CI_FAILED ─ LLM ──> fix_ci brief
PR_OPEN_DIRTY ─── LLM ────> commit brief
PR_OPEN_CONFLICT ─ LLM ───> resolve_conflict brief
COMMITTED_NOT_PUSHED ─ LLM > push brief
PUSHED_NO_PR ────  LLM ───> open_pr brief
DIRTY_NO_COMMIT ── LLM ──> commit brief
CLEAN_NOTHING ──── LLM ──> complete_work brief (re-attempt the issue)
```

After `LOOP_WORKER_MAX_ITERATIONS` attempts without merge:

* emit `worker_iterations_exhausted`,
* label the GH issue `loop:needs-human`,
* post a comment with the worktree path + final state diagnostic,
* leave the PR open (if any).

## Why "focused brief" instead of one big brief

The original worker brief is ~400 lines (issue spec + contract + lumen
discovery + tests). Re-running it costs 50–100 turns. The follow-up
session usually has ONE missing step (push, open PR, address sev1) — a
~10-line brief lands in ~5 turns and costs cents.

Each brief is IMPERATIVE: "Your ONLY job is X. Then exit." This prevents
the follow-up session from re-litigating the original implementation.

## Files

* `src/forge_loop/runner/iteration.py` — probe + brief router + driver.
* `src/forge_loop/briefs/iter/*.md.tmpl` — 7 per-state templates.
* `src/forge_loop/runner/tick.py` — calls `run_iteration_loop` post-`_run_workers`.
* `src/forge_loop/config.py` — `LOOP_WORKER_MAX_ITERATIONS` (default 3).
* `src/forge_loop/worker.py` — `run_worker(..., brief_override=...)` kwarg.

## Out of scope

* Critic ↔ worker semantic ping-pong on subjective findings.
  The iteration loop addresses critic sev1 + sev2 only.
* Trunk-rebase logic. Probe surfaces `PR_OPEN_CONFLICT`; the fixer session
  handles the rebase, escalating if it can't.
* Parallel iteration. One issue → sequential attempts.
