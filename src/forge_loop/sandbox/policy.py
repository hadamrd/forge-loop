"""Worker sandbox capability policy.

This package starts with policy contracts. Backends such as plain worktrees,
Docker, gVisor, Firecracker, or Kata can implement the policy later.
"""

from __future__ import annotations

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
        )


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
    return (
        "CAPABILITY POLICY:\n"
        f"- filesystem read: {read_roots}\n"
        f"- filesystem write: {write_roots}\n"
        f"- network: {network_prefix}; allow {allow_domains}\n"
        f"- mcp: {mcp}\n"
        f"- secrets: {secrets}\n"
    )
