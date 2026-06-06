"""Tests for the worker environment contract (the 2026-06-05 silent-toolchain
incident).

Three layers under test:

1. :mod:`forge_loop.worker_env` — pure provisioning + preflight.
2. The dispatch-path preflight in :func:`forge_loop.worker.run_worker` —
   abort-loud-on-missing vs proceed-on-satisfied vs warn-on-undeclared.
3. Wiring proof: the provisioned env actually reaches ``run_sdk_session``'s
   ``base_kwargs["env"]``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from forge_loop import worker as worker_mod
from forge_loop.sandbox.policy import CapabilityPolicy
from forge_loop.worker_env import build_worker_env, missing_tools, scope_secrets

# ---------------------------------------------------------------------------
# build_worker_env — provisioning
# ---------------------------------------------------------------------------


def test_build_worker_env_prepends_venv_bin_to_path(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    base = {"PATH": "/usr/bin:/bin"}
    env = build_worker_env(base, repo=tmp_path, path_prepend=[".venv/bin"], vars={})

    expected = str((tmp_path / ".venv" / "bin").resolve())
    assert env["PATH"].split(os.pathsep)[0] == expected
    # Existing entries are preserved after the prepended dir.
    assert "/usr/bin" in env["PATH"].split(os.pathsep)
    # The input mapping is never mutated.
    assert base["PATH"] == "/usr/bin:/bin"


def test_build_worker_env_sets_virtual_env_absolute(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    env = build_worker_env({}, repo=tmp_path, vars={"VIRTUAL_ENV": ".venv"})
    # The repo-relative value is rebased to an ABSOLUTE path against repo root.
    assert env["VIRTUAL_ENV"] == str((tmp_path / ".venv").resolve())
    assert os.path.isabs(env["VIRTUAL_ENV"])


def test_build_worker_env_non_path_var_left_verbatim(tmp_path: Path) -> None:
    env = build_worker_env({}, repo=tmp_path, vars={"PYTHONUNBUFFERED": "1"})
    # A bare token (no separator) is NOT treated as a path.
    assert env["PYTHONUNBUFFERED"] == "1"


def test_build_worker_env_absolute_var_not_rebased(tmp_path: Path) -> None:
    env = build_worker_env({}, repo=tmp_path, vars={"VIRTUAL_ENV": "/opt/venv"})
    assert env["VIRTUAL_ENV"] == "/opt/venv"


def test_build_worker_env_path_dedup_and_precedence(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    existing = str((tmp_path / "bin").resolve())
    base = {"PATH": os.pathsep.join([existing, "/usr/bin"])}
    env = build_worker_env(base, repo=tmp_path, path_prepend=["bin"])
    parts = env["PATH"].split(os.pathsep)
    # The declared dir wins precedence and appears exactly once.
    assert parts[0] == existing
    assert parts.count(existing) == 1
    assert "/usr/bin" in parts


def test_build_worker_env_accepts_vars_as_pairs(tmp_path: Path) -> None:
    env = build_worker_env({}, repo=tmp_path, vars=[("FOO", "bar")])
    assert env["FOO"] == "bar"


# ---------------------------------------------------------------------------
# scope_secrets — secret-lease enforcement at spawn (issue #283)
# ---------------------------------------------------------------------------


def test_scope_secrets_keeps_leased_drops_unleased_passes_plain() -> None:
    base = {
        "GITHUB_TOKEN": "gh-secret",
        "ANTHROPIC_API_KEY": "sk-secret",
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "VIRTUAL_ENV": "/opt/venv",
    }
    policy = CapabilityPolicy(secret_names=("GITHUB_TOKEN",))
    env, withheld = scope_secrets(base, policy)

    # Leased secret survives.
    assert env["GITHUB_TOKEN"] == "gh-secret"
    # Unleased secret-shaped key is removed (and reported by name).
    assert "ANTHROPIC_API_KEY" not in env
    assert withheld == ["ANTHROPIC_API_KEY"]
    # Non-secret keys pass through untouched.
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/x"
    assert env["VIRTUAL_ENV"] == "/opt/venv"


def test_scope_secrets_none_policy_withholds_all() -> None:
    base = {
        "GITHUB_TOKEN": "a",
        "ANTHROPIC_API_KEY": "b",
        "DB_PASSWORD": "c",
        "PATH": "/usr/bin",
    }
    env, withheld = scope_secrets(base, None)

    # Fail safe: every secret-shaped key withheld when there is no lease.
    assert "GITHUB_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "DB_PASSWORD" not in env
    assert env["PATH"] == "/usr/bin"
    # Withheld names are sorted/stable and exactly the secret-shaped keys.
    assert withheld == ["ANTHROPIC_API_KEY", "DB_PASSWORD", "GITHUB_TOKEN"]


def test_scope_secrets_empty_policy_withholds_all() -> None:
    # An empty CapabilityPolicy() behaves identically to None — closed default.
    base = {"GITHUB_TOKEN": "a", "PATH": "/usr/bin"}
    env, withheld = scope_secrets(base, CapabilityPolicy())
    assert "GITHUB_TOKEN" not in env
    assert withheld == ["GITHUB_TOKEN"]


def test_scope_secrets_does_not_mutate_input() -> None:
    base = {"GITHUB_TOKEN": "a", "PATH": "/usr/bin"}
    snapshot = dict(base)
    env, _ = scope_secrets(base, CapabilityPolicy(secret_names=("GITHUB_TOKEN",)))
    # The input mapping is untouched; a brand-new dict is returned.
    assert base == snapshot
    assert env is not base


def test_scope_secrets_leased_name_absent_from_base_is_noop() -> None:
    # Leasing a name that isn't in base must not error or invent a key.
    base = {"PATH": "/usr/bin"}
    env, withheld = scope_secrets(base, CapabilityPolicy(secret_names=("GITHUB_TOKEN",)))
    assert "GITHUB_TOKEN" not in env
    assert withheld == []
    assert env == {"PATH": "/usr/bin"}


def test_scope_secrets_pattern_coverage() -> None:
    base = {
        "ANTHROPIC_API_KEY": "1",
        "GITHUB_TOKEN": "2",
        "AWS_SECRET_ACCESS_KEY": "3",
        "DB_PASSWORD": "4",
        "MY_PASSWD": "5",
        "SOME_CREDENTIAL": "6",
        # Non-secret-shaped — must NOT be flagged.
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
        "EDITOR": "vim",
    }
    _, withheld = scope_secrets(base, None)
    assert set(withheld) == {
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "DB_PASSWORD",
        "MY_PASSWD",
        "SOME_CREDENTIAL",
    }
    # PATH/LANG/EDITOR explicitly not flagged.
    for plain in ("PATH", "LANG", "EDITOR"):
        assert plain not in withheld


# ---------------------------------------------------------------------------
# missing_tools — preflight
# ---------------------------------------------------------------------------


def test_missing_tools_detects_absent_tool(tmp_path: Path) -> None:
    # An empty PATH resolves nothing.
    env = {"PATH": str(tmp_path)}
    assert missing_tools(env, ["definitely-not-a-real-tool-xyz"]) == [
        "definitely-not-a-real-tool-xyz"
    ]


def test_missing_tools_passes_when_present(tmp_path: Path) -> None:
    # Plant a fake executable on a tmp dir and point PATH at it.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "faketool"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    env = {"PATH": str(bindir)}
    assert missing_tools(env, ["faketool"]) == []
    # Sanity: it is NOT found on an empty PATH (proves PATH-scoping, not
    # ambient resolution).
    assert missing_tools({"PATH": ""}, ["faketool"]) == ["faketool"]


def test_missing_tools_uses_env_path_not_process_path(tmp_path: Path) -> None:
    # python3 exists on the process PATH; an env with an empty PATH must NOT
    # resolve it (the check is scoped to env["PATH"], not os.environ).
    assert shutil.which("python3") is not None  # precondition
    assert missing_tools({"PATH": ""}, ["python3"]) == ["python3"]


def test_missing_tools_partial(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "present"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    env = {"PATH": str(bindir)}
    assert missing_tools(env, ["present", "absent"]) == ["absent"]


# ---------------------------------------------------------------------------
# run_worker preflight integration
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_worktree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_prep(_repo: Path, _n: int, _branch: str, **_kw: Any) -> tuple[Path, None]:
        return worktree, None

    monkeypatch.setattr(worker_mod, "_prep_worktree", fake_prep)
    return worktree


def _issue() -> dict[str, Any]:
    return {"number": 7, "title": "do thing", "body": "body"}


def test_run_worker_aborts_when_required_tool_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _patch_worktree: Path
) -> None:
    # _run_worker_sdk MUST NOT be reached — a missing required tool aborts
    # before the doomed SDK session starts.
    called = {"sdk": False}

    def boom_sdk(**_kw: Any) -> Any:
        called["sdk"] = True
        raise AssertionError("SDK session must not start on a missing toolchain")

    monkeypatch.setattr(worker_mod, "_run_worker_sdk", boom_sdk)

    events: list[tuple[str, dict[str, Any]]] = []
    out = worker_mod.run_worker(
        _issue(),
        tmp_path,
        tmp_path / "logs",
        30,
        emit=lambda kind, payload: events.append((kind, payload)),
        env_require=("definitely-not-a-real-tool-xyz",),
        env_path_prepend=(".venv/bin",),
    )

    assert called["sdk"] is False
    assert out.status == "failed"
    assert out.error is not None
    assert out.error.startswith("worker_toolchain_unavailable")
    assert "definitely-not-a-real-tool-xyz" in out.error
    kinds = [k for k, _ in events]
    assert "worker_toolchain_unavailable" in kinds
    # The typed event carries the diagnostic triple.
    payload = next(p for k, p in events if k == "worker_toolchain_unavailable")
    assert payload["missing"] == ["definitely-not-a-real-tool-xyz"]
    assert "path" in payload and "venv" in payload


def test_run_worker_proceeds_when_required_tool_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _patch_worktree: Path
) -> None:
    # Plant a fake tool on a dir and declare it via path_prepend + require.
    bindir = tmp_path / "tools"
    bindir.mkdir()
    fake = bindir / "mytool"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)

    def ok_sdk(**_kw: Any) -> worker_mod.WorkerOutcome:
        return worker_mod.WorkerOutcome(
            issue=7,
            title="do thing",
            pr_url="https://x/pull/7",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(worker_mod, "_run_worker_sdk", ok_sdk)

    events: list[tuple[str, dict[str, Any]]] = []
    out = worker_mod.run_worker(
        _issue(),
        tmp_path,
        tmp_path / "logs",
        30,
        emit=lambda kind, payload: events.append((kind, payload)),
        env_path_prepend=("tools",),
        env_require=("mytool",),
    )

    assert out.status == "open"
    kinds = [k for k, _ in events]
    assert "worker_toolchain_unavailable" not in kinds
    # A declared (and satisfied) contract does NOT emit the undeclared warning.
    assert "worker_env_undeclared" not in kinds


def test_run_worker_warns_when_no_env_block_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _patch_worktree: Path
) -> None:
    def ok_sdk(**_kw: Any) -> worker_mod.WorkerOutcome:
        return worker_mod.WorkerOutcome(
            issue=7,
            title="do thing",
            pr_url=None,
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(worker_mod, "_run_worker_sdk", ok_sdk)

    events: list[tuple[str, dict[str, Any]]] = []
    out = worker_mod.run_worker(
        _issue(),
        tmp_path,
        tmp_path / "logs",
        30,
        emit=lambda kind, payload: events.append((kind, payload)),
        # No env_path_prepend / env_vars / env_require → undeclared.
    )

    assert out.status == "open"  # warning, not abort
    kinds = [k for k, _ in events]
    assert "worker_env_undeclared" in kinds
    assert "worker_toolchain_unavailable" not in kinds


def test_run_sdk_session_provisions_env_into_base_kwargs(tmp_path: Path) -> None:
    """Wiring proof: the provisioned env lands in ClaudeAgentOptions(env=...)."""
    import anyio

    from forge_loop import _worker_sdk

    (tmp_path / ".venv" / "bin").mkdir(parents=True)

    captured: dict[str, Any] = {}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    async def fake_query(**_kw: Any) -> Any:
        # Yield nothing — we only care that options were constructed.
        if False:
            yield None
        return

    async def _run() -> Any:
        return await _worker_sdk.run_sdk_session(
            "brief",
            cwd=tmp_path,
            repo=tmp_path,
            env_path_prepend=[".venv/bin"],
            env_vars={"VIRTUAL_ENV": ".venv"},
            query_fn=fake_query,
            options_cls=FakeOptions,
        )

    anyio.run(_run)

    env = captured["env"]
    venv_bin = str((tmp_path / ".venv" / "bin").resolve())
    assert env["PATH"].split(os.pathsep)[0] == venv_bin
    assert env["VIRTUAL_ENV"] == str((tmp_path / ".venv").resolve())


def _run_session_capture_env(
    tmp_path: Path, *, base_env: dict[str, str], **session_kw: Any
) -> dict[str, str]:
    """Drive run_sdk_session with a fake SDK and return the env it built."""
    import anyio

    from forge_loop import _worker_sdk

    captured: dict[str, Any] = {}

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    async def fake_query(**_kw: Any) -> Any:
        if False:
            yield None
        return

    async def _run() -> Any:
        return await _worker_sdk.run_sdk_session(
            "brief",
            cwd=tmp_path,
            env=dict(base_env),
            query_fn=fake_query,
            options_cls=FakeOptions,
            **session_kw,
        )

    anyio.run(_run)
    return captured["env"]


def test_run_sdk_session_scopes_secrets_to_lease(tmp_path: Path) -> None:
    """Integration: only the leased secret survives into ClaudeAgentOptions(env).

    The acceptance customer story — a lease naming only GITHUB_TOKEN keeps it,
    drops ANTHROPIC_API_KEY, and leaves PATH/VIRTUAL_ENV (toolchain) intact.
    """
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    base_env = {
        "GITHUB_TOKEN": "gh",
        "ANTHROPIC_API_KEY": "sk",
        "PATH": "/usr/bin",
        "VIRTUAL_ENV": "/opt/venv",
    }
    env = _run_session_capture_env(
        tmp_path,
        base_env=base_env,
        repo=tmp_path,
        env_path_prepend=[".venv/bin"],
        secret_names=("GITHUB_TOKEN",),
    )

    assert env["GITHUB_TOKEN"] == "gh"
    assert "ANTHROPIC_API_KEY" not in env
    # Toolchain provisioning survives secret scoping.
    assert env["VIRTUAL_ENV"] == "/opt/venv"
    venv_bin = str((tmp_path / ".venv" / "bin").resolve())
    assert env["PATH"].split(os.pathsep)[0] == venv_bin


def test_run_sdk_session_secret_acceptance_gate(tmp_path: Path) -> None:
    """Adversarial acceptance gate from the customer story (issue #283).

    A worker whose lease omits both tokens CANNOT read either from its env;
    a worker whose lease names them CAN.
    """
    base_env = {"ANTHROPIC_API_KEY": "sk", "GITHUB_TOKEN": "gh", "PATH": "/usr/bin"}

    # Lease omits both → both withheld.
    denied = _run_session_capture_env(tmp_path, base_env=base_env, secret_names=())
    assert "ANTHROPIC_API_KEY" not in denied
    assert "GITHUB_TOKEN" not in denied
    assert denied["PATH"] == "/usr/bin"

    # Lease names both → both present.
    granted = _run_session_capture_env(
        tmp_path, base_env=base_env, secret_names=("ANTHROPIC_API_KEY", "GITHUB_TOKEN")
    )
    assert granted["ANTHROPIC_API_KEY"] == "sk"
    assert granted["GITHUB_TOKEN"] == "gh"


def test_run_sdk_session_default_closes_secrets(tmp_path: Path) -> None:
    """No secret_names kwarg → closed default: every secret-shaped key withheld."""
    base_env = {"GITHUB_TOKEN": "gh", "PATH": "/usr/bin"}
    env = _run_session_capture_env(tmp_path, base_env=base_env)
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
