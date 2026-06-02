# Long-Running Agent Architecture Milestones Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve `forge-loop` from an issue-to-PR loop runner into a dogfooded, long-running autonomous software-engineering control system with durable WAL/eventlog, curated memory, frontier cursor, task sagas, sandbox policy, and maestro boot/recovery.

**Architecture:** Build the new architecture as contracts and narrow dogfood integrations before rewiring the current runner. Each milestone must leave a usable vertical slice with clear ownership, explicit event shapes, and lightweight checks; heavier behavior tests are introduced only when an interface becomes stable enough to protect.

**Tech Stack:** Python 3.11+, dataclasses/Pydantic where useful, SQLite WAL for first durable backend, existing `uv`/`ruff`/`mypy`/`pytest`, current `forge_loop` runner modules, Git worktrees, GitHub CLI/API adapters.

---

## Current Baseline

The first architecture scaffold already exists:

- `docs/design/long-running-agent-architecture.md`
- `src/forge_loop/eventlog/`
- `src/forge_loop/control/`
- `src/forge_loop/tasks/`
- `src/forge_loop/frontier/`
- `src/forge_loop/memory/`
- `src/forge_loop/sandbox/`
- `src/forge_loop/execution/`

These modules are contracts only. They do not yet replace `_tick`, the legacy
JSONL event bus, worker sessions, or the existing runner.

Known repo baseline from the 2026-06-02 audit:

- `pytest` and `mypy` were green at `dd2e441`.
- Existing repo-wide `ruff` and `pyright` were not green before this work.
- New architecture modules passed scoped `ruff`, scoped `mypy`, `compileall`,
  and `git diff --check`.

## Milestone Summary

| Milestone | Name | Proof |
| --- | --- | --- |
| M0 | Commit Architecture Baseline | Paper, review, architecture doc, and contract packages are in trunk. |
| M1 | Durable Event Log | SQLite-backed append-only event log can append, replay, enforce sequence/idempotency, and track projection cursors. |
| M2 | Forge-Loop Frontier Cursor | A forge-loop frontier cursor can be loaded on boot, rendered in status, and advanced only through event-log events. |
| M2.5 | Frontier Generation Dogfood | `forge-loop brainstorm` uses repo-local vision/axes to generate axis-aligned, non-cosmetic backlog candidates for this architecture. |
| M3 | Curated Memory Store | Decisions, rejected ideas, episodic lessons, and procedures can be promoted with provenance and loaded into boot context. |
| M4 | Task Saga Lifecycle | Tasks have saga identity, leases, heartbeats, terminal states, and compensations independent of worker implementation. |
| M5 | Maestro Boot Protocol | A fresh process can reconstruct frontier, memory, in-flight tasks, and projection cursors from durable state. |
| M6 | Runner Event Mirroring | Current `_tick` emits enough new eventlog events to dogfood projections without replacing legacy JSONL. |
| M7 | Sandbox Policy And Worktree Manager | Workers receive explicit capability policies and worktree cleanup/quarantine becomes a task compensation. |
| M8 | Worker Result And Memory Proposal Flow | Workers return structured observations; only curator-approved items become durable memory. |
| M9 | Observability And Replay | CLI/status surfaces show frontier, memory promotions, task timelines, and replay health. |
| M10 | Incremental Runner Refactor | `_tick` responsibilities move into control/tasks/enforcement modules without changing external behavior. |

---

## M0: Commit Architecture Baseline

**Goal:** Land the current article, adversarial review, architecture doc, and contract scaffolding as the baseline for future work.

**Files:**

- Add: `paper/00-long-running-autonomous-software-engineering.md`
- Add: `paper/reviews/00-long-running-autonomous-software-engineering-adversarial-review.md`
- Modify: `paper/README.md`
- Add: `docs/design/long-running-agent-architecture.md`
- Add: `src/forge_loop/{eventlog,control,tasks,frontier,memory,sandbox,execution}/`

### Task M0.1: Validate Current Baseline

