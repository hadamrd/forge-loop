"""One-call ``what is the loop doing`` snapshot (issue #64).

Today, an operator (or LLM agent) debugging the loop has to issue 4-5
separate MCP calls to assemble a coherent picture: ``loop_status`` for
tick + state, ``events_recent`` for the recent event stream,
``gh pr list`` for open PRs, ``ls /tmp/wt-loop-*`` for in-flight
worktrees, and sometimes ``attempts_history`` per worker. Every debug
session reassembles the same view.

This module produces a single flat dict containing all of the above,
designed for an LLM to consume in one MCP round-trip. It deliberately
omits SDK log content (use ``worker_logs`` for that — separate tool) so
the payload stays small enough to fit in a system message comfortably.

The snapshot is *per-loop-instance* — multi-repo aggregation is out of
scope (see the issue body). One snapshot reflects one ``Config``.

Design contract:
- All external dependencies (gh CLI, /tmp glob, file system) are
  injectable as keyword arguments so unit tests don't need network or
  a real loop install.
- Every field has a sensible default for the brand-new / empty case —
  callers can rely on the dict shape even before the loop has emitted
  its first event.
- Per-section failures (e.g. ``gh pr list`` returns nonzero) are
  swallowed and surfaced as empty lists, with the failure note attached
  to the relevant key so the operator sees why.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forge_loop.config import Config

# Set of event kinds considered "terminal" for a worker — receiving any
# of these clears the issue from the in-flight set. Kept in lockstep with
# ``cli_tui._compute_inflight`` so the dashboard / CLI / snapshot all
# agree on what "still running" means.
_TERMINAL_KINDS: frozenset[str] = frozenset({
    "worker_done",
    "worker_failed",
    "worker_skip_in_flight",
    "worker_skip_cooldown",
    "budget_worker_killed",
    "watchdog_worker_killed",
    "worker_completed",
    "worker_merged",
})

_HALT_FILENAME = "loop-runner.HALT"

# Per-issue worktree directory shape: ``<worktree_root>/wt-loop-<n>``.
_WORKTREE_PREFIX = "wt-loop-"

# Default config-knob fallbacks: snapshots are best-effort introspection,
# so a missing ``worktree_root`` falls back to /tmp (matching the
# bundled init.py default).
_DEFAULT_WORKTREE_ROOT = Path("/tmp")


# ---------------------------------------------------------------------------
# Type aliases for the injectable seams
# ---------------------------------------------------------------------------
QueueDepthFn = Callable[[str, str], int]  # (repo, label) -> count
OpenPRsFn = Callable[[str], list[dict[str, Any]]]  # (repo) -> [{number,title,branch}]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_snapshot(
    cfg: Config,
    since_minutes: int = 15,
    *,
    now: datetime | None = None,
    queue_depth_fn: QueueDepthFn | None = None,
    open_prs_fn: OpenPRsFn | None = None,
    worktree_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble the one-call introspection dict for ``cfg``.

    Args:
        cfg: Loaded loop config — drives every path the snapshot reads.
        since_minutes: Time window for event aggregations + last-drift
            lookup. Defaults to 15 minutes (matches the issue body).
        now: Optional override for "current time" — tests pin this so
            ``last_event_age_s`` is deterministic.
        queue_depth_fn: Optional override for the gh query that counts
            ``loop:ready`` open issues. Defaults to a shell-out to the
            real ``gh`` CLI.
        open_prs_fn: Optional override for ``gh pr list``. Defaults to
            the real CLI shell-out.
        worktree_root: Override for ``/tmp`` (or ``cfg.worktree_root``).

    Returns:
        Flat dict with every key the issue body lists. Always returns —
        per-section failures are swallowed; check the ``_errors`` key
        for diagnostic notes.
    """
    if since_minutes <= 0:
        since_minutes = 15
    now = now or datetime.now(UTC)
    wt_root_value = worktree_root or getattr(cfg, "worktree_root", _DEFAULT_WORKTREE_ROOT)
    wt_root = wt_root_value if isinstance(wt_root_value, Path) else _DEFAULT_WORKTREE_ROOT
    errors: dict[str, str] = {}

    # ── 1. State file: tick, state, runner_id ────────────────────────────
    state_dict, state_err = _read_state(cfg.state_file)
    if state_err:
        errors["state"] = state_err
    state_label = state_dict.get("state", "uninitialised")
    tick = int(state_dict.get("tick", 0))
    runner_id = _extract_runner_id(state_dict, cfg.events_file)

    # ── 2. Queue depth (loop:ready issues) ───────────────────────────────
    label = cfg.labels.ready
    repo = cfg.github_repo or ""
    if queue_depth_fn is None:
        queue_depth_fn = _default_queue_depth
    try:
        queue_depth = int(queue_depth_fn(repo, label)) if repo else 0
    except Exception as exc:  # noqa: BLE001
        queue_depth = 0
        errors["queue_depth"] = f"{type(exc).__name__}: {exc}"[:200]

    # ── 3+4. In-flight workers + recent_kinds (single events read) ───────
    # We read the events file once and derive every event-driven section
    # from the in-memory list. This avoids invoking DuckDB twice (cheap
    # but adds dependency latency) and lets ``now`` be injected for
    # deterministic windowing in tests.
    events_in_window, events_extended = _load_events(
        cfg.events_file, now=now, since_minutes=since_minutes,
    )
    in_flight = _compute_in_flight(
        events_extended, wt_root=wt_root, now=now, cfg=cfg,
    )
    recent_kinds: dict[str, int] = {}
    for ev in events_in_window:
        kind = ev.get("kind")
        if kind:
            recent_kinds[str(kind)] = recent_kinds.get(str(kind), 0) + 1

    # ── 5. Open PRs ──────────────────────────────────────────────────────
    if open_prs_fn is None:
        open_prs_fn = _default_open_prs
    try:
        open_prs = open_prs_fn(repo) if repo else []
    except Exception as exc:  # noqa: BLE001
        open_prs = []
        errors["open_prs"] = f"{type(exc).__name__}: {exc}"[:200]

    # ── 6. Halt marker ───────────────────────────────────────────────────
    halt_marker = _halt_marker_info(cfg.state_dir / _HALT_FILENAME, now=now)

    # ── 7. Last drift event in window ────────────────────────────────────
    last_drift = _last_drift_event(events_in_window)

    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "ts": now.isoformat(timespec="seconds"),
        "since_minutes": since_minutes,
        "tick": tick,
        "state": state_label,
        "runner_id": runner_id,
        "queue_depth": queue_depth,
        "queue_label": label,
        "in_flight": in_flight,
        "in_flight_count": len(in_flight),
        "recent_kinds": recent_kinds,
        "open_prs": open_prs,
        "halt_marker": halt_marker,
        "last_drift_event": last_drift,
    }
    if errors:
        snapshot["_errors"] = errors
    return snapshot


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _read_state(state_file: Path) -> tuple[dict[str, Any], str | None]:
    if not state_file.exists():
        return {}, None
    try:
        raw = state_file.read_text()
        data = json.loads(raw)
        return (data if isinstance(data, dict) else {}), None
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"[:200]


