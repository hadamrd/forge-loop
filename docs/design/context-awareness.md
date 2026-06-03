# Context-window awareness & self-compaction

> Verified against the installed `claude_agent_sdk` on 2026-06-03. If the SDK
> is upgraded, re-check the field/hook names below before relying on them.

## Why

A long-running agent session (a worker chewing a large issue; a future
agentic maestro) accumulates context until the window fills. We want it to
notice the pressure, persist the *valuable* distilled state, and reset
cleanly — instead of degrading or hard-failing at the limit. The control
plane (`.forge/` frontier + memory + boot context) already provides the
"persist" half; this note records how to wire the "notice and act" half.

## The SDK gives context fullness directly — do not estimate it

`ClaudeSDKClient.get_context_usage()` returns a `ContextUsageResponse`
(`from claude_agent_sdk import ContextUsageResponse`) — the same data as the
CLI `/context` command:

- `percentage: float` — **context window used, 0–100.** This is the number;
  no token math, no model self-report needed.
- `totalTokens: int` — tokens currently in context.
- `maxTokens: int` — effective limit (already reduced by the autocompact buffer).
- `rawMaxTokens: int` — the model's true window.
- `model: str`.
- `isAutoCompactEnabled: bool`, `autoCompactThreshold: int` — token count where
  the SDK's built-in autocompact fires.
- `categories`, `mcpTools`, `memoryFiles`, `agents` — per-bucket token breakdown
  (system prompt, tools, messages; per-MCP-tool; per-CLAUDE.md; per-agent).

Do **not** ask the model "how full are you?" — model introspection about its
own context is unreliable. Read `percentage` instead.

## The `PreCompact` hook is the seam for "persist before compaction"

The SDK exposes a `PreCompact` hook (`PreCompactHookInput`,
`hook_event_name="PreCompact"`) that fires **right before** the SDK compacts
the transcript. Register it to flush distilled state into `memory.db` /
`frontier.yaml` at the exact moment before compaction — no polling, no race.
Autocompact is on by default with `autoCompactThreshold`.

## Integration caveat for forge-loop

`get_context_usage()` is a method on the **connected `ClaudeSDKClient`** (it's
a control request to a live session). Our workers currently use the one-shot
`query()` streaming helper (`async for message in query_fn(...)` in
`_worker_sdk.py`), which does **not** hold a client you can call
`get_context_usage()` on. Two paths:

1. **Hook-only (smaller):** register the `PreCompact` hook in the existing
   options and do the checkpoint there. Hooks work with the current dispatch
   path; no client refactor needed.
2. **Polling (larger):** switch the worker to the `ClaudeSDKClient`
   connect/query/receive style so it can poll `get_context_usage()["percentage"]`
   and decide to checkpoint+reset proactively.

Note: `persist_sdk_result` (in `runner/dispatch.py`) already captures the API
`usage` token counts (`input_tokens`, `cache_read_input_tokens`, …) into the
`cost_telemetry` event. That is **API usage**, not the same as the SDK's
context `percentage` — to get the percentage you need `get_context_usage()` or
the `/context`-equivalent, not the per-response `usage`.

## Proposed design: context-pressure → checkpoint → reseed

Two flavours; the second is more on-brand (it matches our crash-recovery path).

1. **In-context compaction** — let the SDK autocompact; use the `PreCompact`
   hook to persist distilled state first. Keeps the session alive but lossy.
2. **Externalize + fresh session** — at a threshold (`PreCompact` hook, or a
   polled `percentage`): curate the valuable working state into `memory.db` /
   `frontier.yaml` via the `MemoryCurator` (distilled *facts*, not raw
   transcript), end the session, and start a clean one seeded by
   `BootContext.summary()`. This is "reboot from durable state" — the same
   mechanism as crash recovery, triggered by token pressure instead of SIGKILL.

A concrete slice: a `PreCompact` checkpoint hook on worker sessions that
distills the worker's valuable state into curated memory before compaction,
emitting a `context_checkpoint` event carrying `percentage`. Fully testable
in-sandbox with synthetic usage numbers — no live model required.

## Guiding principle

The cheapest way to handle context fullness is to need little context:
keep the orchestrator **stateless** (today's maestro is deterministic code with
no window), keep workers short-lived and single-purpose, and externalize
durable state. Then context pressure is rare, and when it happens a
checkpoint-and-reboot is trivial because the durable store — not the
transcript — is the source of truth. Context-pressure self-compaction matters
for **workers** on big issues, not for the (stateless) maestro.