- [ ] Run scoped checks for the new modules.

```bash
uv run ruff check src/forge_loop/eventlog src/forge_loop/frontier src/forge_loop/memory src/forge_loop/tasks src/forge_loop/control src/forge_loop/sandbox src/forge_loop/execution
uv run ruff format --check src/forge_loop/eventlog src/forge_loop/frontier src/forge_loop/memory src/forge_loop/tasks src/forge_loop/control src/forge_loop/sandbox src/forge_loop/execution
uv run mypy src/forge_loop/eventlog src/forge_loop/frontier src/forge_loop/memory src/forge_loop/tasks src/forge_loop/control src/forge_loop/sandbox src/forge_loop/execution
uv run python -m compileall -q src/forge_loop/eventlog src/forge_loop/frontier src/forge_loop/memory src/forge_loop/tasks src/forge_loop/control src/forge_loop/sandbox src/forge_loop/execution
git diff --check
```

Expected:

- scoped `ruff`: pass
- scoped `mypy`: pass
- `compileall`: no output
- `git diff --check`: no output

- [ ] Remove generated caches before commit.

```bash
find src/forge_loop/control src/forge_loop/eventlog src/forge_loop/execution src/forge_loop/frontier src/forge_loop/memory src/forge_loop/sandbox src/forge_loop/tasks -type d -name __pycache__ -prune -exec rm -rf {} +
```

- [ ] Commit the baseline.

```bash
git add paper/README.md paper/00-long-running-autonomous-software-engineering.md paper/reviews/00-long-running-autonomous-software-engineering-adversarial-review.md docs/design/long-running-agent-architecture.md src/forge_loop/control src/forge_loop/eventlog src/forge_loop/execution src/forge_loop/frontier src/forge_loop/memory src/forge_loop/sandbox src/forge_loop/tasks
git commit -m "docs: define long-running agent architecture"
```

**Maturity gate:** The commit exists and does not claim runner behavior that is not wired yet.

---

## M1: Durable Event Log

**Goal:** Replace the current in-memory `EventLog` scaffolding with a SQLite WAL-backed implementation that can be used by projections and boot recovery.

**Files:**

- Create: `src/forge_loop/eventlog/sqlite.py`
- Modify: `src/forge_loop/eventlog/__init__.py`
- Modify: `src/forge_loop/eventlog/store.py`
- Create: `tests/test_eventlog_sqlite.py`

### Task M1.1: Define SQLite Schema

