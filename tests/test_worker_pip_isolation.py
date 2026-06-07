"""Isolation tests (#315 AC #5): a worker cannot pip-install into the operator
site.

Root cause of the #144 poison: workers inherit the orchestrator's ambient env
(``_clean_sdk_env`` == ``dict(os.environ)``), so a stray ``pip install -e .``
from the /tmp worktree writes an editable into ``~/.local/.../site-packages``.
The fix makes that op INERT by exporting ``PIP_REQUIRE_VIRTUALENV=1`` (plain pip
refuses outside a venv) and ``PYTHONNOUSERSITE=1`` into the worker env.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop._worker_sdk import _clean_sdk_env
from forge_loop.worker_env import build_worker_env


def test_clean_sdk_env_blocks_pip_into_user_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIP_REQUIRE_VIRTUALENV", raising=False)
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    env = _clean_sdk_env()
    assert env["PIP_REQUIRE_VIRTUALENV"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"


def test_clean_sdk_env_does_not_override_operator_provided_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``setdefault`` semantics: an operator that already exports a value (e.g.
    # pointing pip at a worktree-local venv) is respected, not clobbered.
    monkeypatch.setenv("PIP_REQUIRE_VIRTUALENV", "0")
    env = _clean_sdk_env()
    assert env["PIP_REQUIRE_VIRTUALENV"] == "0"


def test_isolation_vars_survive_declared_env_contract(tmp_path: Path) -> None:
    # build_worker_env starts from a copy of the cleaned base, so the isolation
    # vars are still present after the declared PATH/venv contract is applied.
    base = _clean_sdk_env()
    env = build_worker_env(
        base, repo=tmp_path, path_prepend=[".venv/bin"], vars={"VIRTUAL_ENV": ".venv"}
    )
    assert env["PIP_REQUIRE_VIRTUALENV"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["VIRTUAL_ENV"].endswith(".venv")
