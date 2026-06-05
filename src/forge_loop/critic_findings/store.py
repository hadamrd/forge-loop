"""Durable store contract for first-class critic findings (#242).

Critic findings used to live only in memory and round-trip through GitHub
review comments — the repair worker rebuilt its brief by *re-fetching* those
comments. An inline comment on an out-of-diff line 422s and the finding
silently vanishes, so the worker repaired blind (the #230/#234 churn class,
manifesto Q10).

This module makes a :class:`forge_loop.critic.Finding` a first-class,
addressable, stateful work item in the durable ``.forge/`` control plane:
event-sourcing applied to review (log = ``events.db``, projection =
``critic_findings``, GitHub comments = a derived human read-model).

The protocol mirrors :mod:`forge_loop.eventlog.store` /
:mod:`forge_loop.memory.store`: a ``typing.Protocol`` boundary (manifesto Q2)
with a SQLite implementation in :mod:`forge_loop.critic_findings.sqlite`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from forge_loop.critic import Finding


#: The closed set of finding lifecycle states. A ``StrEnum`` so cross-module
#: comparisons are type-checked, never stringly-typed (manifesto rule
#: "No stringly-typed cross-module event boundaries"). The same vocabulary is
#: enforced at the SQLite layer by a ``CHECK(status IN (...))`` constraint.
class FindingStatus(StrEnum):
    OPEN = "open"
    ADDRESSED = "addressed"
    WONTFIX = "wontfix"


#: Convergence baseline: a PR is "drained" when no finding is OPEN.
OPEN_STATUSES = frozenset({FindingStatus.OPEN})


@dataclass(frozen=True)
class StoredFinding:
    """A critic :class:`Finding` persisted as a durable, stateful work item."""

    finding_id: str
    pr: str
    issue: int
    severity: str
    category: str
    file: str | None
    line: int | None
    message: str
    status: FindingStatus
    note: str | None
    created_at: str
    updated_at: str

    def to_finding(self) -> Finding:
        """Project back to the in-memory critic :class:`Finding` shape."""

        return Finding(
            severity=self.severity,
            category=self.category,
            file=self.file,
            line=self.line,
            message=self.message,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialise for MCP tool responses (FastMCP returns plain JSON)."""

        return {
            "finding_id": self.finding_id,
            "pr": self.pr,
            "issue": self.issue,
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "status": self.status.value,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a closed-loop re-review reconciliation pass (AC6)."""

    inserted: int
    kept_open: int
    reopened: int
    closed: int
    open_count: int


class CriticFindingsStore(Protocol):
    """Persistence boundary for first-class critic findings."""

    def upsert(self, pr: str, issue: int, finding: Finding) -> StoredFinding:
        """Insert (status=open) or update one finding idempotently by id."""
        ...

    def get(self, finding_id: str) -> StoredFinding | None:
        """Return one finding by its globally-unique id, or ``None``."""
        ...

    def open_findings(self, pr: str) -> tuple[StoredFinding, ...]:
        """Return all ``open`` findings for ``pr`` (the brief baseline)."""
        ...

    def all_findings(self, pr: str) -> tuple[StoredFinding, ...]:
        """Return every stored finding for ``pr`` regardless of status."""
        ...

    def set_status(
        self, finding_id: str, status: FindingStatus, *, note: str | None = None
    ) -> StoredFinding | None:
        """Transition a finding's status; persist ``note``. ``None`` if absent."""
        ...

    def open_count(self, pr: str) -> int:
        """Number of ``open`` findings for ``pr`` (convergence == 0)."""
        ...

    def close(self) -> None:
        """Release any backing resources (e.g. a SQLite connection).

        The repair hot path opens a store per tick, so an owner that creates a
        store must be able to release it deterministically rather than leak a
        connection every tick.
        """
        ...

    def reconcile(
        self, pr: str, issue: int, findings: list[Finding]
    ) -> ReconcileResult:
        """Closed-loop re-review reconciliation (AC6).

        - still-present finding → keep ``open``;
        - worker-marked ``addressed`` but still present → ``reopen`` to open
          (no false convergence);
        - ``wontfix`` is respected (never auto-reopened);
        - previously-tracked finding now absent → ``close`` (mark addressed);
        - brand-new finding → insert as ``open``.
        """
        ...


def render_findings_block(findings: tuple[StoredFinding, ...] | list[StoredFinding]) -> str:
    """Render durable findings as the AUTHORITATIVE repair-brief baseline.

    This is the single rendering path shared by the repair-dispatch wiring and
    tests. It is tool-free and deterministic: the worker has the findings even
    if it never calls an MCP tool AND even if GitHub posting 422'd (AC3/AC5).
    Returns ``""`` for an empty list so callers can concatenate unconditionally.
    """

    if not findings:
        return ""
    lines = [
        "DURABLE CRITIC FINDINGS (authoritative — from the .forge control "
        "plane, NOT re-fetched from GitHub):",
        "These are the source of truth for what to repair. Address every one; "
        "the PR converges when the open-count drains to 0.",
    ]
    for f in findings:
        loc = ""
        if f.file:
            loc = f" {f.file}" + (f":{f.line}" if f.line is not None else "")
        lines.append(f"- [{f.severity}/{f.category}]{loc} — {f.message} (id={f.finding_id})")
    return "\n".join(lines)
