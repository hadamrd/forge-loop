"""SQLite-backed durable store for first-class critic findings (#242).

Mirrors :mod:`forge_loop.eventlog.sqlite` (WAL journal, ``IF NOT EXISTS``
schema, ``schema_version`` column, crash-safe re-open) and
:mod:`forge_loop.memory.store` (parent-dir creation, ``:memory:`` support for
tests). The ``status`` column carries a ``CHECK`` constraint so an invalid
lifecycle state is rejected by the database itself, not just application code.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from forge_loop.critic import Finding, canonical_pr_key, derive_finding_id
from forge_loop.critic_findings.store import (
    FindingStatus,
    ReconcileResult,
    StoredFinding,
)

#: Bump when the on-disk shape changes (mirrors eventlog's schema_version).
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS critic_findings (
    finding_id TEXT PRIMARY KEY,
    pr TEXT NOT NULL,
    issue INTEGER NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    file TEXT,
    line INTEGER,
    message TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','addressed','wontfix')),
    note TEXT,
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_critic_findings_pr ON critic_findings(pr);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteCriticFindingsStore:
    """Durable critic-findings projection stored in SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        connect_path: str | Path = ":memory:" if str(path) == ":memory:" else self.path
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(connect_path)
        self._connection.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying SQLite connection.

        The repair hot path (``blocking_pr_repairs`` /
        ``ready_issue_open_pr_repairs``) opens a store per runner tick; without
        an explicit close that leaks a WAL connection (and its file handles)
        every tick. Callers that own the store close it; ``__exit__`` makes the
        ``with`` form do so automatically. Idempotent.
        """

        self._connection.close()

    def __enter__(self) -> SqliteCriticFindingsStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── writes ────────────────────────────────────────────────────────────

    def upsert(self, pr: str, issue: int, finding: Finding) -> StoredFinding:
        """Insert (status=open) or update one finding idempotently by id.

        On conflict the *content* (severity/category/file/line/message) is
        refreshed but the existing ``status`` and ``note`` are preserved — a
        worker's ``addressed``/``wontfix`` decision is never silently clobbered
        by a re-write. Lifecycle transitions go through :meth:`set_status` /
        :meth:`reconcile`.
        """

        pr = canonical_pr_key(pr)
        finding_id = derive_finding_id(pr, issue, finding)
        now = _now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO critic_findings (
                    finding_id, pr, issue, severity, category,
                    file, line, message, status, note, schema_version,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(finding_id) DO UPDATE SET
                    severity = excluded.severity,
                    category = excluded.category,
                    file = excluded.file,
                    line = excluded.line,
                    message = excluded.message,
                    updated_at = excluded.updated_at
                """,
                (
                    finding_id,
                    pr,
                    issue,
                    finding.severity,
                    finding.category,
                    finding.file,
                    finding.line,
                    finding.message,
                    FindingStatus.OPEN.value,
                    None,
                    SCHEMA_VERSION,
                    now,
                    now,
                ),
            )
        stored = self.get(finding_id)
        if stored is None:  # pragma: no cover - defensive
            raise RuntimeError("upsert did not persist a finding row")
        return stored

    def set_status(
        self, finding_id: str, status: FindingStatus, *, note: str | None = None
    ) -> StoredFinding | None:
        """Transition a finding's status and persist ``note``."""

        existing = self.get(finding_id)
        if existing is None:
            return None
        new_note = note if note is not None else existing.note
        with self._connection:
            self._connection.execute(
                "UPDATE critic_findings SET status = ?, note = ?, updated_at = ? "
                "WHERE finding_id = ?",
                (status.value, new_note, _now(), finding_id),
            )
        return self.get(finding_id)

    def reconcile(
        self, pr: str, issue: int, findings: list[Finding]
    ) -> ReconcileResult:
        """Closed-loop re-review reconciliation — see protocol docstring."""

        pr = canonical_pr_key(pr)
        current = {derive_finding_id(pr, issue, f): f for f in findings}
        existing = {row.finding_id: row for row in self.all_findings(pr)}

        inserted = kept_open = reopened = closed = 0

        # 1. Findings still present on this re-review.
        for fid, finding in current.items():
            prior = existing.get(fid)
            self.upsert(pr, issue, finding)
            if prior is None:
                inserted += 1
            elif prior.status is FindingStatus.ADDRESSED:
                # Worker claimed it fixed, but the critic still sees it →
                # reopen (no false convergence). ``wontfix`` is respected.
                self.set_status(fid, FindingStatus.OPEN)
                reopened += 1
            elif prior.status is FindingStatus.WONTFIX:
                pass  # respected; left wontfix
            else:
                kept_open += 1

        # 2. Findings we tracked before but the critic no longer reports →
        # resolved. Close anything still open (wontfix stays wontfix).
        for fid, row in existing.items():
            if fid in current:
                continue
            if row.status is FindingStatus.OPEN:
                self.set_status(fid, FindingStatus.ADDRESSED)
                closed += 1

        return ReconcileResult(
            inserted=inserted,
            kept_open=kept_open,
            reopened=reopened,
            closed=closed,
            open_count=self.open_count(pr),
        )

    # ── reads ─────────────────────────────────────────────────────────────

    def get(self, finding_id: str) -> StoredFinding | None:
        row = self._connection.execute(
            "SELECT * FROM critic_findings WHERE finding_id = ?",
            (finding_id,),
        ).fetchone()
        return self._row(row) if row is not None else None

    def open_findings(self, pr: str) -> tuple[StoredFinding, ...]:
        rows = self._connection.execute(
            "SELECT * FROM critic_findings WHERE pr = ? AND status = ? "
            "ORDER BY created_at ASC, finding_id ASC",
            (canonical_pr_key(pr), FindingStatus.OPEN.value),
        ).fetchall()
        return tuple(self._row(row) for row in rows)

    def all_findings(self, pr: str) -> tuple[StoredFinding, ...]:
        rows = self._connection.execute(
            "SELECT * FROM critic_findings WHERE pr = ? "
            "ORDER BY created_at ASC, finding_id ASC",
            (canonical_pr_key(pr),),
        ).fetchall()
        return tuple(self._row(row) for row in rows)

    def open_count(self, pr: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM critic_findings WHERE pr = ? AND status = ?",
            (canonical_pr_key(pr), FindingStatus.OPEN.value),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    @staticmethod
    def _row(row: sqlite3.Row) -> StoredFinding:
        return StoredFinding(
            finding_id=row["finding_id"],
            pr=row["pr"],
            issue=row["issue"],
            severity=row["severity"],
            category=row["category"],
            file=row["file"],
            line=row["line"],
            message=row["message"],
            status=FindingStatus(row["status"]),
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
