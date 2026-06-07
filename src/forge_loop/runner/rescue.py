"""Auto-rescue dirty worker worktrees after a tick."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import subprocess
from pathlib import Path

from forge_loop.config import Config
from forge_loop.state import append_event
from forge_loop.worker import WorkerOutcome

# Loop-planted infrastructure file (see worker_brief.py "DO NOT TOUCH" note).
# Worker SDK sessions mutate it as a pure side-effect (permissions cache), so a
# worktree can read as dirty with zero real product changes. Auto-rescue must
# neither ship a PR for a settings-only diff nor include this file in a mixed
# rescue commit (issue #366).
_PLANTED_SETTINGS_PATH = ".claude/settings.json"

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
    from forge_loop.worker_worktree import worktree_path

    worktree = worktree_path(cfg.repo, outcome.issue)
    if not worktree.exists():
        return None

    dirty = _dirty_paths(worktree)
    if not dirty:
        return None
    if _diff_is_settings_only(dirty):
        # Only the loop-planted settings file changed — no product value to
        # ship. Skip loudly so operators see *why* no PR appeared (#366).
        append_event(cfg.events_file, "rescue_skipped_settings_only", issue=outcome.issue)
        return None

    branch = _current_branch(worktree)
    if not branch or branch in ("trunk", "main", "HEAD"):
        return None

    _run_rescue_formatter(worktree, cfg)
    if not _commit_and_push(worktree, branch, outcome, cfg):
        return None

    has_tests = _diff_has_tests(worktree, cfg.base_branch)
    url = _open_rescue_pr(branch, outcome, cfg, has_tests=has_tests)
    if url is None:
        return None
    if has_tests:
        _enable_best_effort_automerge(url, cfg)
    return url


def _dirty_paths(worktree: Path) -> list[str]:
    """Worktree-relative paths reported dirty by ``git status --porcelain``.

    Covers untracked (``??``), modified (`` M``) and staged (``M ``) entries.
    The porcelain v1 line is ``XY <path>`` (two status columns + a space);
    renames render ``XY orig -> new`` — we keep the post-rename path.

    ``--untracked-files=all`` lists individual untracked files rather than
    collapsing a wholly-untracked directory to ``dir/`` — without it a freshly
    untracked ``.claude/settings.json`` would surface as ``.claude/`` and dodge
    the settings-only guard. The diff is a single small worktree, so the
    large-repo ``-uall`` caveat does not apply here.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if status.returncode != 0:
        return []
    paths: list[str] = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip())
    return paths


def _diff_is_settings_only(dirty: list[str]) -> bool:
    """True iff the *only* dirty path is the loop-planted settings file.

    Exact-path match (not substring/glob), so siblings like
    ``.claude/settings.json.bak`` or ``src/app/settings.json`` are treated as
    real changes and still get rescued (#366 over-eager-match guard)."""
    return bool(dirty) and all(p == _PLANTED_SETTINGS_PATH for p in dirty)


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
    if (
        subprocess.run(
            ["git", "add", "-A"], cwd=worktree, capture_output=True, timeout=60, check=False
        ).returncode
        != 0
    ):
        return False
    # Never ship the loop-planted settings file: unstage it so the mixed-diff
    # rescue commit carries only real worker output (#366). Best-effort — if the
    # path was never staged, ``git reset`` is a harmless no-op.
    subprocess.run(
        ["git", "reset", "-q", "--", _PLANTED_SETTINGS_PATH],
        cwd=worktree,
        capture_output=True,
        timeout=60,
        check=False,
    )
    return (
        subprocess.run(
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
    branch: str,
    outcome: WorkerOutcome,
    cfg: Config,
    *,
    has_tests: bool,
) -> str | None:
    if not cfg.github_repo:
        return None
    from forge_loop import gh_issues as _gh

    labels = ["loop:auto-rescued"]
    if not has_tests:
        labels.append("loop:needs-review")
    try:
        url = _gh.create_pull(
            f"feat(loop): auto-shipped #{outcome.issue} - worker session captured",
            _pr_body(outcome, has_tests=has_tests),
            branch,
            cfg.base_branch,
            cfg.github_repo,
            draft=not has_tests,
        )
    except Exception:  # noqa: BLE001 — rescue is best-effort; never raise on PR open
        return None
    if not url.startswith("https://github.com/"):
        return None
    # Labels are a second call (REST creates PRs without labels). Best-effort:
    # a labelling hiccup must not lose the rescued PR.
    _gh.add_pr_label(url, labels, repo=cfg.github_repo)
    return url


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


def _enable_best_effort_automerge(url: str, cfg: Config) -> None:
    if not cfg.github_repo:
        return
    from forge_loop import gh_issues as _gh

    with contextlib.suppress(Exception):
        _gh.enable_pr_auto_merge(url, repo=cfg.github_repo)
