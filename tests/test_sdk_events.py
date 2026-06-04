"""Tests for the typed SDK event boundary (issue #148).

These pin the contract that kills the stringly-typed event boundary that
caused #147 (and the same-shape #97 / #120 / #128): a discriminated union
keyed by a real ``SdkEventKind`` enum, validated at construction so a
producer typo is a hard error instead of a silent bad string.

Manifesto coverage:
* T1 (one test per edge): every enum member routes to its model.
* T5 (property test on user-input-consuming parser): ``parse_sdk_event``
  must not raise on arbitrary text/dicts.
* adversarial: typo at construction, unknown kind, legacy ``type`` shape.
"""

from __future__ import annotations

from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from forge_loop import _sdk_events
from forge_loop._sdk_events import (
    EVENT_MODEL_BY_KIND,
    AssistantTextEvent,
    AssistantThinkingEvent,
    CostTelemetryEvent,
    ErrorEvent,
    FinalResultEvent,
    SdkEventKind,
    SystemMessageEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnStartEvent,
    WorkerMcpFilteredEvent,
    WorkerMcpFilterNoMatchEvent,
    event_to_record,
    parse_sdk_event,
)

# ---------------------------------------------------------------------------
# Enum name regression pin — the names ARE the cross-module contract.
# ---------------------------------------------------------------------------

EXPECTED_KINDS = {
    "ASSISTANT_TEXT": "assistant_text",
    "ASSISTANT_THINKING": "assistant_thinking",
    "TOOL_USE": "tool_use",
    "TOOL_RESULT": "tool_result",
    "SYSTEM_MESSAGE": "system_message",
    "TURN_START": "turn_start",
    "WORKER_MCP_FILTERED": "worker_mcp_filtered",
    "WORKER_MCP_FILTER_NO_MATCH": "worker_mcp_filter_no_match",
    "FINAL_RESULT": "final_result",
    "COST_TELEMETRY": "cost_telemetry",
    "ERROR": "error",
}


def test_enum_names_and_values_are_pinned() -> None:
    """Regression-pin the enum: renaming a member or its wire value is a
    breaking cross-module change and must show up as a failing test."""
    actual = {m.name: m.value for m in SdkEventKind}
    assert actual == EXPECTED_KINDS


def test_str_enum_value_is_plain_string() -> None:
    """``str`` mixin means the value round-trips through JSON as the literal
    on-disk string — eventdb / log tailers stay byte-compatible."""
    assert SdkEventKind.FINAL_RESULT == "final_result"
    assert SdkEventKind.FINAL_RESULT.value == "final_result"


# ---------------------------------------------------------------------------
# Property: every kind in the enum has a typed Event subclass (test plan).
# ---------------------------------------------------------------------------


def test_every_enum_member_has_a_typed_model() -> None:
    assert set(EVENT_MODEL_BY_KIND) == set(SdkEventKind)


def test_every_union_member_is_in_the_registry() -> None:
    """The discriminated union and the kind->model registry agree — no model
    can be added to one without the other (defends against drift)."""
    union_models = set(get_args(get_args(_sdk_events.SdkEvent)[0]))
    assert union_models == set(EVENT_MODEL_BY_KIND.values())


@pytest.mark.parametrize("kind", list(SdkEventKind))
def test_each_model_pins_its_kind(kind: SdkEventKind) -> None:
    """Every model defaults its ``kind`` to the matching enum member."""
    model_cls = EVENT_MODEL_BY_KIND[kind]
    assert model_cls().kind is kind


# ---------------------------------------------------------------------------
# Discriminated-union routing — one edge per kind (manifesto T1).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(SdkEventKind))
def test_parse_routes_to_correct_model(kind: SdkEventKind) -> None:
    parsed = parse_sdk_event({"kind": kind.value})
    assert parsed is not None
    assert parsed.kind is kind
    assert type(parsed) is EVENT_MODEL_BY_KIND[kind]


