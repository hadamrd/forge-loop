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

from typing import Any

FULL = "full"
STANDARD = "standard"
READONLY = "readonly"
PROFILES: tuple[str, ...] = (FULL, STANDARD, READONLY)
DEFAULT_PROFILE = FULL


def normalize_profile(profile: str | None) -> str:
    """Return a known profile, defaulting unknown/empty values to ``full``."""
    p = (profile or DEFAULT_PROFILE).strip().lower()
    return p if p in PROFILES else DEFAULT_PROFILE


def claude_permission_options(profile: str) -> dict[str, Any]:
    """Claude Agent SDK kwargs for ``profile``.

    Returns ``permission_mode`` always, plus ``sandbox`` (a ``SandboxSettings``
    TypedDict) when the profile asks for host-level confinement. ``full``
    returns no ``sandbox`` key, so the SDK behaviour is byte-identical to the
    historical hardcoded ``permission_mode="bypassPermissions"`` path.
    """
    p = normalize_profile(profile)
    if p == STANDARD:
        return {
            "permission_mode": "bypassPermissions",
            "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True},
        }
    if p == READONLY:
        return {"permission_mode": "plan"}
    return {"permission_mode": "bypassPermissions"}


def codex_sandbox_args(profile: str) -> list[str]:
    """Codex CLI sandbox argv for ``profile``.

    ``full`` reproduces the historical ``danger-full-access`` +
    ``--dangerously-bypass-approvals-and-sandbox`` flags exactly.
    """
    p = normalize_profile(profile)
    if p == STANDARD:
        return ["-s", "workspace-write"]
    if p == READONLY:
        return ["-s", "read-only"]
    return ["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"]
