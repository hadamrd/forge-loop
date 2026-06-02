"""Worker sandbox capability policy.

This package starts with policy contracts. Backends such as plain worktrees,
Docker, gVisor, Firecracker, or Kata can implement the policy later.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    def allows_secret(self, name: str) -> bool:
        return name in self.secret_names
