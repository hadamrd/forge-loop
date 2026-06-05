# Agent Enablement: the declared worker-environment contract

**Status:** design approved 2026-06-05; Python-first implementation in progress
(general, stack-agnostic contract).

> **The dual of governance.** [`worker-permission-profiles.md`](./worker-permission-profiles.md)
> declares what a worker is *forbidden* to reach (the constraining edge of the
> capability envelope). This document declares what a worker must be *given* to
> do its job (the enabling edge). An autonomous agent fails in **both**
> directions: over-permissioned it is dangerous; under-enabled it is
> uselessly, *silently* stuck. The system must specify both edges explicitly.

## Problem

A worker is dispatched into a throwaway git worktree under `/tmp` and handed an
environment by **ambient inheritance** — `_worker_sdk.py` built its subprocess
env as `dict(os.environ)` and only stripped two leaked vars. Nothing ever
*provisioned* or *verified* the toolchain the worker's job depends on
(`python`, `pyright`, `mypy`, `ruff`, `pytest`, `git`, …).

This works by luck — only if whoever launched the orchestrator happened to have
the project virtualenv activated — and rots silently when luck runs out.

### The incident (2026-06-05)

The loop was launched via the installed CLI from a non-activated shell. Workers
inherited `VIRTUAL_ENV=/usr` and a `PATH` with **no project `.venv/bin`**, so
`python` resolved to the system interpreter and `pyright`/`mypy` (venv-only)
did not exist. Every type-check the worker ran returned *command not found*; the
diligent worker retried variant invocations (`mypy` → `python -m mypy` →
`which mypy pyright ruff` …) for **~20 minutes with no error surfaced** — the
only symptom was "slow." Multiple workers in the fleet thrashed identically.

This is a textbook member of a recurring class (see also the gh-cli round-trip,
manifesto Q10): **a load-bearing dependency left implicit, unverified, and
failing soft.** The cure is always the same principle — *make the contract
explicit, provision it deterministically, verify it at the boundary, and fail
loud, fast, and specifically when it is unmet.*

## Why this generalizes

`forge-loop` is a *general* loop; it runs on many projects (it grinds its own
Python repo, but also others). The required toolchain is **stack-specific**:
Python needs a venv / poetry / uv / conda; Node needs `node_modules/.bin` and
the right node; Go needs the go toolchain; Rust needs cargo. The loop cannot
know each project's stack. Therefore the **project must declare** its worker
environment, and the loop's job is to *provision → verify → inform*. A
hardcoded `.venv` inject would be a Python-only band-aid; the contract below is
stack-agnostic, with only the Python case wired today.

## Decision: a declared `worker.env` contract in the forge config

```yaml
worker:
  env:
    path_prepend: [".venv/bin"]              # repo-relative dirs prepended to worker PATH
    vars: { VIRTUAL_ENV: ".venv" }           # env vars; repo-relative path values resolved to absolute
    require: [python, ruff, pytest, pyright] # preflight gate — these MUST resolve
  verify: ["ruff check src tests", "pyright src/forge_loop", "python -m pytest -q"]
```

A Node project would instead write `path_prepend: [node_modules/.bin]`,
`require: [node, eslint, vitest]` — same contract, different values.

The loop then performs three jobs, one per edge of the problem:

### 1. Provision (ROOT fix)

When building the worker subprocess env, the loop no longer inherits ambiently.
It starts from the cleaned base env, sets `vars` (resolving repo-relative path
values to absolute against the repo root), and prepends `path_prepend` dirs to
`PATH`. The worker now has the **same toolchain a developer has after activating
the venv — deterministically, regardless of how the orchestrator was launched.**

### 2. Guard (LOUD, never silent)

Before the SDK session is driven, the loop asserts every `require` tool resolves
on the provisioned `PATH` (`shutil.which(tool, path=…)`).

- **Any missing →** emit typed `worker_toolchain_unavailable {missing, path, venv}`
  and **abort the worker immediately** with that error. A 20-minute silent
  thrash becomes a one-second, actionable failure. No doomed session is started.
- **`worker.env` absent entirely →** emit a `worker_env_undeclared` warning. We
  never *guess* the stack (no magic auto-detect), but we never let the
  environment be silently undeclared either.

### 3. Inform (kill the guessing)

`verify` commands are injected into the worker brief as an explicit *Definition
of Done — run these to verify*. The worker never again rediscovers whether to
call `mypy` or `python -m mypy`; the canonical recipe is declared once and is the
single source of truth for the worker, the critic's expectations, and CI.

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `WorkerConfig` (+ Settings parse) | carry `env_path_prepend`, `env_vars`, `env_require`, `verify_commands` | yaml `worker.env` / `worker.verify` |
| `worker_env.build_worker_env(base, repo, …)` | pure: produce the provisioned env (PATH + vars, absolute) | repo path |
| `worker_env.missing_tools(env, require)` | pure: which required tools don't resolve | `shutil.which` |
| worker dispatch (`worker.py`) | preflight → emit/abort or proceed; inject `verify` into brief | the two above + event emitter |
| events `worker_toolchain_unavailable` / `worker_env_undeclared` | the detectable signal | event log |

The two `worker_env` functions are pure and unit-tested in isolation; the
dispatch wiring is tested for the abort-loud and proceed paths.

## Guard against the class (manifesto Q11)

> *A worker's required toolchain/environment must be **declared and
> preflighted**. No verification step may depend on ambient `PATH` or inherited
> env; a capability the agent needs must be provisioned and verified at dispatch,
> failing loud if absent — never silently degrade.* **(sev2)**

The critic enforces this per-PR; the typed events make violations detectable in
supervision and CI.

## The broader principle

An autonomous coding agent must be **enabled** as deliberately as it is
**governed**. Its capability envelope has two edges, and both must be explicit,
declared, provisioned/enforced, and verified:

- **Permissions** — what it may touch (governance; `worker-permission-profiles.md`).
- **Environment & tools** — what it can run (enablement; this document).
- **Awareness** — what it knows: the task, the value axes, the manifestos, the
  critic findings (see [`context-awareness.md`](./context-awareness.md)).

Under-specifying *any* edge produces failure: over-permission is unsafe,
missing tools cause silent thrash, missing awareness causes drift. The system's
job is to hand the agent a fully-specified, verified envelope — then hold it to it.

## YAGNI / scope

The contract is stack-agnostic by construction, but only the Python path is
wired and tested now (forge-loop's own repo). No auto-detection of stacks (that
would re-introduce the implicit-guessing disease). Other stacks are a config
change plus their own `require`/`path_prepend` values when first needed.
