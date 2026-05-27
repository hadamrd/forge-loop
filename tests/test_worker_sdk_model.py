"""Tests for issue #34: per-role model + thinking-budget threading into the SDK.

The contract being verified here is mechanical: when ``run_sdk_session`` is
given a ``model`` (and optional ``thinking_budget``), those values MUST be
present on the ``ClaudeAgentOptions`` instance the SDK sees, AND the
returned ``SDKRunResult.model`` MUST fall back to the *requested* model
when the response carried no model field (regression point flagged in the
issue's acceptance criteria — ledger rows used to record empty strings).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from forge_loop import _worker_sdk


class _FakeOptions:
    """Captures every kwarg the SDK would have received."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        # Mutate the class-level dict so tests can inspect across invocations.
        _FakeOptions.last_kwargs = dict(kwargs)
        self.kwargs = kwargs


class _NoThinkingOptions:
    """Older-SDK shape: rejects ``thinking_budget`` via TypeError."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        if "thinking_budget" in kwargs:
            raise TypeError("unexpected keyword argument 'thinking_budget'")
        _NoThinkingOptions.last_kwargs = dict(kwargs)


async def _empty_query(prompt: str, options: Any):  # noqa: ARG001
    """A query() stand-in that yields no messages — exercises the empty path."""
    if False:
        yield None
    return


def test_run_sdk_session_passes_model_into_options(tmp_path: Path) -> None:
    """Happy path: model="claude-opus-4-7" lands on ClaudeAgentOptions."""
    _FakeOptions.last_kwargs = {}
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_FakeOptions,
            model="claude-opus-4-7",
            thinking_budget="medium",
        )
    )
    assert _FakeOptions.last_kwargs.get("model") == "claude-opus-4-7"
    assert _FakeOptions.last_kwargs.get("thinking_budget") == "medium"
    # No response → requested model is preserved on the result for ledger.
    assert result.model == "claude-opus-4-7"


def test_run_sdk_session_no_model_no_thinking(tmp_path: Path) -> None:
    """Sad path: when no model is requested, neither knob lands on options."""
    _FakeOptions.last_kwargs = {}
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_FakeOptions,
        )
    )
    assert "model" not in _FakeOptions.last_kwargs
    assert "thinking_budget" not in _FakeOptions.last_kwargs


def test_run_sdk_session_thinking_off_skips_thinking_kwarg(tmp_path: Path) -> None:
    """``thinking=off`` is the documented disable; the kwarg must not be sent."""
    _FakeOptions.last_kwargs = {}
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_FakeOptions,
            model="claude-sonnet-4-6",
            thinking_budget="off",
        )
    )
    assert _FakeOptions.last_kwargs.get("model") == "claude-sonnet-4-6"
    assert "thinking_budget" not in _FakeOptions.last_kwargs


def test_run_sdk_session_falls_back_when_sdk_rejects_thinking(tmp_path: Path) -> None:
    """Older SDK that doesn't know about ``thinking_budget`` must not break.

    The role still gets its model; thinking is silently dropped.
    """
    _NoThinkingOptions.last_kwargs = {}
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_NoThinkingOptions,
            model="claude-opus-4-7",
            thinking_budget="high",
        )
    )
    assert _NoThinkingOptions.last_kwargs.get("model") == "claude-opus-4-7"
    assert "thinking_budget" not in _NoThinkingOptions.last_kwargs
    assert result.model == "claude-opus-4-7"


def test_ledger_model_falls_back_to_requested_when_response_empty(
    tmp_path: Path,
) -> None:
    """Ledger acceptance criterion: ``model`` field on SDKRunResult is the
    *requested* model when the response has no model field — even when the
    session errors with an empty stream.
    """
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_FakeOptions,
            model="claude-opus-4-7",
        )
    )
    assert result.model == "claude-opus-4-7"


def test_ledger_model_empty_string_when_neither_requested_nor_observed(
    tmp_path: Path,
) -> None:
    """Sanity: with no requested model and no response, we get empty string
    (not a NoneType crash)."""
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_FakeOptions,
        )
    )
    assert result.model == ""
