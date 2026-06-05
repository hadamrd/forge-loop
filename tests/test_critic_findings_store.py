"""Unit tests for the durable critic-findings store (#242).

Covers store CRUD + lifecycle: upsert idempotency on
``(pr, issue, finding_id)``, status transitions
``open -> addressed -> wontfix``, the ``CHECK`` constraint rejecting a bad
status, the ``status='open'`` query filter, and crash-safe re-open of an
existing on-disk db.
"""

from __future__ import annotations

import sqlite3

import pytest

from forge_loop.critic import Finding, canonical_pr_key, derive_finding_id
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
    first = store.upsert(PR, 242, f)
    assert first.status is FindingStatus.OPEN
    # Same (pr, issue, finding) → idempotent (one row), and the SAME finding
    # re-observed on a later re-review must also not duplicate (AC2).
    store.upsert(PR, 242, f)
    store.upsert(PR, 242, f)
    rows = store.all_findings(PR)
    assert len(rows) == 1
    assert rows[0].finding_id == derive_finding_id(PR, 242, f)


def test_status_transitions_open_addressed_wontfix() -> None:
    store = SqliteCriticFindingsStore(":memory:")
    stored = store.upsert(PR, 242, _finding())
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
    stored = store.upsert(PR, 242, _finding())
    store.set_status(stored.finding_id, FindingStatus.ADDRESSED)
    # Re-observe the same finding (e.g. content refresh) on a later re-review.
    again = store.upsert(PR, 242, _finding())
    assert again.status is FindingStatus.ADDRESSED


def test_check_constraint_rejects_bad_status() -> None:
    """The DB-level CHECK rejects any status outside the closed vocabulary."""
    store = SqliteCriticFindingsStore(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO critic_findings (finding_id, pr, issue, severity, "
            "category, file, line, message, status, note, schema_version, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x", PR, 242, "sev2", "correctness", None, None, "m", "bogus", None, 1, "t", "t"),
        )


def test_open_findings_filter_excludes_non_open() -> None:
    store = SqliteCriticFindingsStore(":memory:")
    a = store.upsert(PR, 242, _finding("first", line=1))
    store.upsert(PR, 242, _finding("second", line=2))
    store.set_status(a.finding_id, FindingStatus.ADDRESSED)

    open_rows = store.open_findings(PR)
    assert [r.message for r in open_rows] == ["second"]
    assert store.open_count(PR) == 1
    assert len(store.all_findings(PR)) == 2


def test_crash_safe_reopen_of_existing_db(tmp_path) -> None:
    """A store re-opened on the same path sees previously-persisted findings."""
    db = tmp_path / ".forge" / "critic_findings.db"
    store = SqliteCriticFindingsStore(db)
    stored = store.upsert(PR, 242, _finding("durable"))
    store.close()  # simulate process exit via the public lifecycle API

    reopened = SqliteCriticFindingsStore(db)
    again = reopened.get(stored.finding_id)
    assert again is not None
    assert again.message == "durable"
    assert again.status is FindingStatus.OPEN
    reopened.close()


def test_close_releases_connection_and_is_usable_as_context_manager(tmp_path) -> None:
    """sev2/performance regression guard: the repair hot path opens a store per
    tick, so the store MUST release its WAL connection deterministically rather
    than leak one every tick. ``close()`` shuts the connection; the ``with``
    form closes on exit; both must persist the written row across a re-open."""
    db = tmp_path / ".forge" / "critic_findings.db"

    with SqliteCriticFindingsStore(db) as store:
        fid = store.upsert(PR, 242, _finding("scoped")).finding_id
    # After the context exits the connection is closed — further use raises.
    with pytest.raises(sqlite3.ProgrammingError):
        store.get(fid)

    # The row survived the close, proving the data was durably committed.
    reopened = SqliteCriticFindingsStore(db)
    assert reopened.get(fid) is not None
    reopened.close()
    # close() is safe to call again (idempotent shutdown).
    reopened.close()


@pytest.mark.parametrize(
    ("write_pr", "read_pr"),
    [
        # html_url written, same with a trailing slash read back.
        (PR, PR + "/"),
        # html_url written, REST api url (``/repos/.../pulls/``) read back.
        (PR, "https://api.github.com/repos/acme/widgets/pulls/7"),
        # api url written, html_url read back (the reverse drift).
        ("https://api.github.com/repos/acme/widgets/pulls/7", PR),
        # trailing-slash html_url written, clean html_url read back.
        (PR + "/", PR),
    ],
)
def test_pr_key_drift_still_resolves(write_pr: str, read_pr: str) -> None:
    """#242 review fix: any PR-URL format drift between the critic WRITE path and
    the worker READ path must still resolve to the same findings.

    Without canonicalisation, ``open_findings(read_pr)`` returns ``[]`` and the
    worker repairs BLIND — reintroducing the Q10 failure mode. The store keys on
    a single canonical PR id so write/read/MCP paths converge.
    """
    store = SqliteCriticFindingsStore(":memory:")
    store.upsert(write_pr, 242, _finding("drift"))

    open_rows = store.open_findings(read_pr)
    assert [r.message for r in open_rows] == ["drift"]
    assert store.open_count(read_pr) == 1
    assert len(store.all_findings(read_pr)) == 1
    # The finding_id is itself stable across the format drift.
    assert open_rows[0].finding_id == derive_finding_id(read_pr, 242, _finding("drift"))


def test_canonical_pr_key_collapses_known_shapes() -> None:
    """The single canonicalisation helper used by every write/read/MCP path."""
    canonical = "acme/widgets#7"
    assert canonical_pr_key("https://github.com/acme/widgets/pull/7") == canonical
    assert canonical_pr_key("https://github.com/acme/widgets/pull/7/") == canonical
    assert canonical_pr_key("https://api.github.com/repos/acme/widgets/pulls/7") == canonical
    # A bare number (int or str) collapses to a stable ``#n`` key.
    assert canonical_pr_key(7) == "#7"
    assert canonical_pr_key("7") == "#7"
    assert canonical_pr_key("#7") == "#7"
    # The helper is idempotent — re-canonicalising a canonical key is a no-op.
    assert canonical_pr_key(canonical) == canonical
    # An unrecognised shape is trimmed but otherwise preserved (consistent compare).
    assert canonical_pr_key("  weird-key/  ") == "weird-key"


def test_db_path_and_factory_use_dot_forge(tmp_path) -> None:
    assert critic_findings_db_path(tmp_path) == tmp_path / ".forge" / "critic_findings.db"
    store = open_critic_findings_store(tmp_path)
    assert store.path == tmp_path / ".forge" / "critic_findings.db"
    # parent dir is created lazily by the store ctor.
    assert (tmp_path / ".forge").is_dir()
