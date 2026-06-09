"""Worker sandbox capability policy.

This package starts with policy contracts. Backends such as plain worktrees,
Docker, gVisor, Firecracker, or Kata can implement the policy later.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FilesystemScope:
    """Filesystem access granted to a worker."""

    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class NetworkPolicy:
    """Network egress policy for a worker."""

    allow_domains: tuple[str, ...] = ()
    deny_by_default: bool = True


@dataclass(frozen=True)
class McpGrant:
    """One allowed MCP server/tool pattern."""

    server: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityPolicy:
    """Deny-by-default worker capability set."""

    filesystem: FilesystemScope = field(default_factory=FilesystemScope)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    mcp: tuple[McpGrant, ...] = ()
    secret_names: tuple[str, ...] = ()
    preserve_on_failure: bool = False

    def allows_secret(self, name: str) -> bool:
        return name in self.secret_names

    def to_json_obj(self) -> dict[str, Any]:
        return {
            "filesystem": {
                "read_roots": list(self.filesystem.read_roots),
                "write_roots": list(self.filesystem.write_roots),
            },
            "network": {
                "allow_domains": list(self.network.allow_domains),
                "deny_by_default": self.network.deny_by_default,
            },
            "mcp": [{"server": grant.server, "tools": list(grant.tools)} for grant in self.mcp],
            "secret_names": list(self.secret_names),
            "preserve_on_failure": self.preserve_on_failure,
        }

    @classmethod
    def from_json_obj(cls, value: dict[str, Any] | None) -> CapabilityPolicy:
        if value is None:
            return cls()
        filesystem = value.get("filesystem") or {}
        network = value.get("network") or {}
        mcp = value.get("mcp") or ()
        return cls(
            filesystem=FilesystemScope(
                read_roots=tuple(filesystem.get("read_roots") or ()),
                write_roots=tuple(filesystem.get("write_roots") or ()),
            ),
            network=NetworkPolicy(
                allow_domains=tuple(network.get("allow_domains") or ()),
                deny_by_default=bool(network.get("deny_by_default", True)),
            ),
            mcp=tuple(
                McpGrant(
                    server=str(grant.get("server", "")),
                    tools=tuple(grant.get("tools") or ()),
                )
                for grant in mcp
                if isinstance(grant, dict) and grant.get("server")
            ),
            secret_names=tuple(value.get("secret_names") or ()),
            preserve_on_failure=bool(value.get("preserve_on_failure", False)),
        )


def _norm_fs_path(path: str) -> str:
    """Absolute, normalized, trailing-slash-insensitive form of a filesystem path.

    Mirrors :func:`forge_loop.worktree_sweep._norm` (``normpath`` + strip trailing
    separators) but also absolutizes so a relative-vs-absolute pairing for the same
    location compares equal — the normalization the write-root gate (#443) needs.
    """
    return os.path.normpath(os.path.abspath(path.strip())).rstrip(os.sep)


def _path_under_root(path: str, root: str) -> bool:
    """True iff ``path`` is ``root`` itself or nested beneath it (normalized)."""
    p, r = _norm_fs_path(path), _norm_fs_path(root)
    return p == r or p.startswith(r + os.sep)


def write_root_violations(
    changed_paths: Iterable[str],
    write_roots: Iterable[str],
) -> tuple[str, ...]:
    """Changed paths that fall OUTSIDE every leased ``write_roots`` entry (#443).

    Pure helper for the write-root-escape merge gate. Given the paths a worker's
    diff touched and the filesystem write-roots it was leased, return the
    (order-preserving, de-duplicated) tuple of changed paths that escape the
    sandbox. An empty tuple means the diff stayed entirely in-bounds.

    Comparison is normalized (absolute, ``normpath``, trailing-slash insensitive)
    so a path equal-but-for-a-trailing-slash or relative-vs-absolute to a write
    root is NOT a false positive.

    Fail-safe stance — **closed-by-default**: with *no* leased write roots (empty
    ``write_roots``) every changed path is a violation, matching the merge gate's
    "conservative on uncertainty" convention (an un-leased worker that still
    produced a diff is treated as having escaped, not as trusted).
    """
    roots = tuple(r for r in write_roots if r and r.strip())
    seen: set[str] = set()
    violations: list[str] = []
    for raw in changed_paths:
        if not raw or not raw.strip():
            continue
        if any(_path_under_root(raw, root) for root in roots):
            continue
        if raw not in seen:
            seen.add(raw)
            violations.append(raw)
    return tuple(violations)


def mcp_allow_patterns(policy: CapabilityPolicy) -> list[str]:
    """SDK/settings ``allowed_tools`` MCP patterns derived from ``policy.mcp``.

    Single source of truth (#326) for turning a leased
    :class:`CapabilityPolicy` MCP grant into the ``mcp__<server>__*`` /
    ``mcp__<server>__<tool>`` glob patterns the Claude Agent SDK's
    ``allowed_tools`` whitelist (and the worktree ``.claude/settings.json``
    allow-list) understand. Both the SDK enforcement path
    (:func:`forge_loop._worker_sdk.run_sdk_session`) and the worktree
    settings renderer (:mod:`forge_loop.worker_worktree`) call this so the two
    can never disagree on what a grant maps to.

    Deny-by-default: a server NOT named in ``policy.mcp`` produces NO entry,
    never a blanket ``mcp__*``. A grant with no tools (or an explicit ``*``)
    yields ``mcp__<server>__*``; a tool allow-list yields one
    ``mcp__<server>__<tool>`` per tool. Order-preserving, de-duplicated.
    """
    seen: set[str] = set()
    entries: list[str] = []

    def _add(pattern: str) -> None:
        if pattern not in seen:
            seen.add(pattern)
            entries.append(pattern)

    for grant in policy.mcp:
        server = grant.server
        if not server:
            continue
        tools = tuple(tool for tool in grant.tools if tool)
        if not tools or "*" in tools:
            _add(f"mcp__{server}__*")
            continue
        for tool in tools:
            _add(f"mcp__{server}__{tool}")
    return entries


def canonical_policy_json(policy: CapabilityPolicy) -> str:
    """Stable, sorted-keys JSON serialisation of a policy.

    Single source of truth for canonicalisation — both the saga store
    (``tasks/store.py``) and the worker-settings policy-hash reuse this so
    two callers can never disagree on the bytes a policy hashes to.
    """
    return json.dumps(policy.to_json_obj(), sort_keys=True, separators=(",", ":"))


def policy_hash(policy: CapabilityPolicy) -> str:
    """Stable sha256 over the canonical policy JSON.

    Equal policies hash equal; any granted server/path/tool change flips
    the digest. Carried on ``WorkerPolicyEnforcedEvent`` so boot/replay can
    confirm a worker ran within exactly the grant it was leased.
    """
    return hashlib.sha256(canonical_policy_json(policy).encode("utf-8")).hexdigest()


def render_capability_policy(policy: CapabilityPolicy) -> str:
    read_roots = ", ".join(policy.filesystem.read_roots) or "(none)"
    write_roots = ", ".join(policy.filesystem.write_roots) or "(none)"
    network_prefix = "deny-by-default" if policy.network.deny_by_default else "allow-by-default"
    allow_domains = ", ".join(policy.network.allow_domains) or "(none)"
    mcp = (
        ", ".join(f"{grant.server} ({', '.join(grant.tools) or '*'})" for grant in policy.mcp)
        or "(none)"
    )
    secrets = ", ".join(policy.secret_names) or "(none)"
    preserve = "yes" if policy.preserve_on_failure else "no"
    return (
        "CAPABILITY POLICY:\n"
        f"- filesystem read: {read_roots}\n"
        f"- filesystem write: {write_roots}\n"
        f"- network: {network_prefix}; allow {allow_domains}\n"
        f"- mcp: {mcp}\n"
        f"- secrets: {secrets}\n"
        f"- preserve on failure: {preserve}\n"
    )
