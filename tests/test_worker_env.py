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
from forge_loop.worker_env import build_worker_env, missing_tools

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
