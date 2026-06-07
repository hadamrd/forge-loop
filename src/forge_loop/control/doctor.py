"""Control-plane health checks for ``forge-loop doctor`` (issue #202).

``forge-loop doctor`` historically only inspected state that lives *outside*
the durable control plane (config load, halt markers, tmux, orphan worktrees,
git sync). After a hard reset mid-tick the control plane itself can be
unrecoverable — task sagas stuck RUNNING with lapsed leases, a projection
cursor stranded behind the event-log head — while ``doctor`` still reports
all-green. This module adds four read-only probes over the durable stores so
``doctor`` can answer "is frontier/memory/task/replay state healthy, and how
do I fix it?".

Every probe is **read-only**: the replay-determinism check re-projects into a
throwaway in-memory target and never advances the live cursor, and no probe
writes to ``.forge``. When the durable stores are absent (fresh repo) the
probes degrade to ``warn`` rather than crashing or hard-failing ``doctor``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

from forge_loop.control.status import collect_control_plane_status
from forge_loop.eventlog.projections import (
    ProjectionCursor,
    ProjectionReplayError,
    replay_projection,
)
from forge_loop.eventlog.sqlite import (
    GENESIS_CHAIN_HASH,
    canonical_event_fields,
    compute_chain_hash,
)

if TYPE_CHECKING:
    from forge_loop.eventlog.projections import Projection, ProjectionEventLog

# Check verdicts. Kept as module constants (not bare string literals at the
# call sites) so the discriminator is named once — see the manifesto rule on
# stringly-typed cross-module boundaries.
PASS = "pass"
FAIL = "fail"
WARN = "warn"

# Copy-pasteable remediation commands. These are the *exact* operator command
# surfaces: ``forge-loop recover`` (``_cmd_recover`` / ``reconcile_stale_sagas``)
# and ``forge-loop boot`` (re-projects durable state from the event log).
RECOVER_REMEDIATION = (
    "forge-loop recover   # reconcile dead-worker sagas: reap worktrees + close them"
)
REPROJECT_REMEDIATION = (
    "forge-loop boot   # re-project durable control-plane state from the event log"
)
EVENTLOG_INTEGRITY_REMEDIATION = (
    "inspect .forge/events.db at the named sequence and restore it from a known-good "
    "backup BEFORE booting (boot/replay hard-aborts on a broken hash chain)"
)

# The control-plane check names, in display order.
CHECK_NAMES = (
    "projection_lag",
    "stale_leases",
    "memory_integrity",
    "replay_determinism",
    "eventlog_integrity",
)


def _check(status: str, detail: str, remediation: str | None) -> dict[str, Any]:
    return {"status": status, "detail": detail, "remediation": remediation}


def collect_control_plane_doctor(
    repo: Path,
    now: datetime,
    *,
    state_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the four control-plane checks for ``forge-loop doctor``.

    The result is a dict keyed by :data:`CHECK_NAMES`; each value is a
    ``{"status", "detail", "remediation"}`` dict where ``status`` is one of
    :data:`PASS` / :data:`FAIL` / :data:`WARN` and ``remediation`` is a
    copy-pasteable command (or ``None``). Reuses
    :func:`collect_control_plane_status` for projection-lag / stale-lease /
    memory-count facts so ``doctor`` and ``status`` agree on the numbers.
    """

    status = collect_control_plane_status(repo, now, state_dir=state_dir)
    forge_dir = repo / ".forge"
    memory_path = forge_dir / "memory.db"
    event_log_path = forge_dir / "events.db"

    return {
        "projection_lag": _projection_lag_check(status),
        "stale_leases": _stale_leases_check(status),
        "memory_integrity": _memory_integrity_check(status, memory_path),
        "replay_determinism": _replay_determinism_check(status, event_log_path),
        "eventlog_integrity": _eventlog_integrity_check(status, event_log_path),
    }


def unavailable_checks(detail: str) -> dict[str, dict[str, Any]]:
    """Return all four checks as ``warn`` — used when the repo can't be located.

    ``doctor`` must keep running (and must not hard-fail) when config load
    fails, so the control-plane section degrades to ``warn`` rather than
    raising or being silently dropped.
    """

    return {name: _check(WARN, detail, None) for name in CHECK_NAMES}


