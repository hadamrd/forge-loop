"""Control-plane health summary for operator status output."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge_loop.control.boot import BootContext, canonical_task_saga_path
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import SqliteMemoryStore
from forge_loop.tasks import SqliteTaskSagaStore

if TYPE_CHECKING:
    from forge_loop.adapters.git import GitClient
    from forge_loop.gh_client import GhClient


def collect_control_plane_status(
    repo: Path,
    now: datetime,
    *,
    state_dir: Path | None = None,
    github_repo: str | None = None,
    git: GitClient | None = None,
    gh: GhClient | None = None,
) -> dict[str, Any]:
    """Return durable control-plane health for ``forge-loop status --json``.

    ``state_dir`` is accepted for caller compatibility (doctor passes it) but
    no longer locates task health: task in-flight / stale-lease facts are read
    from the canonical saga store at ``.forge/tasks.db`` — the same store the
    runner dispatch path, :func:`assemble_boot_context`, and ``forge-loop
    recover`` use — never the legacy ``worker-sessions.db`` (issue #373).
    """

    del state_dir  # legacy worker-sessions.db location is no longer consulted
    forge_dir = repo / ".forge"
    event_log_path = forge_dir / "events.db"
    frontier_path = forge_dir / "frontier.yaml"
    memory_path = forge_dir / "memory.db"
    tasks_path = canonical_task_saga_path(repo)

    event_log, projections, last_sequence = _event_log_status(event_log_path)
    frontier, frontier_cursor = _frontier_status(frontier_path)
    memory, active_memory_ids = _memory_status(memory_path)
    tasks, in_flight_task_ids = _tasks_status(tasks_path, now)
    boot = _boot_status(
        event_log_available=bool(event_log["available"]),
        frontier_cursor=frontier_cursor,
        memory_available=bool(memory["available"]),
        tasks_available=bool(tasks["available"]),
        active_memory_ids=active_memory_ids,
        in_flight_task_ids=in_flight_task_ids,
        last_sequence=last_sequence,
    )

    operational_entropy = _operational_entropy(
        repo, now, github_repo=github_repo, git=git, gh=gh
    )

    return {
        "event_log": event_log,
        "projections": projections,
        "frontier": frontier,
        "memory": memory,
        "tasks": tasks,
        "boot": boot,
        "operational_entropy": operational_entropy,
    }


def _operational_entropy(
    repo: Path,
    now: datetime,
    *,
    github_repo: str | None,
    git: GitClient | None,
    gh: GhClient | None,
) -> dict[str, Any]:
    """Four cheap, read-only divergence counts for operators (issue #402).

    Each source is independently best-effort: a git or GitHub failure yields
    ``None`` (or ``0`` for an empty-but-reachable backlog) for that count and
    NEVER raises out of the status path — mirroring the degrade-don't-crash
    pattern in ``_open_issue_numbers`` / ``_backlog``. The reads are one branch
    list, one ``git worktree list``, and one ``list_open_backlog`` call (epics +
    backlog age share that single query — no per-issue N+1, manifesto Q9).
    """

    open_epics, backlog_age_days = _backlog_entropy(github_repo, now, gh)
    return {
        "open_branches": _open_branches(repo, git),
        "live_worktrees": _live_worktrees(repo, git),
        "open_epics": open_epics,
        "backlog_age_days": backlog_age_days,
    }


def _open_branches(repo: Path, git: GitClient | None) -> int | None:
    """Count local ``loop/<n>`` branches via ``git branch``; ``None`` on git failure.

    Only ``loop/``-prefixed branches are counted: they are the loop's own
    exhaust (generation branches that must converge / be drained), the signal an
    operator watches for 'quietly accumulating orphan branches'. Unrelated
    branches (``trunk``, feature branches) are not divergence the loop owns, so
    counting them would make this a misleading convergence gauge (issue #415).
    The ``* `` current-branch marker and indentation from ``git branch`` are
    stripped before the prefix test.
    """
    try:
        client = git if git is not None else _default_git()
        result = client.branch_list(repo)
        if not result.ok:
            return None
        return sum(
            1
            for line in result.stdout.splitlines()
            if line.strip().lstrip("* ").startswith("loop/")
        )
    except Exception:  # noqa: BLE001 — best-effort metric, never raises
        return None


def _live_worktrees(repo: Path, git: GitClient | None) -> int | None:
    """Count worktrees from ``git worktree list --porcelain``; ``None`` on failure.

    Each worktree is one ``worktree <path>`` stanza header in the porcelain
    output, so counting those lines tolerates garbage trailing content without
    over-counting (an adversarial-input concern, manifesto T2).
    """
    try:
        client = git if git is not None else _default_git()
        result = client.worktree_list(repo)
        if not result.ok:
            return None
        return sum(1 for line in result.stdout.splitlines() if line.startswith("worktree "))
    except Exception:  # noqa: BLE001 — best-effort metric, never raises
        return None


def _backlog_entropy(
    github_repo: str | None, now: datetime, gh: GhClient | None
) -> tuple[int | None, int | None]:
    """``(open_epics, backlog_age_days)`` from ONE ``list_open_backlog`` call.

    ``(None, None)`` when the repo slug is unconfigured or the GitHub query
    raises (no token / network). An empty-but-reachable backlog returns
    ``backlog_age_days == 0`` (not a crash on ``min()`` of an empty sequence).
    """
    if not github_repo or "/" not in github_repo:
        return (None, None)
    owner, name = github_repo.split("/", 1)
    try:
        from forge_loop.gh_client import GithubkitClient, list_open_backlog

        client = gh if gh is not None else GithubkitClient()
        backlog = list_open_backlog(client, owner, name)
    except Exception:  # noqa: BLE001 — best-effort metric, never raises
        return (None, None)
    open_epics = len(backlog.epics)
    age = _oldest_age_days(list(backlog.epics) + list(backlog.tickets), now)
    return (open_epics, age)


def _oldest_age_days(issues: list[Any], now: datetime) -> int:
    """Whole-day age of the oldest issue ``created_at``; ``0`` when none dated."""
    oldest: datetime | None = None
    for issue in issues:
        created = _parse_iso(getattr(issue, "created_at", None))
        if created is None:
            continue
        if oldest is None or created < oldest:
            oldest = created
    if oldest is None:
        return 0
    reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return max(0, (reference - oldest).days)


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string to an aware ``datetime``; ``None`` if unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _default_git() -> GitClient:
    from forge_loop.adapters.git import SubprocessGit

    return SubprocessGit()


def _event_log_status(path: Path) -> tuple[dict[str, Any], dict[str, Any], int | None]:
    if not path.exists():
        return _unavailable_event_log(path), {}, None

    try:
        with sqlite3.connect(path) as connection:
            last_sequence = _last_event_sequence(connection)
            projections = _projection_status(connection, last_sequence)
    except sqlite3.Error as exc:
        status = _unavailable_event_log(path)
        status["error"] = str(exc)
        return status, {}, None

    return (
        {
            "available": True,
            "path": str(path),
            "last_sequence": last_sequence,
        },
        projections,
        last_sequence,
    )


def _unavailable_event_log(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "last_sequence": None,
    }


def _last_event_sequence(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()
    return int(row[0]) if row is not None else 0


def _projection_status(connection: sqlite3.Connection, last_sequence: int) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT projection_name, sequence FROM projection_cursors ORDER BY projection_name"
    ).fetchall()
    return {
        str(name): {
            "sequence": int(sequence),
            "lag": max(0, last_sequence - int(sequence)),
        }
        for name, sequence in rows
    }


def _frontier_status(path: Path) -> tuple[dict[str, Any], FrontierCursor | None]:
    if not path.exists():
        return _unavailable_frontier(path), None

    try:
        cursor = FrontierStore(path).load()
    except (OSError, ValueError) as exc:
        status = _unavailable_frontier(path)
        status["error"] = str(exc)
        return status, None

    return (
        {
            "available": True,
            "path": str(path),
            "current_problem": cursor.current_problem,
            "next_expansion": cursor.next_expansion,
        },
        cursor,
    )


def _unavailable_frontier(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "current_problem": None,
        "next_expansion": None,
    }


def _memory_status(path: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    if not path.exists():
        return _unavailable_memory(path), ()

    try:
        store = SqliteMemoryStore(path)
        active = store.list_active()
        rejected = store.list_rejected_paths()
    except (OSError, sqlite3.Error, ValueError) as exc:
        status = _unavailable_memory(path)
        status["error"] = str(exc)
        return status, ()

    return (
        {
            "available": True,
            "path": str(path),
            "active_count": len(active),
            "rejected_count": len(rejected),
        },
        tuple(item.memory_id for item in active),
    )


def _unavailable_memory(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "active_count": None,
        "rejected_count": None,
    }


def _tasks_status(path: Path, now: datetime) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Read task health from the canonical saga store at ``.forge/tasks.db``.

    Uses the same APIs dispatch / boot / recover use — ``list_in_flight`` and
    ``list_stale(now=...)`` — so there is no reimplemented lease-expiry math and
    the operator's numbers match the store work is actually dispatched to.
    """

    if not path.exists():
        return _unavailable_tasks(path), ()

    try:
        store = SqliteTaskSagaStore(path)
        try:
            in_flight = store.list_in_flight()
            stale = store.list_stale(now=now)
        finally:
            store.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        status = _unavailable_tasks(path)
        status["error"] = str(exc)
        return status, ()

    # Boot summary consistency: the ids fed into ``_boot_status`` are the saga
    # task ids (``saga.task_id``), matching ``assemble_boot_context``.
    in_flight_task_ids = tuple(saga.task_id for saga in in_flight)

    return (
        {
            "available": True,
            "path": str(path),
            "in_flight_count": len(in_flight),
            "stale_lease_count": len(stale),
        },
        in_flight_task_ids,
    )


def _unavailable_tasks(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "in_flight_count": None,
        "stale_lease_count": None,
    }


def _boot_status(
    *,
    event_log_available: bool,
    frontier_cursor: FrontierCursor | None,
    memory_available: bool,
    tasks_available: bool,
    active_memory_ids: tuple[str, ...],
    in_flight_task_ids: tuple[str, ...],
    last_sequence: int | None,
) -> dict[str, Any]:
    if (
        not event_log_available
        or frontier_cursor is None
        or not memory_available
        or not tasks_available
        or last_sequence is None
    ):
        return {"available": False, "summary": None}

    context = BootContext(
        frontier=frontier_cursor,
        active_memory_ids=active_memory_ids,
        in_flight_task_ids=in_flight_task_ids,
        last_event_sequence=last_sequence,
    )
    return {"available": True, "summary": context.summary()}
