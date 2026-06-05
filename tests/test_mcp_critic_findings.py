"""MCP tool tests for first-class critic findings (#242, AC4).

``critic_findings(pr)`` returns the typed open findings; ``mark_finding_addressed``
flips status + persists the note. Both are registered on the ``forge-loop`` MCP
server, which the worker allow-list (``allowed_mcp_tools`` default includes
``forge-loop``) grants via the ``mcp__forge-loop__*`` pattern.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge_loop import mcp_server
from forge_loop._worker_sdk import build_allowed_tools_patterns
from forge_loop.config import Config
from forge_loop.critic import Finding
from forge_loop.critic_findings import open_critic_findings_store
from forge_loop.critic_findings.store import FindingStatus

PR = "https://github.com/acme/widgets/pull/3"


def _seed(repo) -> str:  # noqa: ANN001
    store = open_critic_findings_store(repo)
    f = Finding(severity="sev1", category="security", file="s.py", line=4, message="leak")
    stored = store.upsert(PR, 242, 1, f)
    return stored.finding_id


def test_critic_findings_tool_returns_typed_open_findings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
    _seed(tmp_path)

    rows = mcp_server.critic_findings(PR)
    assert len(rows) == 1
    row = rows[0]
    assert row["severity"] == "sev1"
    assert row["category"] == "security"
    assert row["file"] == "s.py"
    assert row["line"] == 4
    assert row["message"] == "leak"
    assert row["status"] == "open"


def test_mark_finding_addressed_flips_status_and_persists_note(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
    fid = _seed(tmp_path)

    result = mcp_server.mark_finding_addressed(fid, note="patched in def456")
    assert result["status"] == "addressed"
    assert result["note"] == "patched in def456"

    # The tool reads its own store; the finding is no longer open.
    assert mcp_server.critic_findings(PR) == []
    # And the durable store agrees.
    store = open_critic_findings_store(tmp_path)
    assert store.get(fid).status is FindingStatus.ADDRESSED


def test_mark_finding_addressed_missing_id_returns_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sad path: an unknown finding_id yields an error dict, not a crash."""
    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
    result = mcp_server.mark_finding_addressed("nope")
    assert "error" in result


def test_tools_reachable_under_worker_allow_list() -> None:
    """Both tools live on the ``forge-loop`` server, which the default
    allow-list grants — proving they are reachable to the worker (AC4)."""
    patterns = build_allowed_tools_patterns(("forge-loop",))
    assert any("forge-loop" in p for p in patterns)
    # The wildcard grant covers any tool registered on the server.
    assert any(p == "mcp__forge-loop" or p.startswith("mcp__forge-loop") for p in patterns)


def test_empty_when_no_findings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)
    assert mcp_server.critic_findings(PR) == []


# silence unused import lint if Any not otherwise referenced
_ = Any