def _projection_lag_check(status: dict[str, Any]) -> dict[str, Any]:
    event_log = status["event_log"]
    if not event_log["available"]:
        return _check(WARN, "event log absent; projection lag not applicable", None)

    projections = status["projections"]
    if not projections:
        return _check(PASS, "no projection cursors recorded (nothing to lag)", None)

    lagging = {name: data["lag"] for name, data in projections.items() if data["lag"] > 0}
    if lagging:
        detail = "; ".join(f"{name} behind head by {lag}" for name, lag in sorted(lagging.items()))
        return _check(
            FAIL,
            f"projection cursor(s) stranded behind the event-log head: {detail}",
            REPROJECT_REMEDIATION,
        )
    return _check(
        PASS,
        f"all {len(projections)} projection cursor(s) at the event-log head",
        None,
    )


def _stale_leases_check(status: dict[str, Any]) -> dict[str, Any]:
    tasks = status["tasks"]
    if not tasks["available"]:
        return _check(WARN, "task-saga store absent; stale-lease check not applicable", None)

    stale = tasks["stale_lease_count"] or 0
    in_flight = tasks["in_flight_count"] or 0
    if stale >= 1:
        return _check(
            FAIL,
            f"{stale} in-flight saga(s) with an expired lease (dead-worker candidate(s))",
            RECOVER_REMEDIATION,
        )
    return _check(
        PASS,
        f"{in_flight} in-flight saga(s); no expired leases",
        None,
    )


def _memory_integrity_check(status: dict[str, Any], memory_path: Path) -> dict[str, Any]:
    memory = status["memory"]
    if not memory["available"]:
        # Distinguish "absent" (fresh repo → warn, not applicable) from
        # "present but unopenable" (corrupt → fail). ``collect_control_plane_status``
        # adds an ``error`` key only on the open-failure path.
        if not memory_path.exists():
            return _check(
                WARN,
                f"memory store absent at {memory_path}; integrity check not applicable",
                None,
            )
        error = memory.get("error", "unknown error")
        return _check(
            FAIL,
            f"memory store at {memory_path} could not be opened: {error}",
            f"inspect or restore {memory_path}",
        )

    # On pass, report counts of decisions/active, rejected_paths, and the
    # episodic / procedural (skill) breakdown.
    from forge_loop.memory import MemoryKind, SqliteMemoryStore

    try:
        store = SqliteMemoryStore(memory_path)
        active = store.list_active()
        rejected = store.list_rejected_paths()
        episodic = store.list_active(kind=MemoryKind.EPISODIC)
        procedural = store.list_active(kind=MemoryKind.PROCEDURAL)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return _check(
            FAIL,
            f"memory store at {memory_path} could not be read: {exc}",
            f"inspect or restore {memory_path}",
        )

    detail = (
        f"decisions/active={len(active)}, rejected_paths={len(rejected)}, "
        f"episodes={len(episodic)}, skills/procedural={len(procedural)}"
    )
    return _check(PASS, detail, None)


class _StateProjection(Protocol):
    """A projection that also exposes its rebuilt ``state()`` for comparison.

    The determinism probe needs more than the public :class:`Projection`
    protocol (which only carries ``cursor`` + ``apply``): it must observe the
    *rebuilt state* two replays produce. Rather than widen the public protocol
    (explicitly out of scope for #327), the doctor probe carries its own
    ``state()`` and the check accepts any factory yielding this shape.
    """

    cursor: ProjectionCursor

    def apply(self, event: Any) -> None: ...

    def state(self) -> Any:
        """Return the JSON-serialisable rebuilt state for canonical comparison."""
        ...


@dataclass
class _CountingProjection:
    """A minimal projection that re-applies every event by sequence only.

    Determinism here means "a clean replay of the durable log reproduces the
    event-log head"; the probe deliberately does NOT decode payloads (which
    would couple it to every event kind's schema and could raise on an
    unexpected one). It only needs ``event.sequence`` to advance its cursor.
    Its :meth:`state` is replay-order-independent (it only folds in monotonic
    sequence facts), so a clean log yields byte-identical canonical JSON across
    two replays.
    """

    cursor: ProjectionCursor = ProjectionCursor()
    applied: int = 0

    def apply(self, event: Any) -> None:
        self.cursor = ProjectionCursor(sequence=event.sequence)
        self.applied += 1

    def state(self) -> dict[str, int]:
        return {"applied": self.applied, "head": self.cursor.sequence}


