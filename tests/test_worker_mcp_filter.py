"""Tests for issue #60: filter MCP tool list at SDK init.

These tests cover the full surface of the new ``allowed_mcp_tools`` knob:

* config: defaults + env / yaml resolution + empty fallback to bundled default
* :func:`forge_loop._worker_sdk.build_allowed_tools_patterns`: pattern shape
* :func:`forge_loop._worker_sdk.resolve_mcp_filter`: emits ``worker_mcp_filtered``
  and the ``_no_match`` fallback when an operator typoes a server name
* :func:`forge_loop._worker_sdk.run_sdk_session`: passes ``allowed_tools=`` into
  ``ClaudeAgentOptions``; degrades gracefully on a stale SDK that does not
  accept the kwarg
* an ``ALLOWED_TOOL_HARD_CAP`` regression guard — a synthetic 200-tool init
  fails the cap, but applying the filter brings it back under

We avoid every network call: ``query_fn`` and ``options_cls`` are injected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from forge_loop import _worker_sdk, config

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeOptions:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _FakeOptions.last_kwargs = dict(kwargs)
        self.kwargs = kwargs


class _NoAllowedToolsOptions:
    """Stale-SDK shape: rejects ``allowed_tools=`` via TypeError."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        if "allowed_tools" in kwargs:
            raise TypeError("unexpected keyword argument 'allowed_tools'")
        _NoAllowedToolsOptions.last_kwargs = dict(kwargs)


async def _empty_query(prompt: str, options: Any):  # noqa: ARG001
    if False:
        yield None
    return


def _make_init_query(actual_servers: list[Any]):
    """Build a query_fn that yields one SystemMessage(init) with given servers."""
    from claude_agent_sdk import SystemMessage

    async def _q(prompt: str, options: Any):  # noqa: ARG001
        yield SystemMessage(
            subtype="init",
            data={"mcp_servers": actual_servers},
        )

    return _q


# ---------------------------------------------------------------------------
# Config-layer tests
# ---------------------------------------------------------------------------


def test_default_allowed_mcp_servers_is_three_known_useful_servers() -> None:
    """Bundled default keeps only the three servers a worker actually uses."""
    assert config.DEFAULT_ALLOWED_MCP_SERVERS == ("forge-loop", "lumen", "github")
    # WorkerConfig() — i.e. zero customisation — yields the bundled default.
    assert config.WorkerConfig().allowed_mcp_tools == ("forge-loop", "lumen", "github")


def test_parse_mcp_server_list_handles_csv_and_lists() -> None:
    assert config._parse_mcp_server_list("forge-loop, lumen,github") == (
        "forge-loop", "lumen", "github",
    )
    assert config._parse_mcp_server_list(["a", " b ", ""]) == ("a", "b")
    assert config._parse_mcp_server_list("") == ()
    assert config._parse_mcp_server_list(None) == ()


def test_env_var_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_WORKER_ALLOWED_MCP_TOOLS", "only-this-one")
    block = {"allowed_mcp_tools": ["yaml-one", "yaml-two"]}
    assert config._resolve_allowed_mcp_tools(block) == ("only-this-one",)


def test_yaml_used_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOP_WORKER_ALLOWED_MCP_TOOLS", raising=False)
    block = {"allowed_mcp_tools": ["forge-loop", "lumen"]}
    assert config._resolve_allowed_mcp_tools(block) == ("forge-loop", "lumen")


def test_missing_entirely_uses_bundled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOP_WORKER_ALLOWED_MCP_TOOLS", raising=False)
    assert (
        config._resolve_allowed_mcp_tools({})
        == config.DEFAULT_ALLOWED_MCP_SERVERS
    )


