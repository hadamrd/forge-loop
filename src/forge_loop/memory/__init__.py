"""Curated long-term project memory contracts."""

from __future__ import annotations

from pathlib import Path

from forge_loop.memory.models import (
    AXIS_TAG_PREFIX,
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    axis_from_tags,
    axis_tag,
    derive_memory_id,
)
from forge_loop.memory.store import MemoryStore, SqliteMemoryStore


def memory_db_path(repo_path: str | Path) -> Path:
    """Canonical on-disk location of the durable memory store for a repo.

    Single source of truth for the ``.forge/memory.db`` convention so the CLI
    factories (and anything else resolving the store) cannot drift.
    """
    return Path(repo_path) / ".forge" / "memory.db"


def open_memory_store(repo_path: str | Path) -> SqliteMemoryStore:
    """Construct the durable ``SqliteMemoryStore`` at ``.forge/memory.db``.

    Single construction site so the path convention lives in exactly one place
    (see ``memory_db_path``). Callers that need optional/degrading behaviour
    should guard this call themselves.
    """
    return SqliteMemoryStore(memory_db_path(repo_path))


__all__ = [
    "AXIS_TAG_PREFIX",
    "REJECTED_PATH_TAG",
    "MemoryItem",
    "MemoryKind",
    "MemoryProvenance",
    "MemoryStore",
    "SqliteMemoryStore",
    "axis_from_tags",
    "axis_tag",
    "derive_memory_id",
    "memory_db_path",
    "open_memory_store",
]