def _extract_runner_id(state_dict: dict[str, Any], events_file: Path) -> str | None:
    """``runner_id`` is emitted on the ``loop_start`` event but isn't in
    the state file. We search the events log (last ~200 lines) for the
    most recent ``loop_start`` event and pull its ``runner_id``.

    Falls back to ``state.runner_id`` if the state file carries it (it
    doesn't today, but newer versions may); else None.
    """
    if rid := state_dict.get("runner_id"):
        return str(rid)
    if not events_file.exists():
        return None
    try:
        # Read tail of the file so we don't load 10MB into memory.
        tail = events_file.read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return None
    for raw in reversed(tail):
        raw = raw.strip()
        if not raw or "loop_start" not in raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("kind") == "loop_start" and (rid := ev.get("runner_id")):
            return str(rid)
    return None


def _load_events(
    events_file: Path,
    *,
    now: datetime,
    since_minutes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(events_in_window, events_extended)`` for this snapshot.

    ``events_in_window`` is the slice strictly within
    ``[now - since_minutes, now]`` — used for ``recent_kinds`` and the
    drift lookup. ``events_extended`` widens the lookback so workers
    that started just before the window are still seen as in-flight.

    Reads the raw JSONL file (tail-limited to 2000 lines for sanity);
    we deliberately bypass DuckDB so the caller's injected ``now`` is
    honoured.
    """
    if not events_file.exists():
        return [], []
    try:
        lines = events_file.read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        return [], []
    parsed: list[dict[str, Any]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            parsed.append(ev)

    window_start = now - _td_minutes(since_minutes)
    extended_start = now - _td_minutes(max(since_minutes * 4, since_minutes + 30))
    in_window: list[dict[str, Any]] = []
    extended: list[dict[str, Any]] = []
    for ev in parsed:
        when = _parse_iso(ev.get("ts"))
        if when is None:
            # Keep events with no/parseless ts in the extended slice
            # (defensive: never silently drop in-flight workers).
            extended.append(ev)
            continue
        if when >= extended_start:
            extended.append(ev)
        if when >= window_start:
            in_window.append(ev)
    return in_window, extended


def _td_minutes(m: int) -> timedelta:
    return timedelta(minutes=int(m))


def _parse_iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when


def _compute_in_flight(
    events: list[dict[str, Any]],
    *,
    wt_root: Path,
    now: datetime,
    cfg: Config,
) -> list[dict[str, Any]]:
    """Derive currently in-flight workers from a pre-loaded event slice.

    Tracks ``worker_start`` → terminal-kind pairs by issue number to
    identify open workers, mirroring ``cli_tui._compute_inflight``.
    For each open worker, enriches with the worktree path (if present
    on disk), the branch (best-effort: parsed from the dir's HEAD
    file), the last event's age in seconds, and the SDK log file size
    if a per-issue log file exists.
    """
    open_by_issue: dict[int, dict[str, Any]] = {}
    for ev in events:
        issue = ev.get("issue") or ev.get("issue_number")
        if not isinstance(issue, int):
            with contextlib.suppress(TypeError, ValueError):
                issue = int(issue)  # type: ignore[arg-type]
        if not isinstance(issue, int):
            continue
        kind = ev.get("kind", "")
        if kind == "worker_start":
            open_by_issue[issue] = {
                "issue": issue,
                "started_ts": ev.get("ts"),
                "last_event_ts": ev.get("ts"),
            }
        elif kind in _TERMINAL_KINDS:
            open_by_issue.pop(issue, None)
        elif issue in open_by_issue:
            # Touch last_event_ts for any subsequent event on this issue.
            open_by_issue[issue]["last_event_ts"] = ev.get("ts")

    in_flight: list[dict[str, Any]] = []
    for issue, entry in sorted(open_by_issue.items()):
        worktree = wt_root / f"{_WORKTREE_PREFIX}{issue}"
        branch = _read_worktree_branch(worktree) if worktree.exists() else None
        sdk_size = _sdk_log_size(cfg.logs_dir, issue)
        last_age = _age_seconds(entry.get("last_event_ts"), now=now)
        in_flight.append({
            "issue": issue,
            "branch": branch,
            "worktree": str(worktree) if worktree.exists() else None,
            "started_ts": entry.get("started_ts"),
            "last_event_ts": entry.get("last_event_ts"),
            "last_event_age_s": last_age,
            "sdk_log_size": sdk_size,
        })

    return in_flight


def _read_worktree_branch(worktree: Path) -> str | None:
    """Read ``<worktree>/.git/HEAD`` (or the .git file pointer for a worktree)
    and return the branch name if it's a symbolic ref.
    """
    head_paths: list[Path] = [worktree / ".git" / "HEAD", worktree / ".git"]
    for head in head_paths:
        if not head.exists():
            continue
        try:
            content = head.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        # A worktree's ``.git`` is a file containing ``gitdir: <path>``.
        if content.startswith("gitdir:"):
            gitdir = Path(content.split(":", 1)[1].strip())
            head2 = gitdir / "HEAD"
            if head2.exists():
                with contextlib.suppress(OSError):
                    content = head2.read_text(encoding="utf-8", errors="replace").strip()
        if content.startswith("ref: refs/heads/"):
            return content[len("ref: refs/heads/"):]
        if content:
            # Detached HEAD — return the short sha.
            return content[:12]
    return None


def _sdk_log_size(logs_dir: Path, issue: int) -> int:
    """Sum the bytes of any ``worker-<issue>-*.log`` under ``logs_dir``."""
    if not logs_dir.exists():
        return 0
    total = 0
    with contextlib.suppress(OSError):
        for p in logs_dir.iterdir():
            name = p.name
            if not name.startswith("worker-"):
                continue
            # Strip "worker-" prefix; match issue at the head.
            rest = name[len("worker-"):]
            if rest.startswith(f"{issue}-") or rest == f"{issue}.log":
                with contextlib.suppress(OSError):
                    total += p.stat().st_size
    return total


def _age_seconds(ts: str | None, *, now: datetime) -> int | None:
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (now - when).total_seconds()
    return max(0, int(delta))


def _halt_marker_info(halt_path: Path, *, now: datetime) -> dict[str, Any]:
    if not halt_path.exists():
        return {"present": False, "path": str(halt_path), "age_s": None, "reason": None}
    age_s: int | None = None
    reason: str | None = None
    try:
        st = halt_path.stat()
        age_s = max(0, int(now.timestamp() - st.st_mtime))
    except OSError:
        pass
    with contextlib.suppress(OSError):
        reason = halt_path.read_text(encoding="utf-8", errors="replace").strip()[:500] or None
    return {"present": True, "path": str(halt_path), "age_s": age_s, "reason": reason}


def _last_drift_event(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most-recent event whose ``kind`` matches ``*_drift_*``
    (or contains ``drift``). Events are assumed oldest-first (as
    ``_eventdb.recent`` returns them); we scan in reverse.
    """
    for ev in reversed(list(events)):
        kind = str(ev.get("kind", ""))
        if "drift" in kind:
            return ev
    return None


# ---------------------------------------------------------------------------
# Default gh CLI shell-outs (overridable in tests)
# ---------------------------------------------------------------------------
def _default_queue_depth(repo: str, label: str) -> int:
    """Count open issues carrying ``label`` via ``gh issue list``.

    Uses ``--json number`` + ``jq``-free parsing. ``gh`` paginates to
    1000 by default; we explicitly bump ``--limit 200`` because the
    snapshot is a hot-path read and the queue is rarely larger.
    """
    if not repo:
        return 0
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--label", label,
        "--limit", "200",
        "--json", "number",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
    if r.returncode != 0:
        return 0
    try:
        rows = json.loads(r.stdout)
        return len(rows) if isinstance(rows, list) else 0
    except json.JSONDecodeError:
        return 0


def _default_open_prs(repo: str) -> list[dict[str, Any]]:
    """List open PRs in ``repo`` as ``[{number, title, branch}, ...]``."""
    if not repo:
        return []
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", "50",
        "--json", "number,title,headRefName",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
    if r.returncode != 0:
        return []
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        out.append({
            "number": int(row.get("number", 0)),
            "title": str(row.get("title", "")),
            "branch": str(row.get("headRefName", "")),
        })
    return out


# Kept for callers that prefer ``time.time()`` over ``datetime``.
def _epoch_now() -> float:
    return time.time()
