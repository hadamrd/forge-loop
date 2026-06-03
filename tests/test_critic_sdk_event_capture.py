"""Regression pin for the event-capture field-name mismatch in
``_critic_sdk.run_critic_sdk`` (dogfood-caught running brainstormer on a large project).

Pre-fix the capture used ``event["type"]`` + checked for kind ``"result"``,
but forge_loop._worker_sdk actually emits ``event["kind"]`` with the
session-end kind ``"final_result"``. The mismatch silently ate every
assistant text, breaking critic + PO + brainstormer SDK paths in
production with the symptom ``empty SDK output``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_loop._critic_sdk import run_critic_sdk


@dataclass
class _FakeResult:
    """Mimics the SDKRunResult shape (final_result_text, error)."""
    final_result_text: str = ""
    error: str | None = None


def test_capture_picks_up_final_result_kind(tmp_path: Path) -> None:
    """The headline regression pin — a final_result event must populate
    ``last_message`` even when the SDKRunResult.final_result_text is empty.
    """
    captured_text = "the actual model response was here"

    async def fake_query(*_a, **_kw):
        # Async generator yielding nothing — the on_event callback path
        # is what we're exercising.
        return
        yield  # pragma: no cover

    class _FakeOptions:
        def __init__(self, **_kw): pass

    def patched_run_sdk_session(*args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event:
            # Drive the same event shape forge_loop._worker_sdk uses today.
            on_event({"kind": "assistant_text", "text": "thinking out loud"})
            on_event({"kind": "tool_use", "tool": "Bash"})
            on_event({"kind": "tool_result", "is_error": False})
            on_event({"kind": "final_result", "text": captured_text})

        async def _fake_session():
            return _FakeResult(final_result_text="")  # mimic missing fallback
        return _fake_session()

    import forge_loop._critic_sdk as critic_sdk_mod
    critic_sdk_mod.run_critic_sdk.__globals__["run_sdk_session"] = patched_run_sdk_session  # type: ignore[attr-defined]

    # Re-import the actual symbol on the module
    from forge_loop import _worker_sdk as worker_sdk_mod
    orig = worker_sdk_mod.run_sdk_session
    worker_sdk_mod.run_sdk_session = patched_run_sdk_session  # type: ignore[assignment]
    try:
        result = run_critic_sdk(
            "test prompt", cwd=tmp_path, timeout_s=10, model="claude-opus-4-7",
        )
    finally:
        worker_sdk_mod.run_sdk_session = orig  # type: ignore[assignment]

    assert result.last_message == captured_text, (
        f"capture must read event['kind']=='final_result'; got last_message={result.last_message!r}"
    )
    assert result.timed_out is False
    assert result.error is None


def test_capture_falls_back_to_final_result_text_attr(tmp_path: Path) -> None:
    """If no event-stream final_result fired (e.g. SDK shape evolved),
    we still pick up the text from the result's final_result_text attr."""

    def patched_run_sdk_session(*args, **kwargs):
        async def _fake_session():
            return _FakeResult(final_result_text="from-result-attribute")
        return _fake_session()

    from forge_loop import _worker_sdk as worker_sdk_mod
    orig = worker_sdk_mod.run_sdk_session
    worker_sdk_mod.run_sdk_session = patched_run_sdk_session  # type: ignore[assignment]
    try:
        result = run_critic_sdk(
            "test prompt", cwd=tmp_path, timeout_s=10, model="claude-opus-4-7",
        )
    finally:
        worker_sdk_mod.run_sdk_session = orig  # type: ignore[assignment]

    assert result.last_message == "from-result-attribute"


def test_legacy_type_field_still_tolerated(tmp_path: Path) -> None:
    """Defensive: if a future SDK version reverts to ``event['type']``,
    the capture should still work — we accept either field name."""
    def patched_run_sdk_session(*args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event:
            on_event({"type": "final_result", "text": "legacy-typed"})

        async def _fake_session():
            return _FakeResult()
        return _fake_session()

    from forge_loop import _worker_sdk as worker_sdk_mod
    orig = worker_sdk_mod.run_sdk_session
    worker_sdk_mod.run_sdk_session = patched_run_sdk_session  # type: ignore[assignment]
    try:
        result = run_critic_sdk(
            "p", cwd=tmp_path, timeout_s=10, model="claude-opus-4-7",
        )
    finally:
        worker_sdk_mod.run_sdk_session = orig  # type: ignore[assignment]

    assert result.last_message == "legacy-typed"