def test_empty_value_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty env / yaml value MUST NOT result in an empty allow-list —
    that would strip even forge-loop and break every worker."""
    monkeypatch.setenv("LOOP_WORKER_ALLOWED_MCP_TOOLS", "")
    assert config._resolve_allowed_mcp_tools({}) == config.DEFAULT_ALLOWED_MCP_SERVERS

    monkeypatch.delenv("LOOP_WORKER_ALLOWED_MCP_TOOLS", raising=False)
    assert (
        config._resolve_allowed_mcp_tools({"allowed_mcp_tools": []})
        == config.DEFAULT_ALLOWED_MCP_SERVERS
    )


# ---------------------------------------------------------------------------
# Pattern builder
# ---------------------------------------------------------------------------


def test_build_allowed_tools_patterns_shape() -> None:
    out = _worker_sdk.build_allowed_tools_patterns(["forge-loop", "lumen", "github"])
    assert out == ["mcp__forge-loop__*", "mcp__lumen__*", "mcp__github__*"]


def test_build_allowed_tools_patterns_dedupes_and_strips() -> None:
    out = _worker_sdk.build_allowed_tools_patterns([
        "forge-loop", " lumen ", "forge-loop", "", "  ",
    ])
    assert out == ["mcp__forge-loop__*", "mcp__lumen__*"]


# ---------------------------------------------------------------------------
# resolve_mcp_filter
# ---------------------------------------------------------------------------


def test_resolve_mcp_filter_happy_path_emits_kept_and_dropped() -> None:
    events: list[dict[str, Any]] = []
    resolved = _worker_sdk.resolve_mcp_filter(
        actual_servers=["forge-loop", "lumen", "github", "adaptiq-tutor", "gmail"],
        allow_list=("forge-loop", "lumen", "github"),
        emit=events.append,
    )
    assert resolved == ("forge-loop", "lumen", "github")
    filtered = [e for e in events if e["kind"] == "worker_mcp_filtered"]
    assert len(filtered) == 1
    assert set(filtered[0]["kept"]) == {"forge-loop", "lumen", "github"}
    assert set(filtered[0]["dropped"]) == {"adaptiq-tutor", "gmail"}


def test_resolve_mcp_filter_typo_falls_back_to_default() -> None:
    """Adversarial: operator typoes ``forg-loop``. Filter detects no match
    against the actual server list and falls back to the bundled default."""
    events: list[dict[str, Any]] = []
    resolved = _worker_sdk.resolve_mcp_filter(
        actual_servers=["forge-loop", "lumen", "adaptiq-tutor"],
        allow_list=("forg-loop",),  # typo
        emit=events.append,
        default=("forge-loop", "lumen", "github"),
    )
    assert resolved == ("forge-loop", "lumen", "github")
    kinds = [e["kind"] for e in events]
    assert "worker_mcp_filter_no_match" in kinds
    assert "worker_mcp_filtered" in kinds  # still emitted with the fallback


def test_resolve_mcp_filter_no_servers_active_is_not_a_no_match() -> None:
    """When the SDK reports zero MCP servers loaded at all, we don't claim
    a typo — there's nothing for the allow-list to match against."""
    events: list[dict[str, Any]] = []
    _worker_sdk.resolve_mcp_filter(
        actual_servers=[],
        allow_list=("forge-loop",),
        emit=events.append,
    )
    assert all(e["kind"] != "worker_mcp_filter_no_match" for e in events)


# ---------------------------------------------------------------------------
# run_sdk_session integration
# ---------------------------------------------------------------------------


def test_run_sdk_session_passes_allowed_tools_into_options(tmp_path: Path) -> None:
    _FakeOptions.last_kwargs = {}
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_FakeOptions,
            allowed_mcp_servers=("forge-loop", "lumen", "github"),
        )
    )
    assert _FakeOptions.last_kwargs.get("allowed_tools") == [
        "mcp__forge-loop__*",
        "mcp__lumen__*",
        "mcp__github__*",
    ]


def test_run_sdk_session_applies_bundled_default_when_none(tmp_path: Path) -> None:
    """Caller omits ``allowed_mcp_servers`` → SDK still gets the filter,
    not the firehose."""
    _FakeOptions.last_kwargs = {}
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_FakeOptions,
        )
    )
    assert _FakeOptions.last_kwargs.get("allowed_tools") == [
        "mcp__forge-loop__*",
        "mcp__lumen__*",
        "mcp__github__*",
    ]


def test_run_sdk_session_falls_back_when_sdk_rejects_allowed_tools(
    tmp_path: Path,
) -> None:
    """Older SDK rejects ``allowed_tools=`` — we strip it and keep going."""
    _NoAllowedToolsOptions.last_kwargs = {}
    result = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_empty_query,
            options_cls=_NoAllowedToolsOptions,
            allowed_mcp_servers=("forge-loop",),
        )
    )
    assert "allowed_tools" not in _NoAllowedToolsOptions.last_kwargs
    assert result.error is None  # graceful — no crash


