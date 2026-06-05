[← System Architecture](./07-system-architecture.md) · [Index](./README.md) · [Next: Limitations →](./09-limitations.md)

---

# 8. Agent Enablement: The Other Edge of the Envelope

> *Governance constrains what the agent may do. Enablement guarantees what the
> agent can do. A capability envelope has two edges, and under-specifying
> either one is a defect.*

## 8.1 The asymmetry the governance thesis hides

Sections 1–7 argue that the hard problem of an autonomous factory is
*governance* — bounding what the agent is allowed to produce and admit. That
is true, but it quietly assumes the agent can act at all. It can't, unless the
system first **enables** it: gives it the tools, the environment, the
permissions, and the awareness the task requires.

These are duals. An agent's **capability envelope** has two edges:

- the **constraining** edge — what it must *not* reach (permissions, sandbox,
  the value/quality gates). Get this wrong in the permissive direction and the
  agent is dangerous.
- the **enabling** edge — what it must *be given* (a working toolchain, the
  task context, the verification commands, the relevant memory). Get this wrong
  in the restrictive direction and the agent is not safe-but-useless — it is
  *silently* stuck, which is worse, because the failure wears the costume of
  progress.

A mature system specifies **both** edges explicitly. The literature on the
constraining edge is old and good — least privilege (Saltzer & Schroeder,
*The Protection of Information in Computer Systems*, 1975) is the canonical
statement that a component should hold only the privileges it needs. The
enabling edge has no equally crisp slogan for agents, and its absence is
expensive.

## 8.2 The failure mode of under-enablement: silent thrash

A constraint failure tends to be loud — a permission denied, a sandbox
violation, a blocked merge. An *enablement* failure is the opposite: it is
silent, because a capable agent confronted with a missing capability does not
crash. It **improvises**, indefinitely.

The forge-loop project hit this directly. Workers run in a throwaway worktree
and were handed their environment by ambient inheritance — whatever the
orchestrator's process happened to carry. Launched from a shell without the
project virtualenv activated, a worker inherited a `PATH` with no project
`.venv`: its `python` was the system interpreter and its type-checkers
(`pyright`, `mypy`) simply did not exist. Every verification command returned
*command not found*. The worker — doing exactly what a diligent engineer would —
tried another invocation, then another, then probed which tools existed, for
**twenty minutes, emitting no error.** The only visible symptom was that it was
"slow." An entire fleet of workers thrashed the same way at once.

Nothing was broken in the agent. Everything was broken in its *enablement*: a
load-bearing dependency (the toolchain) was implicit, unprovisioned, and
unverified, and it failed soft. This is the same disease as a load-bearing
datum round-tripped through a fallible side-effect (Section 6's slop taxonomy):
**an implicit dependency that degrades silently.** The cure is invariant —
*declare the contract, provision it deterministically, verify it at the
boundary, and fail loud, fast, and specifically.*

## 8.3 The fix: a declared, provisioned, verified environment contract

Enablement cannot be hardcoded, because the loop is general: it runs on
projects of different stacks, and the toolchain a worker needs is stack-specific
(a Python venv, a Node `node_modules/.bin`, a Go toolchain). The loop cannot
know; the *project* must declare. So enablement becomes a contract the project
writes and the loop honors:

```yaml
worker:
  env:
    path_prepend: [".venv/bin"]              # dirs added to the worker's PATH
    vars: { VIRTUAL_ENV: ".venv" }           # environment the toolchain expects
    require: [python, ruff, pytest, pyright] # the preflight gate
  verify: ["ruff check src tests", "pyright src/forge_loop", "python -m pytest -q"]
```

The loop then does three things, one per facet of enablement:

1. **Provision.** It constructs the worker's environment deliberately — setting
   the declared variables and prepending the declared tool directories — instead
   of inheriting it by accident. The worker gets the exact toolchain a developer
   gets after activating the environment, independent of how the loop was
   launched.
2. **Verify, loud.** Before dispatch it asserts every `require` tool resolves.
   If one is missing it emits a typed `worker_toolchain_unavailable` event and
   *aborts the worker immediately* with a precise error. A twenty-minute silent
   thrash collapses into a one-second, actionable failure. If the contract is
   absent entirely, it warns (`worker_env_undeclared`) — it never guesses the
   stack, but it never lets the environment be silently undeclared either.
3. **Inform.** The `verify` commands are injected into the worker's brief as the
   definition of done, so the agent never has to *rediscover* how the project
   checks itself. The canonical recipe is declared once and shared by the worker,
   the critic's expectations, and CI.

This makes enablement a first-class, **detectable** property: the typed events
let supervision and CI see an under-enabled worker instantly, and a quality
rule (manifesto Q11) lets the critic block any new verification step that
depends on ambient `PATH` — closing the class the way the bug→rule→gate ratchet
(Section 4) closes every other one.

## 8.4 The general principle

The three facets above generalize past the toolchain. An agent is fully
enabled only when three things are declared, provisioned, and verified:

- **Permissions** — what it may touch. (The constraining edge; Section 4's gates
  and the worker permission profiles.)
- **Environment** — what it can run: tools, runtimes, services, credentials.
- **Awareness** — what it knows: the task, the value axes, the quality
  manifestos, and — critically — the *critic's own findings on its work*, fed
  back through a fast, reliable medium rather than re-derived.

Each facet, mishandled, fails differently: excess permission is unsafe, a
missing tool causes silent thrash, missing awareness causes drift (Section 1).
The system's job is not only to *bound* the agent but to hand it a fully
specified, verified envelope — and then hold it to it. Governance without
enablement is a cage with nothing inside it; enablement without governance is
the ungoverned factory of Section 1. The contribution is insisting on **both,
explicitly.**

---

[← System Architecture](./07-system-architecture.md) · [Index](./README.md) · [Next: Limitations →](./09-limitations.md)
