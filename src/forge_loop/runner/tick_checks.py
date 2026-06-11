"""Best-effort per-tick checks that run beside worker dispatch."""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from forge_loop.branch_sweep import BranchSweepReport
from forge_loop.branch_sweep import GhClientLike as BranchGhClientLike
from forge_loop.branch_sweep import sweep as _branch_sweep
from forge_loop.checkout_reconcile import CheckoutReconcileReport, ReconcileOutcome
from forge_loop.checkout_reconcile import reconcile as _reconcile
from forge_loop.config import Config
from forge_loop.epic_sweep import EpicSweepReport
from forge_loop.epic_sweep import GhClientLike as EpicGhClientLike
from forge_loop.epic_sweep import sweep as _epic_sweep
from forge_loop.maintenance import run_maintenance
from forge_loop.state import append_event, write_state
from forge_loop.stuck_sweep import SweepReport
from forge_loop.stuck_sweep import sweep as _stuck_sweep
from forge_loop.worktree_sweep import WorktreeSweepReport, _under_root
from forge_loop.worktree_sweep import sweep_roots as _worktree_sweep


def run_stuck_sweep(cfg: Config, tick: int) -> SweepReport | None:
    """Demote issues that repeatedly failed to leave the ready queue."""
    if cfg.github_repo is None or "/" not in cfg.github_repo:
        return None
    owner, repo = cfg.github_repo.split("/", 1)
    try:
        from forge_loop.gh_client import GithubkitClient

        client = GithubkitClient()
    except Exception as ex:  # noqa: BLE001
        append_event(
            cfg.events_file,
            "stuck_sweep_skipped",
            tick=tick,
            reason=f"gh_client_init: {ex}"[:200],
        )
        return None
    try:
        report = _stuck_sweep(
            cfg.events_file,
            client,
            owner=owner,
            repo=repo,
            threshold=cfg.stuck_threshold_attempts,
            ready_label=cfg.labels.ready,
            tail=cfg.stuck_tail_events,
        )
    except Exception as ex:  # noqa: BLE001
        append_event(cfg.events_file, "stuck_sweep_crashed", tick=tick, err=str(ex)[:200])
        return None
    if report.demotions:
        append_event(
            cfg.events_file,
            "stuck_sweep_done",
            tick=tick,
            demoted=[d.issue for d in report.demotions if d.ok],
            failed=list(report.errors),
            scanned=report.scanned,
        )
    return report


def run_epic_sweep(
    cfg: Config,
    tick: int,
    *,
    client: EpicGhClientLike | None = None,
    now: datetime | None = None,
) -> EpicSweepReport | None:
    """Auto-close resolved epics (#367) + expire stale undecomposed epics (#435).

    Runs on the maintenance cadence (``maintenance_every_n_ticks``) only — a
    no-op off-cadence (returns ``None`` without touching GitHub). Deterministic
    Python; spawns NO LLM subagent. Emits a typed ``epic_sweep_done`` summary
    event (carrying ``expired`` distinctly from ``closed``) and returns the
    report. ``client`` is injectable for tests; in production it is the real
    ``GithubkitClient``. ``now`` is injected for clock-free tests, defaulting to
    the current UTC instant; it + ``cfg.epic_ttl_days`` drive the TTL pass.
    """
    if cfg.maintenance_every_n_ticks <= 0 or tick % cfg.maintenance_every_n_ticks != 0:
        return None
    if cfg.github_repo is None or "/" not in cfg.github_repo:
        return None
    owner, repo = cfg.github_repo.split("/", 1)
    if client is None:
        try:
            from forge_loop.gh_client import GithubkitClient

            client = GithubkitClient()
        except Exception as ex:  # noqa: BLE001
            append_event(
                cfg.events_file,
                "epic_sweep_skipped",
                tick=tick,
                reason=f"gh_client_init: {ex}"[:200],
            )
            return None
    effective_now = now if now is not None else datetime.now(UTC)
    try:
        report = _epic_sweep(
            client,
            owner=owner,
            repo=repo,
            epic_label=cfg.epic_label,
            epic_ttl_days=cfg.epic_ttl_days,
            now=effective_now,
        )
    except Exception as ex:  # noqa: BLE001 — the sweep never raises, but belt-and-braces
        append_event(cfg.events_file, "epic_sweep_crashed", tick=tick, err=str(ex)[:200])
        return None

    from forge_loop.events import EpicSweepDoneEvent, emit

    emit(
        cfg.events_file,
        EpicSweepDoneEvent(
            tick=tick,
            closed=report.closed,
            expired=report.expired,
            skipped_open_subs=report.skipped_open_subs,
            skipped_no_subs=report.skipped_no_subs,
            errors=list(report.errors),
        ),
    )
    return report