- [ ] Add a schema constant in `src/forge_loop/eventlog/sqlite.py`.

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    task_id TEXT,
    saga_id TEXT,
    causal_event_id TEXT,
    causal_sequence INTEGER,
    idempotency_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS projection_cursors (
    projection_name TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL
);
"""
```

- [ ] Add a focused schema test.

```python
def test_sqlite_event_log_creates_schema(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)

    event = log.append(EventKind.FRONTIER_ADVANCED, {"frontier": "wal"})

    assert event.sequence == 1
    assert db.exists()
```

- [ ] Run the new test and verify it fails before implementation.

```bash
uv run pytest tests/test_eventlog_sqlite.py::test_sqlite_event_log_creates_schema -q
```

Expected first failure: `NameError` or import error for `SqliteEventLog`.

### Task M1.2: Implement Append And Replay

- [ ] Implement `SqliteEventLog.append()` with one transaction per event.

Required behavior:

- convert `payload` to canonical JSON with sorted keys;
- assign sequence through SQLite `AUTOINCREMENT`;
- generate `event_id` when missing;
- reject duplicate `idempotency_key`;
- return an `EventEnvelope`;
- persist UTC ISO timestamp.

- [ ] Implement `SqliteEventLog.since(sequence)`.

Required behavior:

- return events ordered by increasing sequence;
- deserialize `payload_json`;
- reconstruct `EventKind`;
- preserve `task_id`, `saga_id`, `idempotency_key`.

- [ ] Add replay test.

```python
def test_sqlite_event_log_replays_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    SqliteEventLog(db).append(EventKind.DECISION_MADE, {"decision": "use sqlite wal"})

    reopened = SqliteEventLog(db)
    events = list(reopened.since(0))

    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].kind is EventKind.DECISION_MADE
    assert events[0].payload["decision"] == "use sqlite wal"
```

### Task M1.3: Projection Cursor Persistence

- [ ] Add methods to `SqliteEventLog`.

```python
def get_projection_cursor(self, projection_name: str) -> ProjectionCursor: ...
def set_projection_cursor(self, projection_name: str, cursor: ProjectionCursor) -> None: ...
```

- [ ] Add cursor test.

```python
def test_projection_cursor_round_trips(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    log.set_projection_cursor("frontier", ProjectionCursor(sequence=42))

    assert log.get_projection_cursor("frontier").sequence == 42
```

### M1 Verification

Run:

```bash
uv run pytest tests/test_eventlog_sqlite.py -q
uv run ruff check src/forge_loop/eventlog tests/test_eventlog_sqlite.py
uv run mypy src/forge_loop/eventlog tests/test_eventlog_sqlite.py
```

**Maturity gate:** SQLite event log can replay after process restart and projection cursors survive restart.

---

## M2: Forge-Loop Frontier Cursor

**Goal:** Dogfood a durable frontier cursor for forge-loop itself.

**Files:**

- Create: `src/forge_loop/frontier/store.py`
- Create: `src/forge_loop/frontier/defaults.py`
- Modify: `src/forge_loop/frontier/__init__.py`
- Create: `tests/test_frontier_store.py`
- Create: `.forge/frontier.yaml` or `.forge-loop/frontier.yaml` after deciding the config home.

### Task M2.1: Add File-Backed Cursor Store

- [ ] Implement `FrontierStore`.

Required behavior:

- load YAML into `FrontierCursor`;
- write YAML from `FrontierCursor`;
- validate required fields: `product_goal`, `current_problem`, `next_expansion`, `why_now`;
- preserve rejected paths and hot artifacts.

- [ ] Add load/save test with a temp file.

```python
def test_frontier_store_round_trips_cursor(tmp_path: Path) -> None:
    path = tmp_path / "frontier.yaml"
    store = FrontierStore(path)
    cursor = FrontierCursor(
        product_goal="make forge-loop durable",
        current_problem="session reset amnesia",
        next_expansion="sqlite event log",
        why_now="eventlog is the spine",
    )

    store.save(cursor)

    assert store.load().next_expansion == "sqlite event log"
```

### Task M2.2: Add Forge-Loop Dogfood Cursor

- [ ] Create a repo-local frontier file that describes the current product frontier.

Recommended first values:

```yaml
product_goal: "turn forge-loop from issue-to-PR runner into a long-running autonomous engineering control system"
current_problem: "make durable eventlog, memory, frontier, task saga, and sandbox planes concrete"
next_expansion: "build SQLite WAL-backed eventlog"
why_now: "all later memory and maestro recovery depends on a replayable event spine"
active_decisions:
  - "contracts land before runner rewiring"
  - "workers are disposable; maestro and curator own durable state"
rejected_paths:
  - idea: "treat markdown manifestos as the whole architecture"
    reason: "markdown gates do not solve reset recovery, task sagas, or memory promotion"
    revisit_if: "a future enforcement engine proves docs alone can drive durable state transitions"
hot_files:
  - ref: "src/forge_loop/eventlog/"
    why_hot: "future WAL spine"
hot_tests:
  - ref: "uv run pytest tests/test_eventlog_sqlite.py -q"
    why_hot: "first durable eventlog proof"
open_questions:
  - "Should frontier.yaml live under .forge or .forge-loop?"
external_sources:
  - "paper/00-long-running-autonomous-software-engineering.md"
```

### Task M2.3: CLI Read Surface

- [ ] Add a small `forge-loop frontier show --json` command only after the store is stable.

Candidate ownership:

- command file: `src/forge_loop/cli_product_commands.py` or new `src/forge_loop/cli_frontier_commands.py`;
- data access: `src/forge_loop/frontier/store.py`.

### M2 Verification

Run:

```bash
uv run pytest tests/test_frontier_store.py -q
uv run forge-loop frontier show --json
uv run ruff check src/forge_loop/frontier tests/test_frontier_store.py
uv run mypy src/forge_loop/frontier tests/test_frontier_store.py
```

**Maturity gate:** A fresh session can read the dogfood frontier cursor without inspecting paper transcripts.

---

## M3: Curated Memory Store

**Goal:** Add durable memory items with provenance, active/superseded lifecycle, and a strict promotion path.

**Files:**

- Create: `src/forge_loop/memory/store.py`
- Modify: `src/forge_loop/memory/curator.py`
- Modify: `src/forge_loop/memory/models.py`
- Create: `tests/test_memory_store.py`

### Task M3.1: Add SQLite Memory Store

- [ ] Store `MemoryItem` records in SQLite.

Required schema:

- `memory_id` primary key;
- `kind`;
- `title`;
- `body`;
- `tags_json`;
- `source_event_id`;
- `source_sequence`;
- `authored_by`;
- `confidence`;
- `created_at`;
- `superseded_by`;
- `evidence_refs_json`.

- [ ] Add round-trip test.

```python
def test_memory_store_round_trips_active_item(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    item = MemoryItem(
        memory_id="m1",
        kind=MemoryKind.SEMANTIC,
        title="Rejected markdown-only architecture",
        body="Markdown gates do not solve reset recovery.",
        provenance=MemoryProvenance(source_event=None, authored_by="test"),
    )

    store.put(item)

    assert store.get("m1").is_active
    assert store.list_active(kind=MemoryKind.SEMANTIC)[0].title == item.title
```

### Task M3.2: Promotion Policy

- [ ] Extend `MemoryCurator.should_promote()`.

Promotion must require:

- non-empty `reason_to_remember`;
- at least one tag;
- a body longer than one sentence or a linked evidence ref;
- confidence >= 0.5 when provided.

- [ ] Add tests for rejection and promotion.

```python
def test_curator_rejects_context_noise_without_reason() -> None:
    curator = MemoryCurator()
    candidate = PromotionCandidate(title="noise", body="ran ls", reason_to_remember="")

    assert not curator.should_promote(candidate)
```

### Task M3.3: Rejected-Ideas Register

- [ ] Add convenience functions for rejected ideas.

Required behavior:

- create a semantic memory item tagged `rejected-path`;
- include `revisit_if`;
- list active rejected paths for boot context.

### M3 Verification

Run:

```bash
uv run pytest tests/test_memory_store.py -q
uv run ruff check src/forge_loop/memory tests/test_memory_store.py
uv run mypy src/forge_loop/memory tests/test_memory_store.py
```

**Maturity gate:** The system can preserve decisions and rejected paths without reading old chat or worker transcripts.

---

## M4: Task Saga Lifecycle

**Goal:** Create a durable task lifecycle independent of the current worker implementation.

**Files:**

- Create: `src/forge_loop/tasks/store.py`
- Create: `src/forge_loop/tasks/lease.py`
- Modify: `src/forge_loop/tasks/saga.py`
- Create: `tests/test_task_saga_store.py`

### Task M4.1: Durable Task Store

- [ ] Store `TaskSaga` rows in SQLite.

Required behavior:

- create planned task;
- transition states with validation;
- list non-terminal tasks;
- record compensations;
- query by issue, task id, saga id.

### Task M4.2: Leases And Heartbeats

- [ ] Add `TaskLease`.

Required fields:

- `task_id`;
- `lease_id`;
- `owner`;
- `expires_at`;
- `heartbeat_at`.

- [ ] Add lease behavior.

Required behavior:

- acquire lease only for non-terminal task;
- heartbeat extends expiry;
- expired lease can be reclaimed;
- terminal task cannot be leased.

### Task M4.3: Compensation Registry

- [ ] Define compensation kinds.

Initial kinds:

- `remove_worktree`;
- `abandon_branch`;
- `close_draft_pr`;
- `remove_label`;
- `quarantine_workspace`.

No external mutation is executed in this milestone. This milestone records
what compensation must happen; M7 wires worktree compensation.

### M4 Verification

Run:

```bash
uv run pytest tests/test_task_saga_store.py -q
uv run ruff check src/forge_loop/tasks tests/test_task_saga_store.py
uv run mypy src/forge_loop/tasks tests/test_task_saga_store.py
```

**Maturity gate:** A task can be killed mid-flight and the system can identify its required compensation from durable state.

---

## M5: Maestro Boot Protocol

**Goal:** Reconstruct strategic state after reset from eventlog, frontier, memory, and task stores.

**Files:**

- Create: `src/forge_loop/control/boot_protocol.py`
- Modify: `src/forge_loop/control/boot.py`
- Create: `tests/test_control_boot_protocol.py`

### Task M5.1: Boot Protocol Inputs

- [ ] Define `BootSources`.

Required fields:

- event log;
- frontier store;
- memory store;
- task store.

### Task M5.2: Boot Context Assembly

- [ ] Implement `assemble_boot_context(sources) -> BootContext`.

Required behavior:

- load current frontier;
- load active semantic memory IDs;
- load active rejected-path memory IDs;
- load non-terminal task IDs;
- load highest event sequence;
- render deterministic summary.

### Task M5.3: Hard-Kill Simulation Test

- [ ] Add a test that creates durable state, constructs a new set of store objects, and verifies the same `BootContext` is assembled.

### M5 Verification

Run:

```bash
uv run pytest tests/test_control_boot_protocol.py -q
uv run ruff check src/forge_loop/control tests/test_control_boot_protocol.py
uv run mypy src/forge_loop/control tests/test_control_boot_protocol.py
```

**Maturity gate:** A fresh process can reconstruct the same frontier and in-flight task list after reopening stores.

---

## M6: Runner Event Mirroring

**Goal:** Start dogfooding the new eventlog by mirroring selected legacy runner events into the durable event log while preserving the old JSONL event bus.

**Files:**

- Create: `src/forge_loop/eventlog/legacy_bridge.py`
- Modify: `src/forge_loop/runner/tick.py`
- Modify: `src/forge_loop/runner/boot.py`
- Create: `tests/test_eventlog_legacy_bridge.py`

### Task M6.1: Bridge Legacy Events

- [ ] Map selected legacy events to new `EventKind`.

Initial mappings:

- `tick_start` -> `TASK_PLANNED` when issues are selected;
- worker dispatch -> `TASK_DISPATCHED`;
- worker outcome failed -> `TASK_FAILED`;
- worker outcome open/merged -> `TASK_COMPLETED`;
- critic verdict -> `CRITIQUE_ISSUED`;
- worktree reaped -> compensation-like metadata.

### Task M6.2: Add Config Flag

- [ ] Add a setting that enables durable eventlog mirroring.

Initial default:

- disabled unless `misc.eventlog_enabled` or similar setting is true.

This avoids destabilizing the runner until M1-M5 prove durable state.

### Task M6.3: One-Tick Smoke

- [ ] Run a bounded tick in a controlled repo only after M6 tests pass.

Required command shape:

```bash
LOOP_GH_REPO=hadamrd/forge-loop uv run forge-loop run --max-ticks 1
```

Use only when the operator explicitly wants a live dogfood tick.

### M6 Verification

Run:

```bash
uv run pytest tests/test_eventlog_legacy_bridge.py tests/test_runner.py -q
uv run ruff check src/forge_loop/eventlog src/forge_loop/runner tests/test_eventlog_legacy_bridge.py
```

**Maturity gate:** A tick can still run using legacy behavior while durable projections receive enough events to rebuild task timelines.

---

## M7: Sandbox Policy And Worktree Manager

**Goal:** Make worker capabilities explicit and turn worktree cleanup into registered task compensation.

**Files:**

- Create: `src/forge_loop/sandbox/worktree.py`
- Modify: `src/forge_loop/sandbox/policy.py`
- Modify: `src/forge_loop/worker_worktree.py`
- Create: `tests/test_sandbox_worktree.py`

### Task M7.1: Worktree Allocation Contract

- [ ] Define `WorktreeAllocation`.

Required fields:

- `task_id`;
- `path`;
- `branch`;
- `base_ref`;
- `policy`.

### Task M7.2: Capability Policy Rendering

- [ ] Render `CapabilityPolicy` into a worker-readable brief block.

The block must include:

- read roots;
- write roots;
- allowed MCP servers/tools;
- allowed secrets;
- network policy.

### Task M7.3: Cleanup Compensation

- [ ] Register `remove_worktree` compensation when a worktree is allocated.
- [ ] Quarantine instead of delete when worker status is failed and policy says preserve.

### M7 Verification

Run:

```bash
uv run pytest tests/test_sandbox_worktree.py tests/test_worker_worktree.py -q
uv run ruff check src/forge_loop/sandbox src/forge_loop/worker_worktree.py tests/test_sandbox_worktree.py
```

**Maturity gate:** Every worker worktree has an explicit capability policy and a recorded cleanup/quarantine path.

---

## M8: Worker Result And Memory Proposal Flow

**Goal:** Connect disposable worker outputs to curator-approved memory promotion without allowing workers to write durable memory directly.

**Files:**

- Modify: `src/forge_loop/execution/worker_result.py`
- Modify: `src/forge_loop/worker.py`
- Modify: `src/forge_loop/worker_brief.py`
- Modify: `src/forge_loop/memory/curator.py`
- Create: `tests/test_worker_memory_proposals.py`

### Task M8.1: Worker Result JSON Contract

- [ ] Update worker brief final JSON shape.

Required final payload:

```json
{
  "issue": 123,
  "pr": "https://github.com/owner/repo/pull/456",
  "status": "open",
  "note": "short outcome",
  "observations": [
    {
      "summary": "what changed future behavior",
      "proposed_memory": true,
      "reason_to_remember": "prevents rediscovering rejected path"
    }
  ],
  "risks": [],
  "next_actions": []
}
```

### Task M8.2: Parse Worker Observations

- [ ] Extend worker result parsing to preserve structured observations.
- [ ] Keep backward compatibility with old worker JSON that lacks observations.

### Task M8.3: Curator Review Path

- [ ] Convert worker observations into `PromotionCandidate`.
- [ ] Emit `MEMORY_PROMOTED` event only when curator accepts.
- [ ] Do not persist rejected memory candidates except in task episode logs.

### M8 Verification

Run:

```bash
uv run pytest tests/test_worker_memory_proposals.py tests/test_worker.py tests/test_worker_brief.py -q
uv run ruff check src/forge_loop/execution src/forge_loop/memory src/forge_loop/worker.py src/forge_loop/worker_brief.py tests/test_worker_memory_proposals.py
```

**Maturity gate:** Workers can propose memory, but only the curator can promote memory.

---

## M9: Observability And Replay

**Goal:** Make the new architecture visible to operators before relying on it for full autonomy.

**Files:**

- Create: `src/forge_loop/cli_architecture_commands.py`
- Modify: `src/forge_loop/cli.py`
- Modify: `src/forge_loop/snapshot.py`
- Create: `tests/test_cli_architecture.py`

### Task M9.1: Architecture Status CLI

- [ ] Add `forge-loop architecture status --json`.

Required output:

- eventlog backend and last sequence;
- frontier current problem and next expansion;
- active memory count;
- rejected-path count;
- in-flight task count;
- stale projection warnings.

### Task M9.2: Replay Health

- [ ] Add `forge-loop architecture replay-check`.

Required behavior:

- replay projections from event 0;
- compare projection cursors with stored cursors;
- report mismatch as non-zero exit.

### M9 Verification

Run:

```bash
uv run pytest tests/test_cli_architecture.py -q
uv run forge-loop architecture status --json
uv run forge-loop architecture replay-check
```

**Maturity gate:** Operators can see whether the new control-plane state is coherent before the runner depends on it.

---

## M10: Incremental Runner Refactor

**Goal:** Move `_tick` responsibilities into explicit architecture modules without changing external behavior.

**Files:**

- Modify: `src/forge_loop/runner/tick.py`
- Modify: `src/forge_loop/runner/dispatch.py`
- Modify: `src/forge_loop/runner/recovery.py`
- Create or expand: `src/forge_loop/control/maestro.py`
- Create or expand: `src/forge_loop/tasks/orchestrator.py`
- Create or expand: `src/forge_loop/enforcement/gates.py`
- Expand existing runner tests.

### Task M10.1: Extract Planning

- [ ] Move issue selection and ready/open PR repair planning into a pure planning function.

Required behavior:

- input: current config, ready issues, repair PRs, force retry set;
- output: planned task list with reasons;
- no GitHub mutation;
- no worker execution.

### Task M10.2: Extract Dispatch

- [ ] Move worker dispatch into a task-orchestrator function that consumes planned tasks.

Required behavior:

- create task saga;
- allocate worktree policy;
- emit task dispatched event;
- call existing worker runtime;
- record terminal event.

### Task M10.3: Extract Enforcement

- [ ] Move critic, merge-gate, and auto-merge decisions into an enforcement module.

Required behavior:

- critic severity controls merge eligibility;
- risk-gated issues do not auto-merge;
- source issue closed mid-flight refuses merge;
- existing tests keep passing.

### M10 Verification

Run:

```bash
uv run pytest tests/test_runner.py tests/test_runner_merge_gate.py tests/test_runner_critic_actions.py tests/test_runner_recovery.py -q
uv run mypy src/forge_loop/control src/forge_loop/tasks src/forge_loop/runner
```

**Maturity gate:** `_tick` reads as a high-level pipeline and existing runner behavior remains stable.

---

## Testing Discipline

This plan intentionally stages tests:

- M1-M5: contract and persistence tests are worth writing immediately because
  they define stable state boundaries.
- M6-M8: integration tests are written around compatibility with the current
  runner and worker contracts.
- M9-M10: broader behavior tests become valuable once the new architecture is
  visible to operators and starts replacing runner responsibilities.

Avoid broad golden tests for unstable prompts or worker internals. Prefer tests
for durable state, projection replay, task lifecycle transitions, and policy
rendering.

## Suggested Commit Sequence

1. `docs: define long-running agent architecture`
2. `feat(eventlog): add sqlite durable event log`
3. `feat(frontier): dogfood forge-loop frontier cursor`
4. `feat(memory): add curated project memory store`
5. `feat(tasks): add durable task saga lifecycle`
6. `feat(control): assemble maestro boot context`
7. `feat(eventlog): mirror runner events into durable log`
8. `feat(sandbox): add worker capability policies`
9. `feat(memory): route worker observations through curator`
10. `feat(cli): expose architecture status and replay checks`
11. `refactor(runner): move tick responsibilities into control modules`

## Final Acceptance Criteria

The architecture is ready for serious dogfood when all of these are true:

- A hard-killed process can reconstruct the same frontier, active decisions,
  rejected paths, and in-flight tasks.
- A worker can finish or fail without leaving an untracked cleanup obligation.
- A worker can propose memory but cannot directly promote memory.
- The operator can inspect frontier, memory, task, and replay state from CLI.
- `_tick` is no longer the place where strategy, dispatch, recovery, and
  enforcement all live inline.
- The old markdown governance surfaces are inputs to executable gates, not the
  whole architecture.
