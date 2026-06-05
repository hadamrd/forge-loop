"""Unit tests for the durable critic-findings store (#242).

Covers store CRUD + lifecycle: upsert idempotency on
``(pr, issue, attempt, finding_id)``, status transitions
``open -> addressed -> wontfix``, the ``CHECK`` constraint rejecting a bad
status, the ``status='open'`` query filter, and crash-safe re-open of an
existing on-disk db.
"""

from __future__ import annotations

import sqlite3

import pytest

from forge_loop.critic import Finding, derive_finding_id
from forge_loop.critic_findings import (
    SqliteCriticFindingsStore,
    critic_findings_db_path,
    open_critic_findings_store,
)
from forge_loop.critic_findings.store import FindingStatus

PR = "https://github.com/acme/widgets/pull/7"


def _finding(message: str = "boom", *, file: str | None = "a.py", line: int | None = 10) -> Finding:
    return Finding(severity="sev2", category="correctness", file=file, line=line, message=message)


def test_upsert_inserts_open_and_is_idempotent() -> None:
    store = SqliteCriticFindingsStore(":memory:")
    f = _finding()
    first = store.upsert(PR, 242, 1, f)
    assert first.status is FindingStatus.OPEN
    # Same (pr, issue, attempt, finding) → idempotent (one row), and the SAME
    # finding at a LATER attempt must also not duplicate (AC2).
    store.upsert(PR, 242, 1, f)
    store.upsert(PR, 242, 2, f)
    rows = store.all_findings(PR)
    assert len(rows) == 1
    assert rows[0].finding_id == derive_finding_id(PR, 242, f)
    # attempt column tracks the last-observed attempt.
    assert rows[0].attempt == 2


def test_status_transitions_open_addressed_wontfix() -> None:
    store = SqliteCriticFindingsStore(":memory:")
    stored = store.upsert(PR, 242, 1, _finding())
    fid = stored.finding_id

    addressed = store.set_status(fid, FindingStatus.ADDRESSED, note="fixed in abc123")
    assert addressed is not None
    assert addressed.status is FindingStatus.ADDRESSED
    assert addressed.note == "fixed in abc123"

    wontfix = store.set_status(fid, FindingStatus.WONTFIX)
    assert wontfix is not None
    assert wontfix.status is FindingStatus.WONTFIX
    # note is preserved when not overridden.
    assert wontfix.note == "fixed in abc123"


def test_set_status_missing_finding_returns_none() -> None:
    """Sad path: addressing a non-existent finding_id returns None, not raise."""
    store = SqliteCriticFindingsStore(":memory:")
    assert store.set_status("deadbeef", FindingStatus.ADDRESSED) is None


def test_upsert_preserves_worker_status_on_conflict() -> None:
    """A re-write of an already-``addressed`` finding must NOT silently reset it
    to ``open`` — the worker's decision survives a plain content upsert."""
    store = SqliteCriticFindingsStore(":memory:")
    stored = store.upsert(PR, 242, 1, _finding())
    store.set_status(stored.finding_id, FindingStatus.ADDRESSED)
    # Re-observe the same finding (e.g. content refresh) at attempt 2.
    again = store.upsert(PR, 242, 2, _finding())
    assert again.status is FindingStatus.ADDRESSED


def test_check_constraint_rejects_bad_status() -> None:
    """The DB-level CHECK rejects any status outside the closed vocabulary."""
    store = SqliteCriticFindingsStore(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO critic_findings (finding_id, pr, issue, attempt, severity, "
            "category, file, line, message, status, note, schema_version, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x", PR, 242, 1, "sev2", "correctness", None, None, "m", "bogus", None, 1, "t", "t"),
        )


def test_open_findings_filter_excludes_non_open() -> None:
    store = SqliteCriticFindingsStore(":memory:")
    a = store.upsert(PR, 242, 1, _finding("first", line=1))
    store.upsert(PR, 242, 1, _finding("second", line=2))
    store.set_status(a.finding_id, FindingStatus.ADDRESSED)

    open_rows = store.open_findings(PR)
    assert [r.message for r in open_rows] == ["second"]
    assert store.open_count(PR) == 1
    assert len(store.all_findings(PR)) == 2


def test_crash_safe_reopen_of_existing_db(tmp_path) -> None:
    """A store re-opened on the same path sees previously-persisted findings."""
    db = tmp_path / ".forge" / "critic_findings.db"
    store = SqliteCriticFindingsStore(db)
    stored = store.upsert(PR, 242, 1, _finding("durable"))
    store._connection.close()  # simulate process exit

    reopened = SqliteCriticFindingsStore(db)
    again = reopened.get(stored.finding_id)
    assert again is not None
    assert again.message == "durable"
    assert again.status is FindingStatus.OPEN


def test_db_path_and_factory_use_dot_forge(tmp_path) -> None:
    assert critic_findings_db_path(tmp_path) == tmp_path / ".forge" / "critic_findings.db"
    store = open_critic_findings_store(tmp_path)
    assert store.path == tmp_path / ".forge" / "critic_findings.db"
    # parent dir is created lazily by the store ctor.
    assert (tmp_path / ".forge").is_dir()