def test_parse_preserves_envelope_fields() -> None:
    """seq/ts envelope (stamped by the emitter) survives parsing via
    ``extra='allow'`` so nothing downstream loses ordering info."""
    parsed = parse_sdk_event(
        {"kind": "final_result", "result": "hi", "seq": 7, "ts": "2026-01-01T00:00:00Z"}
    )
    assert isinstance(parsed, FinalResultEvent)
    assert parsed.result == "hi"
    assert parsed.seq == 7  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Adversarial / sad-path (manifesto T1 fallthrough, T2 false-case).
# ---------------------------------------------------------------------------


def test_producer_typo_kind_raises_at_construction() -> None:
    """``SdkEventKind('typo')`` is a hard error — the whole point of #148."""
    with pytest.raises(ValueError):
        SdkEventKind("typo")


def test_model_rejects_foreign_kind_literal() -> None:
    """Constructing a model with the wrong kind literal fails validation."""
    with pytest.raises(ValidationError):
        FinalResultEvent(kind=SdkEventKind.ERROR)  # type: ignore[arg-type]


def test_parse_unknown_kind_returns_none() -> None:
    assert parse_sdk_event({"kind": "totally_unknown"}) is None


def test_parse_legacy_type_shape_returns_none() -> None:
    """The #147 legacy ``type``-keyed shape has no ``kind`` discriminator,
    so it does NOT parse — the consumer ignores it rather than mis-routing."""
    assert parse_sdk_event({"type": "final_result", "text": "x"}) is None


def test_parse_missing_kind_returns_none() -> None:
    assert parse_sdk_event({"text": "no kind here"}) is None


# ---------------------------------------------------------------------------
# Round-trip: typed -> record -> parsed back to the same typed model.
# ---------------------------------------------------------------------------


def test_event_to_record_renders_kind_as_plain_string() -> None:
    rec = event_to_record(FinalResultEvent(result="done"))
    assert rec["kind"] == "final_result"
    assert isinstance(rec["kind"], str)
    # and round-trips back to the same model
    back = parse_sdk_event(rec)
    assert isinstance(back, FinalResultEvent)
    assert back.result == "done"


@pytest.mark.parametrize(
    "event",
    [
        AssistantTextEvent(text="hi"),
        AssistantThinkingEvent(text="reasoning"),
        SystemMessageEvent(subtype="compact_boundary", data={"reason": "context_full"}),
        ToolUseEvent(tool="Bash", input={"command": "ls"}, tool_use_id="t1"),
        ToolResultEvent(tool_use_id="t1", is_error=True, content="boom"),
        TurnStartEvent(data={"session_id": "s1"}),
        WorkerMcpFilteredEvent(kept=["forge-loop"], dropped=["gmail"], configured=["forge-loop"]),
        WorkerMcpFilterNoMatchEvent(configured=["typo"], available=["forge-loop"], fallback=["x"]),
        FinalResultEvent(result="r", cost_usd=1.5, num_turns=3),
        CostTelemetryEvent(input_tokens=10, output_tokens=5, cache_hit_ratio=0.4),
        ErrorEvent(error_type="rate_limit", message="429", retry_hint="backoff"),
    ],
)
def test_record_round_trip_preserves_model_type(event: object) -> None:
    rec = event_to_record(event)  # type: ignore[arg-type]
    back = parse_sdk_event(rec)
    assert type(back) is type(event)


# ---------------------------------------------------------------------------
# Property-based (manifesto T5): parser must not raise on arbitrary input.
# ---------------------------------------------------------------------------


@given(
    st.dictionaries(
        keys=st.text(),
        values=st.one_of(st.text(), st.integers(), st.booleans(), st.none()),
    )
)
def test_parse_never_raises_on_arbitrary_dict(payload: dict[str, object]) -> None:
    # Returns either a typed event or None — never raises, even on garbage.
    result = parse_sdk_event(payload)
    assert result is None or isinstance(result, tuple(EVENT_MODEL_BY_KIND.values()))


@given(st.text())
def test_parse_with_arbitrary_kind_string_is_safe(kind: str) -> None:
    result = parse_sdk_event({"kind": kind})
    if kind in {m.value for m in SdkEventKind}:
        assert result is not None
    else:
        assert result is None
