# Long-Running Agent Architecture

## Purpose

This document is the landing architecture for evolving `forge-loop` from an
issue-to-PR loop runner into a long-running autonomous software-engineering
control system.

The immediate goal is not to replace the current runner. The immediate goal is
to make the future architecture visible in the repository so new work has a
place to land:

- durable event log / WAL;
- maestro control plane;
- task saga lifecycle;
- frontier cursor;
- curated memory;
- disposable worker execution;
- sandbox capability policy;
- enforcement gates;
- observability and replay.

The current markdown control surfaces (`product-vision.md`, `axes.yaml`,
quality/testing manifestos) remain useful, but they are not the architecture.
They are inputs consumed by a broader control system.

## Architectural Planes

| Plane | Code home | Responsibility |
| --- | --- | --- |
| Event log | `forge_loop.eventlog` | Append-only durable events, sequence, replay, projections, snapshots. |
| Control | `forge_loop.control` | Maestro boot/resume, tick state machine, dispatch decisions, recovery. |
| Tasks | `forge_loop.tasks` | Task/saga lifecycle, leases, heartbeats, compensation, terminal states. |
| Frontier | `forge_loop.frontier` | Current product frontier cursor, hot files/tests, active decisions, rejected paths. |
| Memory | `forge_loop.memory` | Semantic/episodic/procedural memory, promotion, supersession, compaction. |
| Execution | `forge_loop.execution` | Disposable worker runtime contracts and structured worker results. |
| Sandbox | `forge_loop.sandbox` | Worktree/container policy, MCP allowlists, secrets, egress, cleanup/quarantine. |
| Enforcement | Existing + future `forge_loop.enforcement` | Value gates, critic gates, manifestos, codebase audits, stronger oracles. |
| Observability | Existing dashboard/status + future projections | Timelines, replay, frontier/memory views, stuck-agent diagnosis. |

## Migration Strategy

The migration should proceed by adding durable contracts before rewiring
runtime behavior.

1. Define event-log, task, frontier, memory, and sandbox contracts.
2. Build projections and boot-context assembly from those contracts.
3. Mirror current runner events into the new event-log shape.
4. Dogfood a frontier cursor for forge-loop itself.
5. Add a memory curator that promotes selected worker observations.
6. Move `_tick` responsibilities into the control/task modules incrementally.
7. Only then add more parallelism or stronger sandbox backends.

## Non-Goals For The First Slice

- No full runner rewrite.
- No replacement of the existing JSONL event files yet.
- No behavior-heavy tests before the interfaces settle.
- No promise that markdown manifestos alone govern the system.

## Dogfood Contract

The first system to use this architecture is `forge-loop` itself.

The dogfood target is:

1. A fresh maestro session can load a forge-loop frontier cursor.
2. It can see active decisions and rejected paths.
3. It can dispatch a bounded task with an explicit sandbox policy.
4. It can record task events into the durable log contract.
5. It can promote only selected observations into memory.
6. It can explain why the frontier moved.

Until those properties are demonstrable, forge-loop should be described as a
prototype loop runner, not as a mature autonomous engineering system.
