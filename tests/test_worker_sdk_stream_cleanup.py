"""Regression: ``run_sdk_session`` must close the SDK message stream on EVERY
exit path — normal completion, mid-stream error, and (the overnight crash)
timeout-cancellation.

Root cause it guards (observed in the 2026-06-04 overnight run): the SDK
``query()`` async-generator owns the claude **subprocess** transport. When a
worker exceeded its deadline, ``_run_with_timeout`` cancelled the coroutine and
``anyio.run`` closed the event loop while the generator was still open. The
abandoned transport's ``__del__`` later called ``call_soon`` on the closed loop,
printing ``RuntimeError: Event loop is closed`` (and risking an orphaned
subprocess). The fix closes the stream in a shielded ``finally`` so the
subprocess is torn down before the loop closes.

These tests inject a recording stream and assert ``aclose()`` is invoked — a
deterministic contract check (no reliance on GC finalization timing).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import anyio

from forge_loop import _worker_sdk, worker


class _PermissiveOptions:
    """Stand-in for ClaudeAgentOptions — accepts whatever kwargs are passed."""

    def __init__(self, **_kwargs: Any) -> None:  # noqa: D401
        pass


class _RecordingStream:
    """An async message stream that records whether ``aclose()`` was called.

    ``mode`` selects the exit path under test:
      * ``"hang"``  — block forever on the first ``__anext__`` (timeout-cancel).
      * ``"end"``   — raise ``StopAsyncIteration`` immediately (normal finish).
      * ``"raise"`` — raise mid-stream (error path).
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.aclosed = False

    def __aiter__(self) -> _RecordingStream:
        return self

    async def __anext__(self) -> Any:
        if self.mode == "end":
            raise StopAsyncIteration
        if self.mode == "raise":
            raise RuntimeError("boom mid-stream")
        await anyio.sleep(30)  # "hang" — cancelled by the timeout
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclosed = True


def _query_fn(stream: _RecordingStream) -> Any:
    def _qf(prompt: str, options: Any) -> _RecordingStream:  # noqa: ARG001
        return stream

    return _qf


def test_stream_closed_on_timeout_cancellation(tmp_path: Path) -> None:
    """The headline bug: a hung worker hits the deadline → stream still closed."""
    stream = _RecordingStream("hang")

    # The deadline cancellation may surface as TimeoutError or be swallowed into
    # an error result (existing behaviour); either way the contract under test is
    # that the stream is CLOSED, not how the cancellation surfaces.
    with contextlib.suppress(TimeoutError):
        worker._run_with_timeout(
            lambda: _worker_sdk.run_sdk_session(
                "prompt",
                cwd=tmp_path,
                query_fn=_query_fn(stream),
                options_cls=_PermissiveOptions,
            ),
            timeout_s=1,
        )

    assert stream.aclosed is True, (
        "SDK stream was abandoned un-closed on timeout — the dangling subprocess "
        "transport is exactly what prints 'Event loop is closed' at teardown."
    )


def test_stream_closed_on_normal_completion(tmp_path: Path) -> None:
    stream = _RecordingStream("end")

    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_query_fn(stream),
            options_cls=_PermissiveOptions,
        )
    )

    assert stream.aclosed is True


def test_stream_closed_on_midstream_error(tmp_path: Path) -> None:
    stream = _RecordingStream("raise")

    # run_sdk_session swallows the mid-stream error into an ErrorEvent + result,
    # so this returns rather than raising — but the stream must still be closed.
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_query_fn(stream),
            options_cls=_PermissiveOptions,
        )
    )

    assert stream.aclosed is True