@dataclass
class _ReadOnlyReplayTarget:
    """Read-only :class:`ProjectionEventLog` over an existing ``events.db``.

    ``since`` streams ``(sequence)`` rows from a ``mode=ro`` SQLite connection,
    so opening it can never flip the journal mode or otherwise mutate the live
    event log (which would break the "doctor must not mutate state" invariant).
    ``advance_projection_cursor`` records the cursor in memory and NEVER writes
    back to the live store.
    """

    event_log_path: Path
    persisted: ProjectionCursor | None = field(default=None)

    def since(self, sequence: int = 0) -> Iterator[SimpleNamespace]:
        uri = f"file:{self.event_log_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT sequence FROM events WHERE sequence > ? ORDER BY sequence ASC",
                (sequence,),
            ).fetchall()
        finally:
            connection.close()
        return iter(SimpleNamespace(sequence=int(row[0])) for row in rows)

    def advance_projection_cursor(
        self,
        projection_name: str,  # noqa: ARG002 - protocol shape; throwaway target
        cursor: ProjectionCursor,
    ) -> None:
        self.persisted = cursor


def _canonical_state_json(projection: _StateProjection) -> str:
    """Canonical JSON of a projection's rebuilt state.

    Reuses the canonical-JSON convention of
    :func:`forge_loop.sandbox.policy.canonical_policy_json`
    (``sort_keys=True, separators=(",", ":")``) rather than inventing a new
    canonicaliser, so two byte strings are comparable iff the states are equal.
    """

    return json.dumps(projection.state(), sort_keys=True, separators=(",", ":"))


def _one_replay(
    target: ProjectionEventLog,
    projection_factory: Callable[[], _StateProjection],
) -> tuple[ProjectionCursor, str]:
    """Replay the whole log into a fresh projection; return (head, canonical state)."""

    projection = projection_factory()
    cursor = replay_projection(target, "doctor-replay-probe", cast("Projection", projection))
    return cursor, _canonical_state_json(projection)


def _replay_determinism_check(
    status: dict[str, Any],
    event_log_path: Path,
    *,
    projection_factory: Callable[[], _StateProjection] = _CountingProjection,
) -> dict[str, Any]:
    event_log = status["event_log"]
    if not event_log["available"]:
        return _check(WARN, "event log absent; replay determinism not applicable", None)

    last_sequence = event_log["last_sequence"] or 0

    # The lightweight read-only target/projection only consume ``event.sequence``;
    # cast to the structural protocols replay_projection expects. We replay the
    # durable log TWICE over fresh projections so we can compare the rebuilt
    # state, not just the sequence head — an order-dependent or wall-clock
    # projection reaches the same head yet yields divergent state (#327).
    target = cast("ProjectionEventLog", _ReadOnlyReplayTarget(event_log_path))
    try:
        rebuilt, first_state = _one_replay(target, projection_factory)
        _, second_state = _one_replay(target, projection_factory)
        cursors = _live_projection_cursors(event_log_path)
    except (ProjectionReplayError, sqlite3.Error, OSError, ValueError) as exc:
        return _check(
            FAIL,
            f"re-projecting from the event log failed: {exc}",
            REPROJECT_REMEDIATION,
        )

    if rebuilt.sequence != last_sequence:
        return _check(
            FAIL,
            (
                f"re-projection reached sequence {rebuilt.sequence} but the "
                f"event-log head is {last_sequence}"
            ),
            REPROJECT_REMEDIATION,
        )

    # A cursor *ahead* of a clean replay's head is a divergence the lag check
    # cannot see (lag floors at 0): the stored cursor claims more than the log
    # holds, so the live projection state cannot be reproduced from the log.
    ahead = {name: seq for name, seq in cursors.items() if seq > last_sequence}
    if ahead:
        detail = "; ".join(
            f"{name} cursor at {seq} > head {last_sequence}" for name, seq in sorted(ahead.items())
        )
        return _check(
            FAIL,
            f"projection cursor(s) diverged ahead of a clean replay: {detail}",
            REPROJECT_REMEDIATION,
        )

    # Same head, but does the rebuilt STATE match byte-for-byte across two
    # replays? If not the projection is non-deterministic (order/clock/set
    # iteration) and boot reconstruction is not reproducible.
    if first_state != second_state:
        return _check(
            FAIL,
            (
                "two clean replays produced divergent projection state "
                "(non-deterministic reduce): canonical JSON differs "
                f"({first_state!r} != {second_state!r})"
            ),
            REPROJECT_REMEDIATION,
        )

    return _check(
        PASS,
        (
            f"clean re-projection reproduced the event-log head at sequence "
            f"{last_sequence}; projection state byte-identical across two replays"
        ),
        None,
    )


