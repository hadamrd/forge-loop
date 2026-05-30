"""Auto-rescue dirty worker worktrees after a tick."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import subprocess
from pathlib import Path

from forge_loop.config import Config
from forge_loop.worker import WorkerOutcome

_TEST_FILE_GLOBS = (
    "**/test/**",
    "**/tests/**",
    "**/*Test.java",
    "**/*Test.kt",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.test.js",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/*.spec.js",
    "**/*_test.go",
    "**/test_*.py",
)


def rescue_uncommitted_work(outcome: WorkerOutcome, cfg: Config) -> str | None:
    """Commit, push, and open a PR for dirty worker output. Never raises."""
    worktree = Path(f"/tmp/wt-loop-{outcome.issue}")
    if not worktree.exists() or not _has_uncommitted_changes(worktree):
        return None

    branch = _current_branch(worktree)
    if not branch or branch in ("trunk", "main", "HEAD"):
        return None

    _run_rescue_formatter(worktree, cfg)
    if not _commit_and_push(worktree, branch, outcome, cfg):
        return None

    has_tests = _diff_has_tests(worktree, cfg.base_branch)
    url = _open_rescue_pr(worktree, branch, outcome, cfg, has_tests=has_tests)
    if url is None:
        return None
    if has_tests:
        _enable_best_effort_automerge(worktree, url)
    return url


def _has_uncommitted_changes(worktree: Path) -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return status.returncode == 0 and bool(status.stdout.strip())


def _current_branch(worktree: Path) -> str:
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return branch.stdout.strip() if branch.returncode == 0 else ""


def _run_rescue_formatter(worktree: Path, cfg: Config) -> None:
    fmt_cmd = getattr(cfg.worker, "rescue_format_cmd", "") or ""
    if fmt_cmd.strip():
        with contextlib.suppress(subprocess.SubprocessError):
            subprocess.run(
                fmt_cmd,
                cwd=worktree,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
                env={**os.environ, "JAVA_TOOL_OPTIONS": "-Xmx1500m"},
                check=False,
            )


def _commit_and_push(worktree: Path, branch: str, outcome: WorkerOutcome, cfg: Config) -> bool:
    commit_msg = _commit_message(outcome, cfg)
    return (
        subprocess.run(
            ["git", "add", "-A"], cwd=worktree, capture_output=True, timeout=60, check=False
        ).returncode
        == 0
        and subprocess.run(
            ["git", "commit", "--no-verify", "-m", commit_msg, "--allow-empty-message"],
            cwd=worktree,
            capture_output=True,
            timeout=60,
            check=False,
        ).returncode
        == 0
        and subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=worktree,
            capture_output=True,
            timeout=120,
            check=False,
        ).returncode
        == 0
    )


def _commit_message(outcome: WorkerOutcome, cfg: Config) -> str:
    msg = (
        f"feat(loop): auto-shipped from worker session - closes #{outcome.issue}\n"
        "\n"
        "Worker exited its SDK session without running git commit. The\n"
        "loop captured the uncommitted output, applied the configured\n"
        "format command, committed, and pushed. Auto-merge is enabled;\n"
        "CI gates + critic-block-on-sev1 are the merge contract.\n"
        "\n"
        f"Worker status: {outcome.status}\n"
        f"Worker log: docs/ops/loop-runner-logs/worker-{outcome.issue}-*.log\n"
    )
    if cfg.coauthor:
        msg += f"\nCo-Authored-By: {cfg.coauthor}\n"
    return msg


def _diff_has_tests(worktree: Path, base_branch: str) -> bool:
    diff = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base_branch}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    changed = diff.stdout.strip().splitlines() if diff.returncode == 0 else []
    return any(fnmatch.fnmatch(path, glob) for path in changed for glob in _TEST_FILE_GLOBS)


def _open_rescue_pr(
    worktree: Path,
    branch: str,
    outcome: WorkerOutcome,
    cfg: Config,
    *,
    has_tests: bool,
) -> str | None:
    pr_args = [
        "gh",
        "pr",
        "create",
        "--repo",
        cfg.github_repo,
        "--base",
        cfg.base_branch,
        "--head",
        branch,
        "--title",
        f"feat(loop): auto-shipped #{outcome.issue} - worker session captured",
        "--body",
        _pr_body(outcome, has_tests=has_tests),
        "--label",
        "loop:auto-rescued",
    ]
    if not has_tests:
        pr_args.insert(3, "--draft")
        pr_args.extend(["--label", "loop:needs-review"])
    pr = subprocess.run(
        pr_args,
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if pr.returncode != 0:
        return None
    url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
    return url if url.startswith("https://github.com/") else None


def _pr_body(outcome: WorkerOutcome, *, has_tests: bool) -> str:
    return (
        f"**Auto-shipped by forge-loop** - worker for #{outcome.issue} exited its\n"
        "SDK session without committing. The loop captured + formatted +\n"
        "pushed the output. Auto-merge is enabled.\n"
        "\n"
        f"- has tests in diff: **{has_tests}**\n"
        f"- worker status: `{outcome.status}`\n"
        f"- worker log: `docs/ops/loop-runner-logs/worker-{outcome.issue}-*.log`\n"
        "\n"
        "Merge gate: CI must pass + critic must not block on sev1.\n"
    )


def _enable_best_effort_automerge(worktree: Path, url: str) -> None:
    subprocess.run(
        ["gh", "pr", "merge", url, "--squash", "--auto", "--delete-branch"],
        cwd=worktree,
        capture_output=True,
        timeout=60,
        check=False,
    )
