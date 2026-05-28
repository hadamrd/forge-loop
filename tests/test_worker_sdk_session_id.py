"""Tests for issue #109: SDK session_id wiring for warm prompt cache.

Acceptance criteria under test:

* ``run_sdk_session()`` returns ``sdk_session_id`` on the SDKRunResult.
* When ``resume=<id>`` is passed, it lands on ClaudeAgentOptions.
* A ``cost_telemetry`` event fires carrying ``cache_hit_ratio``.
* ``persist_sdk_result`` writes the id through the store and emits
  cost telemetry.
* ``resume_kwargs_for`` returns the resume payload only in RUNNING /
  REVISING; empty dict otherwise (DISPATCHED cold-start, AWAITING_CRITIC
  paused, missing session).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from forge_loop import _worker_sdk
from forge_loop.runner.dispatch import (
    persist_sdk_result,
    resume_kwargs_for,
)
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState

# ---------------------------------------------------------------------------
# Fake SDK harness — mirrors test_worker_sdk_model.py style.
# ---------------------------------------------------------------------------


class _FakeOptions:
    """Captures every kwarg the SDK would have received."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _FakeOptions.last_kwargs = dict(kwargs)
        self.kwargs = kwargs


class _SystemInitMsg:
    """Stand-in for claude_agent_sdk.SystemMessage(subtype='init')."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.subtype = "init"
        self.data = data


class _ResultMsg:
    """Stand-in for claude_agent_sdk.ResultMessage."""

    def __init__(
        self,
        *,
        result: str = "",
        usage: dict[str, Any] | None = None,
        session_id: str | None = None,
        cost_usd: float = 0.0,
        num_turns: int = 1,
    ) -> None:
        self.result = result
        self.usage = usage or {}
        self.session_id = session_id
        self.total_cost_usd = cost_usd
        self.is_error = False
        self.num_turns = num_turns


def _make_query(messages: list[Any]):
    async def _q(prompt: str, options: Any):  # noqa: ARG001
        for m in messages:
            yield m

    return _q


def _patch_sdk_message_types(monkeypatch: Any) -> None:
    """Inject our fake message classes as the SDK message-type imports.

    ``run_sdk_session`` performs the SDK imports *inside* the function
    (so the SDK is an optional dep); we shim ``sys.modules`` so isinstance
    checks against the fake messages succeed.
    """
    import sys
    import types

    fake = types.ModuleType("claude_agent_sdk")

    class _AssistantMessage:  # never instantiated in these tests
        pass

    class _UserMessage:
        pass

    class _TextBlock:
        pass

    class _ToolUseBlock:
        pass

    class _ToolResultBlock:
        pass

    fake.SystemMessage = _SystemInitMsg  # type: ignore[attr-defined]
    fake.ResultMessage = _ResultMsg  # type: ignore[attr-defined]
    fake.AssistantMessage = _AssistantMessage  # type: ignore[attr-defined]
    fake.UserMessage = _UserMessage  # type: ignore[attr-defined]
    fake.TextBlock = _TextBlock  # type: ignore[attr-defined]
    fake.ToolUseBlock = _ToolUseBlock  # type: ignore[attr-defined]
    fake.ToolResultBlock = _ToolResultBlock  # type: ignore[attr-defined]
    fake.ClaudeAgentOptions = _FakeOptions  # type: ignore[attr-defined]
    fake.query = _make_query([])  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


# ---------------------------------------------------------------------------
# _worker_sdk.run_sdk_session — session_id capture + resume + telemetry.
# ---------------------------------------------------------------------------


def test_sdk_session_id_captured_from_init_message(tmp_path: Path, monkeypatch: Any) -> None:
    """Happy path: SystemMessage(init).data['session_id'] lands on the result."""
    _patch_sdk_message_types(monkeypatch)
    msgs = [_SystemInitMsg({"session_id": "sdk-init-XYZ", "mcp_servers": []})]
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_make_query(msgs),
            options_cls=_FakeOptions,
        )
    )
    assert result.sdk_session_id == "sdk-init-XYZ"


def test_sdk_session_id_result_message_takes_precedence(tmp_path: Path, monkeypatch: Any) -> None:
    """A ResultMessage.session_id overrides the init one (newer SDK shape)."""
    _patch_sdk_message_types(monkeypatch)
    msgs = [
        _SystemInitMsg({"session_id": "init-OLD", "mcp_servers": []}),
        _ResultMsg(
            result='{"pr": null, "status": "no_pr"}',
            session_id="result-NEW",
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 400,
            },
        ),
    ]
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_make_query(msgs),
            options_cls=_FakeOptions,
        )
    )
    assert result.sdk_session_id == "result-NEW"
    # cache_hit_ratio = 400 / (400 + 100) = 0.8
    assert abs(result.cache_hit_ratio - 0.8) < 1e-6


def test_resume_kwarg_lands_on_options(tmp_path: Path) -> None:
    """Sad path inverse: when resume='abc' is passed, options sees it."""
    _FakeOptions.last_kwargs = {}

    async def _q(prompt: str, options: Any):  # noqa: ARG001
        if False:
            yield None
        return

    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_q,
            options_cls=_FakeOptions,
            resume="sdk-prior-RESUME",
        )
    )
    assert _FakeOptions.last_kwargs.get("resume") == "sdk-prior-RESUME"


def test_no_resume_kwarg_when_not_passed(tmp_path: Path) -> None:
    """Adversarial: omitting resume must not leak the kwarg onto options."""
    _FakeOptions.last_kwargs = {}

    async def _q(prompt: str, options: Any):  # noqa: ARG001
        if False:
            yield None
        return

    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_q,
            options_cls=_FakeOptions,
        )
    )
    assert "resume" not in _FakeOptions.last_kwargs


def test_cost_telemetry_event_emitted_with_cache_ratio(tmp_path: Path, monkeypatch: Any) -> None:
    """Acceptance: a ``cost_telemetry`` event fires with ``cache_hit_ratio``."""
    _patch_sdk_message_types(monkeypatch)
    msgs = [
        _SystemInitMsg({"session_id": "s1", "mcp_servers": []}),
        _ResultMsg(
            result='{"pr": null, "status": "no_pr"}',
            session_id="s1",
            usage={
                "input_tokens": 250,
                "output_tokens": 100,
                "cache_read_input_tokens": 750,
                "cache_creation_input_tokens": 0,
            },
            cost_usd=0.012,
        ),
    ]
    captured: list[dict[str, Any]] = []
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_make_query(msgs),
            options_cls=_FakeOptions,
            on_event=captured.append,
            resume="s1",
        )
    )
    telem = [e for e in captured if e.get("kind") == "cost_telemetry"]
    assert len(telem) == 1, f"expected exactly one cost_telemetry event, got {telem}"
    ev = telem[0]
    assert ev["input_tokens"] == 250
    assert ev["output_tokens"] == 100
    assert ev["cache_read_input_tokens"] == 750
    # ratio = 750 / (750+250) = 0.75
    assert abs(ev["cache_hit_ratio"] - 0.75) < 1e-6
    assert ev["resumed"] is True
    assert ev["sdk_session_id"] == "s1"
    assert abs(result.cache_hit_ratio - 0.75) < 1e-6


# ---------------------------------------------------------------------------
# compute_cache_hit_ratio — edge cases.
# ---------------------------------------------------------------------------


def test_cache_hit_ratio_zero_usage_returns_zero() -> None:
    """Empty/missing usage must not crash — telemetry stays defensive."""
    assert _worker_sdk.compute_cache_hit_ratio({}) == 0.0


def test_cache_hit_ratio_handles_garbage_types() -> None:
    """Non-numeric values fall back to 0 instead of raising."""
    assert (
        _worker_sdk.compute_cache_hit_ratio(
            {"input_tokens": "oops", "cache_read_input_tokens": None}
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# Dispatch helpers — persist_sdk_result + resume_kwargs_for.
# ---------------------------------------------------------------------------


class _Result:
    """Plain-attr stand-in for SDKRunResult."""

    def __init__(
        self,
        *,
        sdk_session_id: str | None = None,
        usage: dict[str, Any] | None = None,
        cache_hit_ratio: float = 0.0,
        cost_usd: float = 0.0,
        model: str = "claude-opus-4-7",
    ) -> None:
        self.sdk_session_id = sdk_session_id
        self.usage = usage or {}
        self.cache_hit_ratio = cache_hit_ratio
        self.cost_usd = cost_usd
        self.model = model


def _new_store_with_session(
    state: WorkerState = WorkerState.RUNNING,
) -> tuple[WorkerSessionStore, str]:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=109, branch="loop/109-test")
    # Walk to target state via legal transitions.
    if state is WorkerState.DISPATCHED:
        return store, sess.session_id
    store.transition_to(sess.session_id, WorkerState.RUNNING, reason="t")
    if state is WorkerState.RUNNING:
        return store, sess.session_id
    store.transition_to(sess.session_id, WorkerState.AWAITING_CRITIC, reason="t")
    if state is WorkerState.AWAITING_CRITIC:
        return store, sess.session_id
    store.transition_to(sess.session_id, WorkerState.REVISING, reason="t")
    return store, sess.session_id


def test_persist_sdk_result_saves_id_and_emits_telemetry() -> None:
    """Happy path: id lands in store, cost_telemetry emitted with the ratio."""
    store, sid = _new_store_with_session(WorkerState.RUNNING)
    captured: list[tuple[str, dict[str, Any]]] = []

    def _emit(kind: str, **kw: Any) -> None:
        captured.append((kind, kw))

    res = _Result(
        sdk_session_id="sdk-RUN-1",
        usage={
            "input_tokens": 200,
            "output_tokens": 100,
            "cache_read_input_tokens": 600,
        },
        cache_hit_ratio=0.75,
        cost_usd=0.01,
    )
    persist_sdk_result(store=store, session_id=sid, result=res, emit=_emit)

    assert store.get(sid).sdk_session_id == "sdk-RUN-1"
    telem = [c for c in captured if c[0] == "cost_telemetry"]
    assert len(telem) == 1
    payload = telem[0][1]
    assert payload["sdk_session_id"] == "sdk-RUN-1"
    assert payload["cache_hit_ratio"] == 0.75
    assert payload["input_tokens"] == 200


def test_persist_sdk_result_no_id_does_not_overwrite() -> None:
    """Adversarial: result with no sdk_session_id leaves the stored id alone."""
    store, sid = _new_store_with_session(WorkerState.RUNNING)
    store.set_sdk_session_id(sid, "pre-existing")
    res = _Result(sdk_session_id=None)
    persist_sdk_result(store=store, session_id=sid, result=res, emit=None)
    assert store.get(sid).sdk_session_id == "pre-existing"


def test_persist_sdk_result_emit_none_does_not_crash() -> None:
    """emit=None must be tolerated — legacy callers / unit tests."""
    store, sid = _new_store_with_session(WorkerState.RUNNING)
    res = _Result(sdk_session_id="sdk-X")
    persist_sdk_result(store=store, session_id=sid, result=res, emit=None)
    assert store.get(sid).sdk_session_id == "sdk-X"


def test_resume_kwargs_for_running_session_returns_resume() -> None:
    store, sid = _new_store_with_session(WorkerState.RUNNING)
    store.set_sdk_session_id(sid, "sdk-RUNNING")
    assert resume_kwargs_for(store, sid) == {"resume": "sdk-RUNNING"}


def test_resume_kwargs_for_revising_session_returns_resume() -> None:
    """REVISING is the post-critic resume edge — the key persistent-worker win."""
    store, sid = _new_store_with_session(WorkerState.REVISING)
    store.set_sdk_session_id(sid, "sdk-REVISING")
    assert resume_kwargs_for(store, sid) == {"resume": "sdk-REVISING"}


def test_resume_kwargs_for_dispatched_is_cold_start() -> None:
    """DISPATCHED is by definition a fresh session — no resume."""
    store, sid = _new_store_with_session(WorkerState.DISPATCHED)
    store.set_sdk_session_id(sid, "sdk-stale")
    assert resume_kwargs_for(store, sid) == {}


def test_resume_kwargs_for_awaiting_critic_does_not_resume() -> None:
    """AWAITING_CRITIC means control is with the critic, no worker dispatch."""
    store, sid = _new_store_with_session(WorkerState.AWAITING_CRITIC)
    store.set_sdk_session_id(sid, "sdk-paused")
    assert resume_kwargs_for(store, sid) == {}


def test_resume_kwargs_for_missing_session_returns_empty() -> None:
    store = WorkerSessionStore(":memory:")
    assert resume_kwargs_for(store, "does-not-exist") == {}


def test_resume_kwargs_for_no_sdk_id_yet_returns_empty() -> None:
    """First dispatch: there's no SDK id to resume from yet."""
    store, sid = _new_store_with_session(WorkerState.RUNNING)
    assert resume_kwargs_for(store, sid) == {}
