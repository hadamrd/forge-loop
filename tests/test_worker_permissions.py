"""Worker permission-profile mapping + back-compat guarantees."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.agent_backend import build_codex_exec_argv
from forge_loop.worker_permissions import (
    DEFAULT_PROFILE,
    PROFILES,
    claude_permission_options,
    codex_sandbox_args,
    normalize_profile,
)


def test_default_profile_is_full() -> None:
    assert DEFAULT_PROFILE == "full"
    assert set(PROFILES) == {"full", "standard", "readonly"}


@pytest.mark.parametrize(
    "given,expected",
    [
        ("full", "full"),
        ("standard", "standard"),
        ("readonly", "readonly"),
        ("FULL", "full"),
        ("  Standard  ", "standard"),
        ("", "full"),
        (None, "full"),
        ("nonsense", "full"),
        ("bypassPermissions", "full"),  # not a profile name → safe default
    ],
)
def test_normalize_profile(given: str | None, expected: str) -> None:
    assert normalize_profile(given) == expected


def test_full_claude_options_have_no_sandbox() -> None:
    # 'full' must be byte-identical to the historical hardcoded path:
    # permission_mode=bypassPermissions and NO sandbox key.
    opts = claude_permission_options("full")
    assert opts == {"permission_mode": "bypassPermissions"}
    assert "sandbox" not in opts


def test_standard_claude_options_enable_sandbox() -> None:
    opts = claude_permission_options("standard")
    assert opts["permission_mode"] == "bypassPermissions"
    assert opts["sandbox"]["enabled"] is True
    # Bash must auto-allow inside the sandbox or a sandboxed worker can't work.
    assert opts["sandbox"]["autoAllowBashIfSandboxed"] is True


def test_readonly_claude_options_use_plan_mode_and_no_sandbox() -> None:
    opts = claude_permission_options("readonly")
    assert opts == {"permission_mode": "plan"}


def test_unknown_profile_falls_back_to_full_options() -> None:
    assert claude_permission_options("banana") == claude_permission_options("full")
    assert codex_sandbox_args("banana") == codex_sandbox_args("full")


def test_codex_sandbox_args_per_profile() -> None:
    assert codex_sandbox_args("full") == [
        "-s",
        "danger-full-access",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    assert codex_sandbox_args("standard") == ["-s", "workspace-write"]
    assert codex_sandbox_args("readonly") == ["-s", "read-only"]


def test_codex_argv_default_is_unchanged_when_no_sandbox_args(tmp_path: Path) -> None:
    """Regression: omitting sandbox_args reproduces the historical full-access argv."""
    argv = build_codex_exec_argv(cwd=tmp_path, last_message_path=tmp_path / "last.txt")
    assert "danger-full-access" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    # full profile's args are exactly what the default produces.
    full = codex_sandbox_args("full")
    joined = " ".join(argv)
    assert " ".join(full) in joined


def test_codex_argv_threads_profile_sandbox_args(tmp_path: Path) -> None:
    argv = build_codex_exec_argv(
        cwd=tmp_path,
        last_message_path=tmp_path / "last.txt",
        sandbox_args=codex_sandbox_args("standard"),
    )
    assert "workspace-write" in argv
    # The dangerous full-access flag must NOT leak into a sandboxed worker.
    assert "danger-full-access" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
