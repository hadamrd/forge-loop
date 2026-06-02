# forge-loop Product Vision

`forge-loop` should become a durable, inspectable control plane for long-running
software-engineering agents.

The product starts as a pragmatic issue-to-PR loop runner, but its intended
direction is larger: a repo-local maestro that can survive session resets,
recover from crashed or stale workers, preserve project cognition, and keep the
next product frontier explicit. It should be useful on itself before it claims
to be useful on other repositories.

## Core promise

An operator should be able to stop and restart the agent system and still know:

- what the product is trying to become;
- what has already been decided and why;
- which approaches were rejected and why;
- where the current frontier is;
- which workers are active, stale, failed, or compensated;
- which repo files, tests, and indexes are currently hot;
- what evidence moved the frontier forward.

That durable cognition must live outside the model context. Context windows are
working memory, not state. Worker context is disposable. Maestro state is
reconstructed from durable logs, projections, curated memory, and explicit
frontier files.

## Brainstorming layer

The brainstormer is the product-frontier generator.

It turns the product vision, value axes, current backlog, external research, and
curated memory into plausible frontier candidates. Its output is not trusted
automatically. It proposes candidate epics and tickets; the maestro and curator
decide what becomes durable backlog.

Good brainstorm output should:

- open new architectural frontier, not merely tidy code;
- name the customer and failure mode;
- explain why the work matters now;
- avoid duplicating existing GitHub issues;
- produce tasks small enough for isolated workers;
- preserve rejected paths so the project does not re-litigate old ideas;
- make the system better at dogfooding itself.

## Near-term product frontier

The near-term frontier is turning the long-running-agent architecture from
documents and package stubs into runnable dogfood primitives:

1. durable SQLite/WAL event log and projection cursors;
2. repo-local frontier cursor that can be loaded at boot;
3. curated memory store for decisions, rejected ideas, episodes, and skills;
4. task saga lifecycle with leases, heartbeats, terminal states, and
   compensations;
5. sandbox policy for isolated worker worktrees and least-privilege tool access;
6. maestro boot protocol that reconstructs state after reset;
7. observability commands that explain frontier, memory, task, and replay state;
8. critic/enforcer gates that test semantic behavior, not just happy paths.

## Non-goals

Do not propose cosmetic cleanup, broad rewrites, dashboard polish, prompt
wordsmithing, or documentation-only tickets unless they directly establish a
control-plane contract that workers and the maestro will consume.

Do not scale parallel workers until the event log, task lifecycle, sandbox
cleanup, and boot recovery path can explain and recover stale or crashed work.

Do not treat markdown manifestos as sufficient architecture. They are inputs to
the control plane, not the control plane itself.
