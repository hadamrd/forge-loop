"""Issue #326: the SDK ``allowed_tools`` MCP allow-list is derived from the
leased :class:`CapabilityPolicy.mcp` grant — deny-by-default — NOT the
operator-global config.

Before this change a worker's MCP grant was *advisory*: it was printed in the
brief and enforced at the worktree ``settings.json`` layer, but the SDK
``allowed_tools`` whitelist was fed from a SEPARATE config-sourced
``allowed_mcp_servers``. So a worker leased without a grant for server X could
still have X's tools in its SDK allow-list. These tests pin the leased policy
as the single source of truth.

The headline falsifiable test (:func:`test_lease_without_grant_excludes_server`)
compares two leases that differ ONLY in their ``mcp`` grant and asserts the
ungranted server's tools are absent from ``allowed_tools``.

No network: ``query_fn`` / ``options_cls`` are injected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from forge_loop import _worker_sdk
from forge_loop.sandbox import CapabilityPolicy, McpGrant, mcp_allow_patterns

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeOptions:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _FakeOptions.last_kwargs = dict(kwargs)
        self.kwargs = kwargs


async def _empty_query(prompt: str, options: Any):  # noqa: ARG001
    if False:
        yield None
    return


def _make_init_query(actual_servers: list[Any]):
    from claude_agent_sdk import SystemMessage

    async def _q(prompt: str, options: Any):  # noqa: ARG001
        yield SystemMessage(subtype="init", data={"mcp_servers": actual_servers})

    return _q


def _allowed_tools_for(policy: CapabilityPolicy | None) -> list[str]:
    """Run a session with ``policy`` leased and return the SDK allow-list."""
    _FakeOptions.last_kwargs = {}
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=Path("/tmp"),
            query_fn=_empty_query,
            options_cls=_FakeOptions,
            capability_policy=policy,
        )
    )
    tools = _FakeOptions.last_kwargs.get("allowed_tools")
    assert isinstance(tools, list)
    return tools


# ---------------------------------------------------------------------------
# mcp_allow_patterns — the single source of truth
# ---------------------------------------------------------------------------


def test_mcp_allow_patterns_server_only_grant_is_wildcard() -> None:
    policy = CapabilityPolicy(mcp=(McpGrant(server="forge-loop"),))
    assert mcp_allow_patterns(policy) == ["mcp__forge-loop__*"]


def test_mcp_allow_patterns_tool_scoped_grant_lists_each_tool() -> None:
    policy = CapabilityPolicy(mcp=(McpGrant(server="github", tools=("create_issue", "get_pr")),))
    assert mcp_allow_patterns(policy) == [
        "mcp__github__create_issue",
        "mcp__github__get_pr",
    ]


def test_mcp_allow_patterns_explicit_star_tool_is_wildcard() -> None:
    policy = CapabilityPolicy(mcp=(McpGrant(server="lumen", tools=("*",)),))
    assert mcp_allow_patterns(policy) == ["mcp__lumen__*"]


def test_mcp_allow_patterns_dedupes_and_skips_blank_server() -> None:
    policy = CapabilityPolicy(
        mcp=(
            McpGrant(server="forge-loop"),
            McpGrant(server="forge-loop"),  # dup
            McpGrant(server=""),  # blank — skipped
        )
    )
    assert mcp_allow_patterns(policy) == ["mcp__forge-loop__*"]


def test_mcp_allow_patterns_empty_policy_is_deny_all() -> None:
    """Adversarial: no MCP grant ⇒ EMPTY allow-list, never a blanket mcp__*."""
    assert mcp_allow_patterns(CapabilityPolicy()) == []


# ---------------------------------------------------------------------------
# run_sdk_session derives allowed_tools from the lease
# ---------------------------------------------------------------------------


def test_leased_policy_drives_allowed_tools() -> None:
    policy = CapabilityPolicy(mcp=(McpGrant(server="forge-loop"), McpGrant(server="lumen")))
    assert _allowed_tools_for(policy) == ["mcp__forge-loop__*", "mcp__lumen__*"]


def test_lease_without_grant_excludes_server() -> None:
    """Falsifiable acceptance (#326): two leases that differ ONLY in their
    ``mcp`` grant. The lease WITHOUT a grant for ``github`` must have
    ``github``'s tools excluded from the SDK ``allowed_tools`` patterns; the
    lease WITH the grant must include them. Proves enforcement reads the
    leased policy, not the operator config."""
    with_github = CapabilityPolicy(mcp=(McpGrant(server="forge-loop"), McpGrant(server="github")))
    without_github = CapabilityPolicy(mcp=(McpGrant(server="forge-loop"),))

    allowed_with = _allowed_tools_for(with_github)
    allowed_without = _allowed_tools_for(without_github)

    assert "mcp__github__*" in allowed_with
    assert "mcp__github__*" not in allowed_without
    # The only difference between the two leases is the github grant.
    assert allowed_with == [*allowed_without, "mcp__github__*"]


def test_empty_lease_yields_empty_allowed_tools() -> None:
    """A worker leased with NO MCP grant gets an explicit empty allow-list —
    deny-by-default, not the bundled firehose fallback."""
    assert _allowed_tools_for(CapabilityPolicy()) == []


def test_leased_policy_overrides_operator_config() -> None:
    """When BOTH a lease and an operator ``allowed_mcp_servers`` are present,
    the lease wins — the leased policy is the single source of truth."""
    _FakeOptions.last_kwargs = {}
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=Path("/tmp"),
            query_fn=_empty_query,
            options_cls=_FakeOptions,
            # Operator config would allow github + gmail...
            allowed_mcp_servers=("github", "gmail"),
            # ...but the lease only grants forge-loop.
            capability_policy=CapabilityPolicy(mcp=(McpGrant(server="forge-loop"),)),
        )
    )
    assert _FakeOptions.last_kwargs.get("allowed_tools") == ["mcp__forge-loop__*"]


# ---------------------------------------------------------------------------
# resolve_mcp_filter respects deny-by-default under a lease
# ---------------------------------------------------------------------------


def test_leased_policy_no_match_does_not_fall_back_to_bundled() -> None:
    """Adversarial: a lease grants ``forge-loop`` but the SDK init reports a
    DIFFERENT set of loaded servers (none of them granted). Under a lease the
    no-match fallback must NOT re-add the bundled default servers — that would
    silently widen the grant. ``kept`` is empty; everything is dropped."""
    events: list[dict[str, Any]] = []
    actual = [{"name": "gmail"}, {"name": "drive"}]
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=Path("/tmp"),
            query_fn=_make_init_query(actual),
            options_cls=_FakeOptions,
            on_event=events.append,
            capability_policy=CapabilityPolicy(mcp=(McpGrant(server="forge-loop"),)),
        )
    )
    no_match = [e for e in events if e["kind"] == "worker_mcp_filter_no_match"]
    assert len(no_match) == 1
    # Deny-by-default: the bundled servers are NOT offered as a fallback.
    assert no_match[0]["fallback"] == []
    filtered = [e for e in events if e["kind"] == "worker_mcp_filtered"]
    assert filtered[0]["kept"] == []
    assert set(filtered[0]["dropped"]) == {"gmail", "drive"}


def test_no_lease_preserves_operator_config_path() -> None:
    """Regression: when NO policy is leased, the legacy operator-config path is
    untouched — the bundled default still applies so critic/brainstormer/legacy
    callers keep working."""
    _FakeOptions.last_kwargs = {}
    anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "prompt",
            cwd=Path("/tmp"),
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
