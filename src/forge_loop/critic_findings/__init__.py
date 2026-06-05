"""First-class durable critic findings (#242).

Critic findings are persisted to a SQLite projection under ``.forge/`` so the
repair worker reads them from the durable control plane instead of re-fetching
GitHub review comments (which 422 and silently drop findings — manifesto Q10).
"""

from __future__ import annotations

from pathlib import Path

from forge_loop.critic_findings.sqlite import SqliteCriticFindingsStore
from forge_loop.critic_findings.store import (
    CriticFindingsStore,
    FindingStatus,
    ReconcileResult,
    StoredFinding,
)


def critic_findings_db_path(repo_path: str | Path) -> Path:
    """Canonical on-disk location of the critic-findings store for a repo.

    Single source of truth for the ``.forge/critic_findings.db`` convention so
    every resolver (critic write path, repair read path, MCP tools) agrees.
    """

    return Path(repo_path) / ".forge" / "critic_findings.db"


def open_critic_findings_store(repo_path: str | Path) -> SqliteCriticFindingsStore:
    """Construct the durable store at ``.forge/critic_findings.db``."""

    return SqliteCriticFindingsStore(critic_findings_db_path(repo_path))


__all__ = [
    "CriticFindingsStore",
    "FindingStatus",
    "ReconcileResult",
    "SqliteCriticFindingsStore",
    "StoredFinding",
    "critic_findings_db_path",
    "open_critic_findings_store",
]
