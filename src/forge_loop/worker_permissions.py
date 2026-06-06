"""Worker permission profiles → concrete agent-backend options.

A worker runs under one of three permission PROFILES, each mapped onto the
provider's *native* mechanism — Claude Agent SDK options or Codex sandbox
flags. We deliberately reuse what the agent CLIs already enforce rather than
building a bespoke sandbox.

Profiles:

- ``full`` (default) — full host access, no sandbox, no approval prompts. The
  worker runs exactly like the operator's own interactive agent. This is the
  historical behaviour and stays the default until forge-loop targets
  untrusted / distributable execution.
- ``standard`` — the worker works freely inside its worktree but is sandboxed
  from the rest of the host (Claude SDK sandbox / Codex ``workspace-write``).
- ``readonly`` — look but don't touch: planning/triage only, no edits or
  command execution (Claude ``plan`` mode / Codex ``read-only``).

The ``CapabilityPolicy`` (rendered into the brief as advisory text) is
orthogonal: it *tells* the agent the rules; these options are what the runtime
actually enforces.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from forge_loop.sandbox.policy import CapabilityPolicy, NetworkPolicy

FULL = "full"
STANDARD = "standard"
READONLY = "readonly"
PROFILES: tuple[str, ...] = (FULL, STANDARD, READONLY)
DEFAULT_PROFILE = FULL


def normalize_profile(profile: str | None) -> str:
    """Return a known profile, defaulting unknown/empty values to ``full``."""
    p = (profile or DEFAULT_PROFILE).strip().lower()
    return p if p in PROFILES else DEFAULT_PROFILE


def _sandbox_network_config(network: NetworkPolicy) -> dict[str, list[str]]:
    """Map a leased :class:`NetworkPolicy` onto the Claude SDK's native egress
    allow-list (``SandboxNetworkConfig.allowedDomains``).

    Fail-safe closed (issue #282): ``deny_by_default`` with an empty
    ``allow_domains`` renders ``{"allowedDomains": []}`` — egress fully closed,
    NEVER open-by-default or wildcarded. This mirrors the "empty policy → empty
    allow" rule that already governs the filesystem/MCP surface (#200): the
    effective egress surface EQUALS the lease.
    """
    return {"allowedDomains": list(network.allow_domains)}


def claude_permission_options(
    profile: str, policy: CapabilityPolicy | None = None
) -> dict[str, Any]:
    """Claude Agent SDK kwargs for ``profile``.

    Returns ``permission_mode`` always, plus ``sandbox`` (a ``SandboxSettings``
    TypedDict) when the profile asks for host-level confinement. ``full``
    returns no ``sandbox`` key, so the SDK behaviour is byte-identical to the
    historical hardcoded ``permission_mode="bypassPermissions"`` path.

    When a ``policy`` is supplied and the profile is sandboxed (``standard``),
    the lease's :class:`NetworkPolicy` is bound into the sandbox's native
    egress allow-list (``sandbox.network.allowedDomains``) — issue #282. A
    ``None`` policy keeps the historical no-network-key behaviour so non-leased
    callers are unaffected; an empty-domain lease renders a CLOSED egress list
    (fail safe). ``full`` ignores ``policy`` entirely: no sandbox is active, so
    there is nothing to bind, and its output stays byte-identical to today.
    """
    p = normalize_profile(profile)
    if p == STANDARD:
        sandbox: dict[str, Any] = {"enabled": True, "autoAllowBashIfSandboxed": True}
        if policy is not None:
            sandbox["network"] = _sandbox_network_config(policy.network)
        return {"permission_mode": "bypassPermissions", "sandbox": sandbox}
    if p == READONLY:
        return {"permission_mode": "plan"}
    return {"permission_mode": "bypassPermissions"}


def _codex_network_args(network: NetworkPolicy) -> list[str]:
    """Codex sandbox egress flags for a leased :class:`NetworkPolicy`.

    Codex ``workspace-write`` denies network egress by default, so an empty
    ``allow_domains`` lease needs NO extra flags and stays fully closed (fail
    safe, issue #282). When the lease grants domains, egress is enabled and the
    per-domain allow-list is bound via config so a Codex build that honours
    ``allowed_domains`` confines egress to exactly the lease; a build too old to
    read the key degrades to coarse ``network_access`` without crashing the
    worker.
    """
    if not network.allow_domains:
        return []
    domains = json.dumps(list(network.allow_domains))
    return [
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        f"sandbox_workspace_write.allowed_domains={domains}",
    ]


def codex_sandbox_args(profile: str, policy: CapabilityPolicy | None = None) -> list[str]:
    """Codex CLI sandbox argv for ``profile``.

    ``full`` reproduces the historical ``danger-full-access`` +
    ``--dangerously-bypass-approvals-and-sandbox`` flags exactly and ignores
    ``policy`` (no sandbox ⇒ nothing to bind). When ``policy`` is supplied and
    the profile is ``standard``, the lease's network allow-list is bound onto
    the Codex sandbox's egress config (issue #282); a ``None`` policy or an
    empty-domain lease leaves egress at the workspace-write default (closed).
    """
    p = normalize_profile(profile)
    if p == STANDARD:
        args = ["-s", "workspace-write"]
        if policy is not None:
            args += _codex_network_args(policy.network)
        return args
    if p == READONLY:
        return ["-s", "read-only"]
    return ["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"]
