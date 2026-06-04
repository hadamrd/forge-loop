"""Typed, discriminated SDK event boundary (issue #148).

The worker SDK (:mod:`forge_loop._worker_sdk`) streams one event per typed
message it receives from the Claude Agent SDK. Historically every event was a
bare ``dict`` keyed by a *string literal* ``"kind"`` — and every consumer
re-derived that discriminator with a string ``==`` comparison. That is exactly
the failure shape the quality manifesto bans ("No stringly-typed cross-module
event boundaries"):

* ``#147`` — ``_critic_sdk`` checked ``event["type"] == "result"`` while
  ``_worker_sdk`` emitted ``event["kind"] == "final_result"``. A two-field-name
  mismatch silently ate every assistant text, breaking critic + PO +
  brainstormer SDK paths in production until it was hot-fixed.
* ``#97`` / ``#120`` / ``#128`` — same shape, different discriminators.

This module replaces the string boundary with:

* :class:`SdkEventKind` — a ``str`` ``Enum`` enumerating **every** kind the
  worker SDK emits. Producers and consumers import this single source of truth
  and compare with ``is`` (identity), which the type checker enforces.
* one Pydantic model per kind, each pinning ``kind`` to a ``Literal`` enum
  member so a typo fails at *construction* (``SdkEventKind("typo")`` raises).
* :data:`SdkEvent` — a Pydantic *discriminated union* over ``kind`` so a raw
  dict off the wire validates into exactly the right typed model.

Consumers call :func:`parse_sdk_event` to turn an on-the-wire dict into a typed
:data:`SdkEvent` (or ``None`` for an unrecognised / malformed payload) and then
branch on ``event.kind is SdkEventKind.FINAL_RESULT`` — no string literals, no
field-name drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


class SdkEventKind(StrEnum):
    """Every event kind the worker SDK emits.

    :class:`~enum.StrEnum` so the value round-trips through JSON as a plain
    string (the on-disk ``events.jsonl`` shape is unchanged) while in-process
    code gets a real enum it can compare with ``is``.

    Every member below has a live producer in
    :mod:`forge_loop._worker_sdk`: ``ASSISTANT_THINKING`` is emitted from a
    ``ThinkingBlock`` (extended-thinking surface), ``SYSTEM_MESSAGE`` from any
    non-``init`` ``SystemMessage``, and ``WORKER_MCP_FILTER_NO_MATCH`` from
    :func:`forge_loop._worker_sdk.resolve_mcp_filter`. No member is a dormant /
    forward-declared path — the manifesto rule this module adds bans
    stringly-typed boundaries, and a bit-rotting enum member is the same
    drift hazard in slow motion.
    """

    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_THINKING = "assistant_thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    SYSTEM_MESSAGE = "system_message"
    TURN_START = "turn_start"
    WORKER_MCP_FILTERED = "worker_mcp_filtered"
    WORKER_MCP_FILTER_NO_MATCH = "worker_mcp_filter_no_match"
    FINAL_RESULT = "final_result"
    COST_TELEMETRY = "cost_telemetry"
    ERROR = "error"


class _SdkEventBase(BaseModel):
    """Base for every typed SDK event.

    ``extra="allow"`` so the seq/ts envelope stamped by the emitter (and any
    forward-compatible field a newer SDK adds) round-trips without tripping
    validation — the discriminator + declared payload fields are still
    type-checked.

    ``kind`` is declared here (widened to the full :class:`SdkEventKind`) so a
    consumer holding a parsed-but-not-yet-narrowed event can read ``.kind`` and
    branch on it; each concrete subclass overrides it with a ``Literal`` member
    so the discriminated union can route a raw dict to exactly one model.
    """

    model_config = ConfigDict(extra="allow")

    kind: SdkEventKind


class AssistantTextEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.ASSISTANT_TEXT] = SdkEventKind.ASSISTANT_TEXT
    text: str = ""


class AssistantThinkingEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.ASSISTANT_THINKING] = SdkEventKind.ASSISTANT_THINKING
    text: str = ""


class ToolUseEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.TOOL_USE] = SdkEventKind.TOOL_USE
    tool: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    tool_use_id: str = ""


class ToolResultEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.TOOL_RESULT] = SdkEventKind.TOOL_RESULT
    tool_use_id: str = ""
    is_error: bool = False
    content: str = ""


class SystemMessageEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.SYSTEM_MESSAGE] = SdkEventKind.SYSTEM_MESSAGE
    subtype: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class TurnStartEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.TURN_START] = SdkEventKind.TURN_START
    data: dict[str, Any] = Field(default_factory=dict)


class WorkerMcpFilteredEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.WORKER_MCP_FILTERED] = SdkEventKind.WORKER_MCP_FILTERED
    kept: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    configured: list[str] = Field(default_factory=list)


class WorkerMcpFilterNoMatchEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.WORKER_MCP_FILTER_NO_MATCH] = SdkEventKind.WORKER_MCP_FILTER_NO_MATCH
    configured: list[str] = Field(default_factory=list)
    available: list[str] = Field(default_factory=list)
    fallback: list[str] = Field(default_factory=list)


class FinalResultEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.FINAL_RESULT] = SdkEventKind.FINAL_RESULT
    result: str = ""
    cost_usd: float = 0.0
    usage: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    num_turns: int = 0
    is_error: bool = False
    sdk_session_id: str | None = None


class CostTelemetryEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.COST_TELEMETRY] = SdkEventKind.COST_TELEMETRY
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_hit_ratio: float = 0.0
    cost_usd: float = 0.0
    model: str = ""
    sdk_session_id: str | None = None
    resumed: bool = False


class ErrorEvent(_SdkEventBase):
    kind: Literal[SdkEventKind.ERROR] = SdkEventKind.ERROR
    error_type: str = ""
    message: str = ""
    retry_hint: str | None = None


# Discriminated union — Pydantic routes a raw dict to the right model by its
# ``kind`` discriminator. Adding a member to :class:`SdkEventKind` without a
# matching model here is caught by ``test_sdk_events`` (the property test that
# asserts every enum member has a typed subclass).
SdkEvent = Annotated[
    AssistantTextEvent
    | AssistantThinkingEvent
    | ToolUseEvent
    | ToolResultEvent
    | SystemMessageEvent
    | TurnStartEvent
    | WorkerMcpFilteredEvent
    | WorkerMcpFilterNoMatchEvent
    | FinalResultEvent
    | CostTelemetryEvent
    | ErrorEvent,
    Field(discriminator="kind"),
]


# kind -> model, the single mapping the property test and any registry-style
# consumer can rely on.
EVENT_MODEL_BY_KIND: dict[SdkEventKind, type[_SdkEventBase]] = {
    SdkEventKind.ASSISTANT_TEXT: AssistantTextEvent,
    SdkEventKind.ASSISTANT_THINKING: AssistantThinkingEvent,
    SdkEventKind.TOOL_USE: ToolUseEvent,
    SdkEventKind.TOOL_RESULT: ToolResultEvent,
    SdkEventKind.SYSTEM_MESSAGE: SystemMessageEvent,
    SdkEventKind.TURN_START: TurnStartEvent,
    SdkEventKind.WORKER_MCP_FILTERED: WorkerMcpFilteredEvent,
    SdkEventKind.WORKER_MCP_FILTER_NO_MATCH: WorkerMcpFilterNoMatchEvent,
    SdkEventKind.FINAL_RESULT: FinalResultEvent,
    SdkEventKind.COST_TELEMETRY: CostTelemetryEvent,
    SdkEventKind.ERROR: ErrorEvent,
}


_SDK_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(SdkEvent)


def event_to_record(event: _SdkEventBase) -> dict[str, Any]:
    """Serialise a typed event to its on-the-wire dict.

    ``mode="json"`` renders the ``kind`` str-enum back to its plain string
    value so the on-disk ``events.jsonl`` shape is byte-identical to the
    legacy raw-dict path.
    """
    return event.model_dump(mode="json")


def parse_sdk_event(data: Mapping[str, Any]) -> _SdkEventBase | None:
    """Validate a raw on-the-wire dict into a typed :data:`SdkEvent`.

    Returns ``None`` for an unrecognised ``kind`` (the legacy ``type``-keyed
    shape, a typo, a future kind we don't model yet) or a payload that fails
    validation — consumers branch on a real enum, never a string literal, and
    an unparseable event is simply ignored rather than crashing the stream.
    """
    try:
        return cast(_SdkEventBase, _SDK_EVENT_ADAPTER.validate_python(dict(data)))
    except ValidationError:
        return None


__all__ = [
    "AssistantTextEvent",
    "AssistantThinkingEvent",
    "CostTelemetryEvent",
    "ErrorEvent",
    "EVENT_MODEL_BY_KIND",
    "FinalResultEvent",
    "SdkEvent",
    "SdkEventKind",
    "SystemMessageEvent",
    "ToolResultEvent",
    "ToolUseEvent",
    "TurnStartEvent",
    "WorkerMcpFilterNoMatchEvent",
    "WorkerMcpFilteredEvent",
    "event_to_record",
    "parse_sdk_event",
]