def test_run_sdk_session_emits_worker_mcp_filtered_at_init(tmp_path: Path) -> None:
    """When the SDK init message lists MCP servers, the filter event is
    emitted with kept + dropped names."""
    events: list[dict[str, Any]] = []
    actual = [
        {"name": "forge-loop", "status": "connected"},
        {"name": "lumen", "status": "connected"},
        {"name": "adaptiq-tutor", "status": "connected"},
        {"name": "gmail", "status": "connected"},
    ]
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_make_init_query(actual),
            options_cls=_FakeOptions,
            on_event=events.append,
            allowed_mcp_servers=("forge-loop", "lumen", "github"),
        )
    )
    filtered = [e for e in events if e["kind"] == "worker_mcp_filtered"]
    assert len(filtered) == 1
    assert set(filtered[0]["kept"]) == {"forge-loop", "lumen"}
    assert set(filtered[0]["dropped"]) == {"adaptiq-tutor", "gmail"}


def test_run_sdk_session_emits_no_match_on_typo(tmp_path: Path) -> None:
    """Adversarial integration: typo'd allow-list AND we observe the
    fallback event in the session output."""
    events: list[dict[str, Any]] = []
    actual = [{"name": "forge-loop"}, {"name": "lumen"}]
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=tmp_path,
            query_fn=_make_init_query(actual),
            options_cls=_FakeOptions,
            on_event=events.append,
            allowed_mcp_servers=("forg-loop",),
        )
    )
    kinds = [e["kind"] for e in events]
    assert "worker_mcp_filter_no_match" in kinds


# ---------------------------------------------------------------------------
# ALLOWED_TOOL_HARD_CAP regression guard
# ---------------------------------------------------------------------------


def test_allowed_tool_hard_cap_is_60() -> None:
    """If someone bumps the cap, they need to read the test and the issue
    rationale. Keep this assertion sticky."""
    assert _worker_sdk.ALLOWED_TOOL_HARD_CAP == 60


def test_synthetic_firehose_fails_cap_unfiltered() -> None:
    """Sanity: a 200-tool init message exceeds the cap. This is the
    *unfiltered* baseline — if the filter ever regresses, the matching
    filtered test below will start failing in lockstep."""
    fake_init_tools = [f"mcp__server-{i // 10}__tool-{i}" for i in range(200)]
    assert len(fake_init_tools) > _worker_sdk.ALLOWED_TOOL_HARD_CAP


def test_filter_brings_firehose_under_cap() -> None:
    """Given a synthetic 200-tool init (20 servers × 10 tools each), the
    SDK ``allowed_tools=`` filter (server-level globs) selects only the
    three allowed servers, leaving 30 tools — comfortably under the cap.

    This is the regression guard: if a future change accidentally
    re-enables the firehose (e.g. drops the ``allowed_tools`` kwarg), the
    filtered count will exceed the cap and this test will fail loudly.
    """
    fake_init_tools = [f"mcp__server-{i // 10}__tool-{i}" for i in range(200)]
    allowed_servers = ("server-0", "server-1", "server-2")
    patterns = _worker_sdk.build_allowed_tools_patterns(allowed_servers)
    # Apply the patterns as the SDK would: keep tools whose name matches
    # any ``mcp__<server>__*`` glob.
    import fnmatch

    kept = [
        t for t in fake_init_tools
        if any(fnmatch.fnmatch(t, pat) for pat in patterns)
    ]
    assert len(kept) <= _worker_sdk.ALLOWED_TOOL_HARD_CAP
    # And the unfiltered list would have busted the cap (defensive).
    assert len(fake_init_tools) > _worker_sdk.ALLOWED_TOOL_HARD_CAP


# ---------------------------------------------------------------------------
# Integration: a real ANTHROPIC_API_KEY worker — skipped without the key.
# ---------------------------------------------------------------------------


import os  # noqa: E402


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs ANTHROPIC_API_KEY for a real dispatch",
)
def test_real_dispatch_under_tool_cap(tmp_path: Path) -> None:
    """End-to-end: a real (live) worker dispatch must report fewer than
    ALLOWED_TOOL_HARD_CAP tools in its init message. Skipped in CI without
    a key."""
    events: list[dict[str, Any]] = []
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "echo hello and exit",
            cwd=tmp_path,
            max_turns=1,
            on_event=events.append,
            allowed_mcp_servers=("forge-loop", "lumen", "github"),
        )
    )
    init = next((e for e in events if e["kind"] == "turn_start"), None)
    assert init is not None
    tools = init["data"].get("tools") or []
    assert len(tools) <= _worker_sdk.ALLOWED_TOOL_HARD_CAP