def _eventlog_integrity_check(
    status: dict[str, Any],
    event_log_path: Path,
) -> dict[str, Any]:
    """Walk the event-log hash chain and report the FIRST break (#339).

    Recomputes ``chain_hash_n = sha256(prev_chain_hash + canonical(fields))``
    for every row from a ``mode=ro`` connection and compares it against the
    stored hash. The probe is strictly read-only and never raises: any
    ``sqlite3.Error``/``OSError``/decode error converts to a verdict so
    ``doctor`` runs to completion.

    - clean chain → ``PASS`` referencing the head sequence;
    - a mutated/corrupt row → ``FAIL`` naming the first offending sequence;
    - rows predating the chain-hash column (NULL) → ``WARN`` (unverifiable);
    - event log absent → ``WARN`` (not applicable).
    """

    event_log = status["event_log"]
    if not event_log["available"]:
        return _check(WARN, "event log absent; integrity not applicable", None)

    try:
        if not _has_chain_hash_column(event_log_path):
            return _check(
                WARN,
                "eventlog_integrity: event log predates the chain-hash column; "
                "integrity unverifiable",
                None,
            )
        rows = _read_event_chain_rows(event_log_path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return _check(
            FAIL,
            f"eventlog_integrity: event log could not be read: {exc}",
            EVENTLOG_INTEGRITY_REMEDIATION,
        )

    prev = GENESIS_CHAIN_HASH
    head_sequence = 0
    for row in rows:
        sequence = int(row["sequence"])
        head_sequence = sequence
        stored = row["chain_hash"]
        if stored is None:
            return _check(
                WARN,
                f"eventlog_integrity: event at sequence {sequence} predates the "
                "chain-hash column (NULL); integrity unverifiable",
                None,
            )
        try:
            expected = compute_chain_hash(
                prev,
                canonical_event_fields(
                    event_id=row["event_id"],
                    kind=row["kind"],
                    payload_json=row["payload_json"],
                    schema_version=int(row["schema_version"]),
                    occurred_at=row["occurred_at"],
                    task_id=row["task_id"],
                    saga_id=row["saga_id"],
                    idempotency_key=row["idempotency_key"],
                ),
            )
        except (ValueError, TypeError) as exc:
            return _check(
                FAIL,
                f"eventlog_integrity: could not recompute chain at sequence {sequence}: {exc}",
                EVENTLOG_INTEGRITY_REMEDIATION,
            )
        if expected != stored:
            return _check(
                FAIL,
                f"eventlog_integrity: chain-hash mismatch first at sequence {sequence} "
                "(payload tampered or corrupted)",
                EVENTLOG_INTEGRITY_REMEDIATION,
            )
        # Continue from the STORED hash so a single tampered row flags only
        # itself as the first break, not every following row.
        prev = str(stored)

    return _check(
        PASS,
        f"eventlog_integrity=ok (verified chain through head sequence {head_sequence})",
        None,
    )


def _has_chain_hash_column(event_log_path: Path) -> bool:
    """Return whether the read-only ``events`` table carries ``chain_hash``."""

    uri = f"file:{event_log_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    finally:
        connection.close()
    return "chain_hash" in columns


def _read_event_chain_rows(event_log_path: Path) -> list[sqlite3.Row]:
    """Read the hash-chain columns of every event via a read-only connection."""

    uri = f"file:{event_log_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT sequence, chain_hash, event_id, kind, payload_json, "
            "schema_version, occurred_at, task_id, saga_id, idempotency_key "
            "FROM events ORDER BY sequence ASC"
        ).fetchall()
    finally:
        connection.close()


def _live_projection_cursors(event_log_path: Path) -> dict[str, int]:
    """Read stored projection cursor sequences via a read-only connection."""

    uri = f"file:{event_log_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT projection_name, sequence FROM projection_cursors"
        ).fetchall()
    finally:
        connection.close()
    return {str(name): int(sequence) for name, sequence in rows}
