# Task Saga Leases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist task saga leases, heartbeats, stale-worker detection, terminal-state guards, and compensation records for issue #168.

**Architecture:** Extend the existing `TaskSaga` dataclass with lease metadata and add transition methods to `SqliteTaskSagaStore`. Keep the persistence boundary under `forge_loop.tasks.store` and mirror the same contract in `FakeTaskSagaStore`.

**Tech Stack:** Python dataclasses, `datetime` UTC timestamps, SQLite, pytest.

---

### Task 1: Lifecycle Contract Tests

**Files:**
- Modify: `tests/test_task_saga_store.py`

- [ ] Add tests covering planned task creation, lease acquisition, heartbeat extension, stale lease listing, terminal lease rejection, failed compensation preservation, and SQLite reopen persistence.
- [ ] Run `env -u VIRTUAL_ENV uv run --extra dev pytest tests/test_task_saga_store.py::TestTaskSagaLeaseLifecycle -q` and verify the tests fail because the new store methods/fields do not exist yet.

### Task 2: Saga Models and Store Transitions

**Files:**
- Modify: `src/forge_loop/tasks/saga.py`
- Modify: `src/forge_loop/tasks/store.py`
- Modify: `src/forge_loop/tasks/__init__.py`
- Modify: `src/forge_loop/_testing/task_saga_store.py`

- [ ] Add lease metadata fields and store exceptions.
- [ ] Persist the metadata in SQLite with a compatibility migration for existing DBs.
- [ ] Implement `create`, `acquire_lease`, `heartbeat`, `list_stale`, and terminal mark methods.
- [ ] Mirror behavior in the fake store.
- [ ] Re-run the focused lifecycle tests until they pass.

### Task 3: Verification and Shipping

**Files:**
- Changed files from Tasks 1-2.

- [ ] Run Lumen discovery if available and targeted discovered test classes.
- [ ] Run required ruff check and format checks on changed files.
- [ ] Commit with a body referencing #168.
- [ ] Push and create the PR with `Fixes #168`.