def _list_remote_branches(repo: Path) -> list[str]:
    """Remote branch short-names via ``git ls-remote --heads origin`` — repo ops, not
    business GitHub logic. Returns [] on any failure (the sweep then no-ops)."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--heads", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    names: list[str] = []
    for line in out.splitlines():
        parts = line.split("\trefs/heads/", 1)
        if len(parts) == 2:
            names.append(parts[1].strip())
    return names


def run_branch_sweep(
    cfg: Config,
    tick: int,
    *,
    client: BranchGhClientLike | None = None,
    branch_lister: Callable[[], list[str]] | None = None,
) -> BranchSweepReport | None:
    """Delete ``loop/<n>`` branches whose issue is closed (operational-convergence axis).

    Maintenance-cadence only; deterministic Python, no LLM. Squash-merge severs git's
    own merged-signal, so issue-closed is the landed-signal. Conservative: only loop/<n>
    branches, never the base branch. ``client`` + ``branch_lister`` injectable for tests.
    """
    if cfg.maintenance_every_n_ticks <= 0 or tick % cfg.maintenance_every_n_ticks != 0:
        return None
    if cfg.github_repo is None or "/" not in cfg.github_repo:
        return None
    owner, repo = cfg.github_repo.split("/", 1)
    if client is None:
        try:
            from forge_loop.gh_client import GithubkitClient

            client = GithubkitClient()
        except Exception as ex:  # noqa: BLE001
            append_event(
                cfg.events_file, "branch_sweep_skipped", tick=tick, reason=f"gh_client_init: {ex}"[:200]
            )
            return None
    branches = branch_lister() if branch_lister is not None else _list_remote_branches(cfg.repo)
    protected = frozenset({cfg.base_branch, "trunk", "main", "master", "HEAD"})
    try:
        report = _branch_sweep(
            client, owner=owner, repo=repo, branch_names=branches, protected=protected
        )
    except Exception as ex:  # noqa: BLE001 — the sweep never raises; belt-and-braces
        append_event(cfg.events_file, "branch_sweep_crashed", tick=tick, err=str(ex)[:200])
        return None
    append_event(
        cfg.events_file,
        "branch_sweep_done",
        tick=tick,
        deleted=report.deleted,
        skipped_open=len(report.skipped_open),
        skipped_unknown=len(report.skipped_unknown),
        errors=list(report.errors),
    )
    return report


def restore_base_branch(
    repo: Path,
    base_branch: str,
    *,
    events_file: Path,
    tick: int,
) -> str:
    """Restore the shared/main checkout's HEAD to ``base_branch`` (issue #401).

    Operational-convergence axis: a dispatch tick must never leave the shared
    checkout at ``cfg.repo`` sitting on a worker/feature branch (a "HEAD-hop"),
    or the next ``git fetch origin <base>`` sync diffs against the wrong ref and
    ``forge-loop doctor`` reports spurious drift. Worker *worktrees* keep their
    own ``loop/<n>`` branches — this only touches the main checkout's HEAD and is
    a deliberate no-op when HEAD is already on ``base_branch`` (the common case).

    Reuses the ``_current_branch`` probe from ``runner/rescue.py`` (don't
    reinvent the ``rev-parse --abbrev-ref HEAD`` call). Mirrors the
    swallow-and-emit pattern of :func:`run_branch_sweep`: a checkout failure
    (dirty tree, missing/detached base) emits a best-effort
    ``base_branch_restore_failed`` event and returns without raising, so an
    in-tick restore hiccup never crashes the tick.

    Event convergence (issue #422): a *successful* restore emits the SAME typed
    :class:`forge_loop.events.CheckoutRestoredEvent` (``kind="checkout_restored"``,
    fields ``from_branch``/``to_branch``) as the maintenance-cadence
    :func:`run_checkout_reconcile` — a single, documented event name records a
    drifted-then-restored shared checkout from BOTH return arcs, never a third.
    The *failure* path keeps its own ``base_branch_restore_failed`` name on
    purpose: it is a distinct best-effort-degraded outcome (HEAD unreadable /
    checkout rejected / git hang) with no typed model, and the reconcile's
    failure events (``checkout_reconcile_*``) are likewise mechanism-specific;
    only the restored-observation is shared.

    Returns ``"noop"`` (HEAD already on base), ``"moved"`` (HEAD restored, one
    ``checkout_restored`` event emitted with ``from_branch``/``to_branch``),
    or ``"failed"`` (HEAD unreadable or checkout rejected).
    """
    from forge_loop.runner.rescue import _current_branch

    # Both git interactions below run inside _tick's finally-guard, so a raised
    # subprocess.TimeoutExpired / OSError (git hang, missing binary) would crash
    # the tick — and mask a body exception. check=False only suppresses non-zero
    # exit codes, not these. Swallow-and-emit, mirroring run_branch_sweep above.
    try:
        current = _current_branch(repo)
    except (subprocess.SubprocessError, OSError) as ex:
        append_event(
            events_file,
            "base_branch_restore_failed",
            tick=tick,
            reason=f"head_probe: {type(ex).__name__}"[:200],
        )
        return "failed"
    if not current:
        append_event(
            events_file, "base_branch_restore_failed", tick=tick, reason="head_unreadable"
        )
        return "failed"
    if current == base_branch:
        return "noop"
    try:
        result = subprocess.run(
            ["git", "checkout", base_branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as ex:
        append_event(
            events_file,
            "base_branch_restore_failed",
            tick=tick,
            from_branch=current,
            to_branch=base_branch,
            err=f"{type(ex).__name__}: {ex}"[:200],
        )
        return "failed"
    if result.returncode != 0:
        append_event(
            events_file,
            "base_branch_restore_failed",
            tick=tick,
            from_branch=current,
            to_branch=base_branch,
            err=(result.stderr or "").strip()[:200],
        )
        return "failed"
    from forge_loop.events import CheckoutRestoredEvent, emit

    emit(
        events_file,
        CheckoutRestoredEvent(tick=tick, from_branch=current, to_branch=base_branch),
    )
    return "moved"


def _checkout_is_dirty(repo: Path) -> bool:
    """True iff the shared checkout has ANY uncommitted change or untracked file.

    Reuses ``rescue._dirty_paths`` (``git status --porcelain --untracked-files=all``)
    so the porcelain parsing lives in one place. Conservative: any output ⇒ dirty ⇒
    the reconcile keeps its hands off (issue #416 never clobbers uncommitted work).
    Note we intentionally do NOT apply the settings-only exemption here — for the
    shared checkout, ANY dirtiness is a hands-off signal.
    """
    from forge_loop.runner.rescue import _dirty_paths

    return bool(_dirty_paths(repo))


def _switch_branch(repo: Path, branch: str) -> bool:
    """``git checkout <branch>`` in the shared checkout; True on success.

    Plain (non-``--force``) checkout — git itself refuses to switch when it would
    clobber local changes, a second belt under the explicit dirty-tree guard. A
    raised TimeoutExpired/OSError (git hang / missing binary) propagates to the
    caller's try/except, which records it as an ERROR outcome.
    """
    result = subprocess.run(
        ["git", "checkout", branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def run_checkout_reconcile(
    cfg: Config,
    tick: int,
    *,
    read_branch: Callable[[], str] | None = None,
    read_dirty: Callable[[], bool] | None = None,
    switch: Callable[[str], bool] | None = None,
) -> CheckoutReconcileReport | None:
    """Switch the shared checkout at ``cfg.repo`` back to ``base_branch`` (issue #416).

    Sibling to ``run_branch_sweep`` / ``run_worktree_sweep``: maintenance-cadence
    only (a no-op off-cadence, returning ``None``), deterministic Python, no LLM.
    Conservative by construction — switches ONLY when the checkout sits on a
    ``loop/<n>`` branch with a CLEAN tree; a dirty tree, an already-on-base checkout,
    or a non-loop branch is left untouched. Emits a typed ``checkout_restored`` event
    only when it actually moves HEAD; a dirty skip / error emits a best-effort skip
    event. Never raises into the tick (belt-and-braces ``noqa: BLE001`` swallow).

    The three git probes are injectable so the decision is unit-tested without real
    git; in production they shell out against ``cfg.repo``.
    """
    if cfg.maintenance_every_n_ticks <= 0 or tick % cfg.maintenance_every_n_ticks != 0:
        return None
    repo = cfg.repo
    rb = read_branch if read_branch is not None else (lambda: _current_branch(repo))
    rd = read_dirty if read_dirty is not None else (lambda: _checkout_is_dirty(repo))
    sw = switch if switch is not None else (lambda b: _switch_branch(repo, b))
    try:
        report = _reconcile(read_branch=rb, read_dirty=rd, switch=sw, base_branch=cfg.base_branch)
    except Exception as ex:  # noqa: BLE001 — reconcile never raises; belt-and-braces
        append_event(cfg.events_file, "checkout_reconcile_crashed", tick=tick, err=str(ex)[:200])
        return None
    if report.outcome is ReconcileOutcome.RESTORED:
        from forge_loop.events import CheckoutRestoredEvent, emit

        emit(
            cfg.events_file,
            CheckoutRestoredEvent(
                tick=tick,
                from_branch=report.from_branch or "",
                to_branch=report.to_branch or "",
            ),
        )
    elif report.outcome is ReconcileOutcome.SKIPPED_DIRTY:
        append_event(
            cfg.events_file,
            "checkout_reconcile_skipped",
            tick=tick,
            from_branch=report.from_branch,
            reason=report.reason or "dirty_tree",
        )
    elif report.outcome is ReconcileOutcome.ERROR:
        append_event(
            cfg.events_file,
            "checkout_reconcile_crashed",
            tick=tick,
            from_branch=report.from_branch,
            err=report.reason or "",
        )
    return report


def _current_branch(repo: Path) -> str:
    """Shared-checkout current branch via ``rescue._current_branch`` (no reinvent)."""
    from forge_loop.runner.rescue import _current_branch as _cb

    return _cb(repo)


def _worktree_porcelain(repo: Path) -> str:
    """Raw ``git worktree list --porcelain`` for THIS repo; "" on any failure."""
    try:
        return subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _parse_worktree_records(porcelain: str) -> list[tuple[str, bool, bool]]:
    """Parse porcelain into ``(path, locked, prunable)`` per worktree block. Git emits
    one block per worktree, fields one-per-line, blocks separated by a blank line; the
    optional ``locked`` / ``prunable`` markers are git's own liveness signal (#405)."""
    records: list[tuple[str, bool, bool]] = []
    path: str | None = None
    locked = prunable = False
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                records.append((path, locked, prunable))
            path, locked, prunable = line[len("worktree ") :].strip(), False, False
        elif line == "locked" or line.startswith("locked "):
            locked = True
        elif line == "prunable" or line.startswith("prunable "):
            prunable = True
    if path is not None:
        records.append((path, locked, prunable))
    return records


def _list_worktrees(repo: Path) -> list[str]:
    """Paths of every git worktree of THIS repo (`git worktree list --porcelain`).
    Scoped to forge-loop's own worktrees by git; [] on failure."""
    return [path for path, _locked, _prunable in _parse_worktree_records(_worktree_porcelain(repo))]


def _agent_root(repo: Path) -> Path:
    """Second GC root (#405): the harness's agent worktrees under the checkout."""
    return repo / ".claude" / "worktrees"


# Conservative age floor (#405): an intact, unlocked agent worktree is only reaped
# once it has been idle on disk longer than this. Git marks a worktree ``prunable``
# ONLY when its working dir is already gone — a crashed run that leaves an intact
# ``.claude/worktrees/wt-*`` dir behind is never prunable, so without this floor it
# would accrete forever. Anything younger (or whose mtime can't be read) is kept.
_AGENT_WORKTREE_MIN_AGE_S = 24 * 3600


def _path_age_s(path: str, now: float) -> float | None:
    """Seconds since ``path`` was last modified; ``None`` if its mtime can't be read
    (fail-safe on unknown — an unreadable age must never make a worktree reapable)."""
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def _agent_live_paths(
    repo: Path, *, min_age_s: float = _AGENT_WORKTREE_MIN_AGE_S, now: float | None = None
) -> set[str]:
    """Live ``.claude/worktrees/*`` agent worktrees (#405).

    The task-saga store knows nothing about agent worktrees, so liveness comes from
    git's own porcelain markers plus a conservative age floor:
      * ``locked`` ⇒ always LIVE (in use).
      * ``prunable`` and not locked ⇒ git-confirmed dead ⇒ reapable.
      * intact (no marker), unlocked ⇒ reapable ONLY once idle longer than
        ``min_age_s``; younger, or mtime unreadable ⇒ kept (fail-safe on unknown).
    This adds the positive reap path for intact-but-stale crashed agent worktrees,
    which git never marks prunable."""
    agent_root = str(_agent_root(repo))
    clock = time.time() if now is None else now
    live: set[str] = set()
    for path, locked, prunable in _parse_worktree_records(_worktree_porcelain(repo)):
        if not _under_root(path, agent_root):
            continue
        if locked:
            live.add(path)
            continue
        if prunable:
            continue  # git-confirmed dead → reapable
        age = _path_age_s(path, clock)
        if age is None or age < min_age_s:
            live.add(path)  # too young / unknown age → keep (fail-safe)
    return live


def _remove_worktree(repo: Path, path: str) -> bool:
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", path],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _inflight_worktrees(cfg: Config) -> set[str]:
    """Worktree paths the control plane still leases (authoritative tasks.db). A live
    lease's worktree is never reaped. Empty set on any error — but see the protected/
    root guards: an empty live-set still can't touch the main checkout or off-root dirs."""
    try:
        from forge_loop.control.boot import canonical_task_saga_path
        from forge_loop.tasks import SqliteTaskSagaStore

        path = canonical_task_saga_path(cfg.repo)
        if not path.exists():
            return set()
        store = SqliteTaskSagaStore(path)
        try:
            return {s.worktree for s in store.list_in_flight() if s.worktree}
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        return set()


_SYSTEM_ROOTS = {"/", "/tmp", "/var/tmp", "/home"}


def _clamp_overbroad_root(cfg: Config, root, tick: int):
    """#451 — never honor a worktree_root that is a SYSTEM directory.

    A live run configured ``worktree_root: /tmp``; the sweep then treated
    every /tmp worktree as loop-owned and reaped two OPERATOR worktrees
    (one seconds old, uncommitted work destroyed). Roots resolving to /,
    /tmp, /var/tmp, /home or $HOME are clamped to the loop's own per-repo
    namespace (``worktree_base(repo)``) and a typed event is emitted so
    the operator sees the config smell."""
    import os
    from pathlib import Path as _P

    from forge_loop.worker_worktree import worktree_base

    resolved = str(_P(root).resolve())
    home = os.path.expanduser("~")
    if resolved in _SYSTEM_ROOTS or resolved == home:
        clamped = worktree_base(cfg.repo)
        append_event(
            cfg.events_file,
            "worktree_root_clamped",
            tick=tick,
            configured=str(root),
            clamped_to=str(clamped),
        )
        return clamped
    return root


def run_worktree_sweep(
    cfg: Config,
    tick: int,
    *,
    worktrees: list[str] | None = None,
    live_paths: set[str] | None = None,
    remove: Callable[[str], bool] | None = None,
) -> WorktreeSweepReport | None:
    """Reap orphaned worktrees across BOTH GC roots (operational-convergence, #405).

    Maintenance-cadence only; deterministic, no LLM. Reconciles two disjoint roots in
    one pass: the loop's ``worktree_root`` (task worktrees, liveness = in-flight lease)
    and ``<repo>/.claude/worktrees`` (agent worktrees, liveness = git porcelain
    locked/prunable markers). Removes any worktree under either root that no live
    owner claims — never the main checkout, never a leased/locked worktree, fail-safe
    on unknown. Args injectable for tests; ``live_paths`` overrides BOTH liveness
    sources with a single combined set.
    """
    if cfg.maintenance_every_n_ticks <= 0 or tick % cfg.maintenance_every_n_ticks != 0:
        return None
    root = getattr(cfg, "worktree_root", None)
    if not root:
        return None
    root = _clamp_overbroad_root(cfg, root, tick)
    wts = worktrees if worktrees is not None else _list_worktrees(cfg.repo)
    live = (
        live_paths
        if live_paths is not None
        else (_inflight_worktrees(cfg) | _agent_live_paths(cfg.repo))
    )
    rm = remove if remove is not None else (lambda p: _remove_worktree(cfg.repo, p))
    protected = {str(cfg.repo)}
    roots = [str(root), str(_agent_root(cfg.repo))]
    try:
        report = _worktree_sweep(rm, wts, roots=roots, live_paths=live, protected=protected)
    except Exception as ex:  # noqa: BLE001 — the sweep never raises; belt-and-braces
        append_event(cfg.events_file, "worktree_sweep_crashed", tick=tick, err=str(ex)[:200])
        return None
    append_event(
        cfg.events_file,
        "worktree_sweep_done",
        tick=tick,
        reaped=report.reaped,
        kept_live=len(report.kept_live),
        errors=list(report.errors),
    )
    return report


def run_codebase_audit(cfg: Config, tick: int) -> None:
    """Emit codebase-audit drift events on the maintenance cadence."""
    try:
        from forge_loop.codebase_audit import audit
        from forge_loop.events import AuditCleanEvent, emit

        report = audit(cfg.repo)
    except Exception as ex:  # noqa: BLE001
        append_event(
            cfg.events_file,
            "audit_skipped",
            tick=tick,
            reason=f"{type(ex).__name__}: {ex}"[:200],
        )
        return
    if report.is_clean:
        with contextlib.suppress(Exception):
            emit(cfg.events_file, AuditCleanEvent(probes_run=list(report.probes_run)))
        return
    for violation in report.violations:
        append_event(
            cfg.events_file,
            "audit_violation_observed",
            tick=tick,
            probe=violation.probe,
            target=violation.target,
            severity=violation.severity,
            title=violation.title,
            metrics=violation.metrics,
        )
    for probe_name, err in report.errors.items():
        append_event(
            cfg.events_file,
            "audit_probe_crashed",
            tick=tick,
            probe=probe_name,
            err=err,
        )


def run_maintenance_tick(cfg: Config, tick: int) -> None:
    """Run the AI-as-PM maintenance branch and write its tick state."""
    write_state(cfg.state_file, {"state": "maintenance", "tick": tick})
    append_event(cfg.events_file, "maintenance_start", tick=tick)
    brief = cfg.briefs.maintenance
    outcome = (
        run_maintenance(cfg.repo, cfg.logs_dir, brief=brief)
        if brief
        else run_maintenance(cfg.repo, cfg.logs_dir)
    )
    append_event(
        cfg.events_file,
        "maintenance_done",
        tick=tick,
        acted_on=outcome.acted_on,
        added_ready=outcome.added_ready,
        closed_dupes=outcome.closed_dupes,
        retitled=outcome.retitled,
        duration_s=round(outcome.duration_s, 1),
    )
    write_state(
        cfg.state_file,
        {
            "state": "between-ticks",
            "tick": tick,
            "last_maintenance": {
                "acted_on": outcome.acted_on,
                "added_ready": outcome.added_ready,
            },
        },
    )
