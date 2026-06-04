"""Producers for ASSISTANT_THINKING / SYSTEM_MESSAGE (issue #148 follow-up).

PR #198 review (sev3/product): the ``ASSISTANT_THINKING`` and
``SYSTEM_MESSAGE`` enum members + their models had no producer in
``_worker_sdk`` — dormant paths exercised only by the property test, a
bit-rot hazard. These tests pin the now-live producers:

* a ``ThinkingBlock`` in an ``AssistantMessage`` → one typed
  ``assistant_thinking`` event carrying the reasoning text.
* a non-``init`` ``SystemMessage`` → one typed ``system_message`` event
  carrying its ``subtype`` + ``data`` instead of being silently dropped.

Both go through the typed ``emit`` path, so the on-the-wire ``kind`` is the
enum's plain-string value (eventdb / tailer byte-compatible). The fake-SDK
harness mirrors ``test_worker_sdk_session_id.py``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import anyio

from forge_loop import _worker_sdk
from forge_loop._sdk_events import SdkEventKind, parse_sdk_event


class _FakeOptions:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _FakeOptions.last_kwargs = dict(kwargs)
        self.kwargs = kwargs


class _ThinkingBlock:
    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _AssistantMessage:
    def __init__(self, content: list[Any], model: str = "claude-x") -> None:
        self.content = content
        self.model = model


class _SystemMsg:
    def __init__(self, subtype: str, data: dict[str, Any] | None = None) -> None:
        self.subtype = subtype
        self.data = data or {}


def _make_query(messages: list[Any]):
    async def _q(prompt: str, options: Any):  # noqa: ARG001
        for m in messages:
            yield m

    return _q


def _patch_sdk(monkeypatch: Any) -> None:
    """Install a fake ``claude_agent_sdk`` that *does* declare ThinkingBlock."""
    fake = types.ModuleType("claude_agent_sdk")

    class _UserMessage:
        pass

    class _ToolUseBlock:
        pass

    class _ToolResultBlock:
        pass

    class _ResultMessage:  # never instantiated here
        pass

    fake.SystemMessage = _SystemMsg  # type: ignore[attr-defined]
    fake.ResultMessage = _ResultMessage  # type: ignore[attr-defined]
    fake.AssistantMessage = _AssistantMessage  # type: ignore[attr-defined]
    fake.UserMessage = _UserMessage  # type: ignore[attr-defined]
    fake.TextBlock = _TextBlock  # type: ignore[attr-defined]
    fake.ThinkingBlock = _ThinkingBlock  # type: ignore[attr-defined]
    fake.ToolUseBlock = _ToolUseBlock  # type: ignore[attr-defined]
    fake.ToolResultBlock = _ToolResultBlock  # type: ignore[attr-defined]
    fake.ClaudeAgentOptions = _FakeOptions  # type: ignore[attr-defined]
    fake.query = _make_query([])  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def _run(messages: list[Any], tmp_path: Path) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_make_query(messages),
            options_cls=_FakeOptions,
            on_event=captured.append,
        )
    )
    return captured


def test_thinking_block_emits_typed_assistant_thinking(tmp_path: Path, monkeypatch: Any) -> None:
    _patch_sdk(monkeypatch)
    msgs = [_AssistantMessage([_ThinkingBlock("let me reason about this")])]
    captured = _run(msgs, tmp_path)

    thinking = [e for e in captured if e.get("kind") == SdkEventKind.ASSISTANT_THINKING.value]
    assert len(thinking) == 1
    ev = thinking[0]
    # On-the-wire kind is the plain-string enum value (eventdb compatible).
    assert ev["kind"] == "assistant_thinking"
    assert ev["text"] == "let me reason about this"
    # ...and it round-trips back into the typed model on the consumer side.
    parsed = parse_sdk_event(ev)
    assert parsed is not None
    assert parsed.kind is SdkEventKind.ASSISTANT_THINKING


def test_thinking_and_text_blocks_both_emit_in_order(tmp_path: Path, monkeypatch: Any) -> None:
    """A mixed assistant message emits thinking *and* text, not one or the other."""
    _patch_sdk(monkeypatch)
    msgs = [_AssistantMessage([_ThinkingBlock("think"), _TextBlock("answer")])]
    captured = _run(msgs, tmp_path)

    kinds = [e["kind"] for e in captured if e["kind"] in {"assistant_thinking", "assistant_text"}]
    assert kinds == ["assistant_thinking", "assistant_text"]


def test_non_init_system_message_emits_typed_system_message(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _patch_sdk(monkeypatch)
    msgs = [_SystemMsg("compact_boundary", {"reason": "context_full", "n": 3})]
    captured = _run(msgs, tmp_path)

    sysm = [e for e in captured if e.get("kind") == SdkEventKind.SYSTEM_MESSAGE.value]
    assert len(sysm) == 1
    ev = sysm[0]
    assert ev["kind"] == "system_message"
    assert ev["subtype"] == "compact_boundary"
    assert ev["data"] == {"reason": "context_full", "n": 3}
    parsed = parse_sdk_event(ev)
    assert parsed is not None
    assert parsed.kind is SdkEventKind.SYSTEM_MESSAGE


def test_init_system_message_does_not_emit_system_message_event(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Adversarial: the init message is handled (turn_start + mcp filter),
    NOT mis-routed to a SYSTEM_MESSAGE event — only non-init subtypes are."""
    _patch_sdk(monkeypatch)
    msgs = [_SystemMsg("init", {"session_id": "s1", "mcp_servers": []})]
    captured = _run(msgs, tmp_path)

    assert not [e for e in captured if e.get("kind") == "system_message"]
    # init still produces its turn_start event.
    assert [e for e in captured if e.get("kind") == "turn_start"]
