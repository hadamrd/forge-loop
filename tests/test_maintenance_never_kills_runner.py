"""Maintenance is a periodic nicety. It must NEVER end the runner.

☠ run_maintenance shelled out to a bare "claude". The agent SDK ships its OWN claude binary and the
workers use that, so a machine can run workers perfectly while having no `claude` on PATH — exactly
this machine. FileNotFoundError escaped run_maintenance and killed the whole process every
`maintenance_every_n_ticks` ticks. Same class as the POSIX-only SIGUSR1 handler: an optional feature
ending the service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import forge_loop.maintenance as m


def test_missing_claude_returns_an_outcome_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(m, "_claude_executable", lambda: None)
    monkeypatch.setattr(m, "ensure_subagent_trusted", lambda *_a, **_k: None)

    out = m.run_maintenance(tmp_path, tmp_path / "logs")

    assert out.acted_on == 0
    assert "claude" in str(out.raw.get("error", "")).lower()


def test_spawn_failure_is_also_degraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolver can succeed and the spawn still fail — deleted, not executable, bad perms."""
    monkeypatch.setattr(m, "_claude_executable", lambda: "/nonexistent/claude")
    monkeypatch.setattr(m, "ensure_subagent_trusted", lambda *_a, **_k: None)

    def _boom(*_a, **_k):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(m.subprocess, "run", _boom)

    out = m.run_maintenance(tmp_path, tmp_path / "logs")
    assert "spawn failed" in str(out.raw.get("error", "")).lower()


def test_resolver_falls_back_to_the_sdk_bundled_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """NEV-CTL-04: prove the resolver can actually FIND something, or the tests above are vacuous."""
    monkeypatch.setattr(m.shutil, "which", lambda _n: None)  # force the fallback path
    found = m._claude_executable()
    assert found is not None and "claude" in found.lower(), (
        "the SDK bundles a claude binary; the fallback must find it"
    )
