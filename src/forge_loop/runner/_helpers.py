"""Pure helpers extracted from the runner package.

Everything in this module is self-contained: no module-level state, no
imports from ``forge_loop.runner`` itself. The runner facade re-exports
every public name from here so existing imports of
``forge_loop.runner._reap_worktree`` etc. keep working unchanged.

Grouped by purpose:
  * boot helpers — orphan worktree reaper, version reader
  * drift helpers — error-signature classifier + deploy-fail scanner
  * worktree reaper — per-issue cleanup used both at boot and post-tick
  * force-retry token — operator-side bypass for the fingerprint cooldown
"""

from __future__ import annotations

import glob
import json
import subprocess
from pathlib import Path

from forge_loop.state import append_event, rotate_events_file_if_needed

# ---------------------------------------------------------------------------
# Events log rotation
# ---------------------------------------------------------------------------


def rotate_events_file_at_boot(events_file: Path) -> dict | None:
    """Boot-time wrapper around :func:`forge_loop.state.rotate_events_file_if_needed`.

    Centralises the call so the runner ``boot`` module stays a thin facade
    and tests can patch this single entry point.

    Returns whatever the underlying helper returns (``None`` if no rotation
    was needed). The helper is best-effort: an OSError never escapes — it
    is recorded as an ``events_rotation_failed`` event in the events file
    and reported via the return value.
    """
    return rotate_events_file_if_needed(events_file)


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


def reap_worktree(repo: Path, issue: int) -> None:
    """Force-remove a worker's worktree after success. Best-effort."""
    wt = Path(f"/tmp/wt-loop-{issue}")
    if not wt.exists():
        return
    # The planted .claude/ is locked read-only (chmod 555). Unlock first.
    claude_dir = wt / ".claude"
    if claude_dir.exists():
        subprocess.run(
            ["chmod", "-R", "u+w", str(claude_dir)],
            capture_output=True,
        )
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=repo, capture_output=True,
    )


def reap_orphan_worktrees(repo: Path, events_file: Path) -> int:
    """Boot-time cleanup of stale /tmp/wt-loop-* worktrees.

    Any /tmp/wt-loop-* on disk at boot is by definition stale — the loop
    is the sole owner of those paths and we haven't started any worker
    yet. Returns the count reaped (for telemetry).
    """
    reaped = 0
    for path in sorted(glob.glob("/tmp/wt-loop-*")):
        name = Path(path).name
        # Quarantined dirs (wt-loop-<N>.stale-<ts>) from failed cleanups —
        # try to rm them at boot. If still un-removable due to uid
        # mismatch, leave them; operator sweep can take over.
        if ".stale-" in name:
            try:
                import shutil
                shutil.rmtree(path, ignore_errors=True)
            except Exception:  # noqa: BLE001 — best-effort boot cleanup
                pass
            if not Path(path).exists():
                reaped += 1
            continue
        try:
            issue = int(name.removeprefix("wt-loop-").split("-")[0])
        except ValueError:
            # Defensive: skip non-numeric suffixes without crashing the
            # boot path. A future operator tool might leave a marker dir
            # under this prefix; the runner must keep going.
            continue
        reap_worktree(repo, issue)
        if not Path(path).exists():
            reaped += 1
    if reaped:
        append_event(events_file, "orphan_worktrees_reaped", count=reaped)
    # Prune git-internal worktree entries that no longer have a backing
    # dir (accumulate when a worktree is rm -rf'd without `git worktree
    # remove`).
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo, capture_output=True,
    )
    return reaped


# ---------------------------------------------------------------------------
# Version detection (self-upgrade restart)
# ---------------------------------------------------------------------------


def installed_version() -> str:
    """Best-effort read of the installed forge-loop version.

    Returns the empty string on failure — a missing version is treated as
    "unchanged" so the runner won't spuriously restart itself.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("forge-loop")
    except (ImportError, PackageNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# Drift classification
# ---------------------------------------------------------------------------


def error_signature(outcome_error: str | None, stdout_tail: str) -> str:
    """Reduce a failure to a stable signature for drift detection.

    Looks for known terminal patterns; falls back to the first 60 chars of
    the error message. Used by the 3-consecutive-tick drift detector to
    decide whether the loop should halt.
    """
    blob = f"{outcome_error or ''} {stdout_tail or ''}"[:2000].lower()
    patterns = [
        ("exit code 137", "oom-exit-137"),
        ("watchdog_worker_killed", "watchdog-kill"),
        ("worker exceeded", "wall-timeout"),
        ("worktree-create-failed", "worktree-create-fail"),
        ("gh_list_failed", "gh-list-fail"),
        ("permission denied", "permission-denied"),
        ("rate limit", "rate-limit"),
    ]
    for needle, tag in patterns:
        if needle in blob:
            return tag
    if outcome_error:
        return f"err:{outcome_error[:60].strip().lower()}"
    return "unknown"


def consecutive_deploy_fails(events_file: Path) -> int:
    """Best-effort scan of the tail of events for consecutive failed redeploys."""
    if not events_file.exists():
        return 0
    try:
        with open(events_file) as f:
            lines = f.readlines()[-200:]
    except OSError:
        return 0
    count = 0
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") != "redeploy":
            continue
        if e.get("ok"):
            break
        count += 1
        if count >= 5:  # cap scan
            break
    return count


# ---------------------------------------------------------------------------
# Force-retry token (operator-side bypass for the fingerprint cooldown)
# ---------------------------------------------------------------------------


def force_retry_file(state_dir: Path) -> Path:
    return state_dir / "loop-runner.force-retry.json"


def consume_force_set(state_dir: Path) -> set[int]:
    """Read & clear the force-retry marker. Issues listed here bypass
    fingerprint guards exactly once. Written by ``forge-loop retry --force``.
    """
    path = force_retry_file(state_dir)
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return set()
    nums: set[int] = set()
    for n in payload.get("issues") or []:
        try:
            nums.add(int(n))
        except (TypeError, ValueError):
            continue
    path.unlink(missing_ok=True)
    return nums
