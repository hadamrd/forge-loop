"""Critic write path closes its durable findings store (#242 review, sev2/perf).

`apply_critic_report` is fed a `findings_store` opened by the critic review
paths (`boot.py` `_critic_fn`, `dispatch.py` `_run_critic_for_outcomes`). Both
sites used to open a WAL SQLite connection inline and never close it, leaking a
connection (plus -wal/-shm handles) per critic review — the exact leak the PR
guards against on the repairs hot path. These tests pin the context-manager fix:
the store opened for a critic tick is closed once the work drains.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

import forge_loop.critic_findings as critic_findings_mod
from forge_loop.runner import dispatch as dispatch_mod

# Reuse the Config scaffolding from the dispatch FSM tests.
from tests.test_persistent_dispatch import _make_cfg


def _spy_open_store(monkeypatch: Any) -> list[Any]:
    """Capture every store handed out by ``open_critic_findings_store``."""
    opened: list[Any] = []
    real = critic_findings_mod.open_critic_findings_store

    def _capturing(repo_path: Any) -> Any:
        store = real(repo_path)
        opened.append(store)
        return store

    monkeypatch.setattr(
        critic_findings_mod, "open_critic_findings_store", _capturing
    )
    return opened


def test_run_critic_for_outcomes_closes_its_store(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The critic tick opens one findings store and closes it on the way out.

    With no reviewable outcomes the loop body never runs, but the ``with``
    block still opens the store — so a leak would leave the connection usable.
    After the call it must be closed: operating on it raises
    ``sqlite3.ProgrammingError``.
    """
    cfg = _make_cfg(tmp_path)
    opened = _spy_open_store(monkeypatch)

    # No outcomes -> no GitHub / critic-review side effects, just open+close.
    dispatch_mod._run_critic_for_outcomes(cfg, [], bus_emit=lambda *a, **k: None)

    assert len(opened) == 1, "exactly one store opened per critic tick"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0]._connection.execute("SELECT 1")
