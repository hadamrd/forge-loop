"""Project scaffolding — `forge-loop init` creates a new project's config + manual.

Idempotent: never overwrites an existing file unless ``--force`` is passed.
"""

from __future__ import annotations

import os
from pathlib import Path

from forge_loop.eventlog import SqliteEventLog
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import SqliteMemoryStore
from forge_loop.precommit import PreCommitRunner, ensure_precommit_hook
from forge_loop.tasks import SqliteTaskSagaStore
from forge_loop.worker_sessions import WorkerSessionStore

SAMPLE_YAML = """# forge-loop config — tune the loop for THIS project.
# All keys are optional; env vars (LOOP_*) override yaml values.

repo:
  # GitHub coordinates used by gh CLI calls.
  github: {github_repo}
  worktree_root: /tmp

deploy:
  # `task` target invoked when a tick lands any merged PR.
  # LEAVE EMPTY (the default) to skip redeploy entirely — most projects
  # don't have a `task`-based deploy. Set this only if you actually have a
  # Taskfile.yml target like `deploy:staging` you want the loop to run
  # after every merged PR.
  # Override at runtime with LOOP_DEPLOY_TASK env.
  task: ""

scheduling:
  parallel: 3
  tick_interval_s: 60
  worker_timeout_s: 3600
  max_ticks: 0
  # Every Nth tick: AI-as-PM grooms the backlog instead of fixing issues.
  maintenance_every_n_ticks: 5

labels:
  ready: "loop:ready"
  triage: "loop:triage"
  blocked: "loop:blocked"
  # Optional: issues tagged this label skip auto-merge (human review required).
  # Set to "" to disable. Default: risk:high
  risk_gate: "risk:high"

agent:
  # Default provider for worker / PO / critic. Use `codex` to route roles
  # through the local Codex CLI.
  provider: claude

worker:
  provider: claude
  model: claude-opus-4-7
  thinking: medium
  # Worker power: full (default, full host access) | standard (sandboxed to
  # the worktree) | readonly (planning/triage only). See docs/design.
  permissions: full

po:
  provider: claude
  model: claude-opus-4-7
  thinking: high

# Optional: enable the critic agent. When true, a critic subagent reviews
# every PR a worker opens BEFORE auto-merge fires (~30-90s per PR).
critic:
  enabled: false
  provider: claude
  model: claude-sonnet-4-6
  thinking: off

# Optional: per-issue attempt history persisted as GH issue comments.
# Workers read past attempts before starting; humans see them in the UI.
attempts:
  enabled: true
"""


SAMPLE_MANUAL_ENTRY = """# Project quick-reference

A starter knowledge entry. Drop more `.md` files into this directory; the
loop's AI agents can read them via `manual_lookup(topic)`.

## Conventions
- All ops go through `task <target>` (see Taskfile.yml).
- Branch + PR off `trunk`; never commit directly.
- Pre-commit gates run on every commit.

## Where things live
- Tests: `<module>/src/test/`
- E2E: `e2e/specs/`
- Migrations: see your DB module's migration dir.

## Deploying
- After any merge to `trunk`, redeploy via `task deploy:...` (your project's
  canonical deploy entry — set in `forge-loop.yaml`).

## Secrets
- Document your secret manager here (Infisical, Vault, sealed-secrets, etc.)
  so the loop's agents can fetch them without bothering humans.
"""


def init_project(
    target_dir: Path,
    github_repo: str = "owner/repo",
    force: bool = False,
    precommit_runner: PreCommitRunner | None = None,
) -> dict[str, list[str]]:
    """Scaffold a fresh forge-loop config in ``target_dir``.

    Returns ``{"created": [...], "skipped": [...]}`` (paths relative to target).
    """
    created: list[str] = []
    skipped: list[str] = []
    precommit: list[str] = []
    precommit_hint: list[str] = []

    yaml_path = target_dir / "forge-loop.yaml"
    if yaml_path.exists() and not force:
        skipped.append(str(yaml_path.relative_to(target_dir)))
    else:
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(SAMPLE_YAML.format(github_repo=github_repo))
        created.append(str(yaml_path.relative_to(target_dir)))

    manual_dir = target_dir / "manual"
    manual_dir.mkdir(exist_ok=True)
    sample_md = manual_dir / "project-quickref.md"
    if sample_md.exists() and not force:
        skipped.append(str(sample_md.relative_to(target_dir)))
    else:
        sample_md.write_text(SAMPLE_MANUAL_ENTRY)
        created.append(str(sample_md.relative_to(target_dir)))

    gitignore = target_dir / ".gitignore"
    snippet = """
# forge-loop runtime
loop-runner.pid
loop-runner.pause
loop-runner.stop
loop-runner-logs/
docs/ops/loop-runner.json
docs/ops/loop-runner-events.jsonl
docs/ops/loop-runner-summaries.jsonl
docs/ops/loop-runner.pid
docs/ops/loop-runner.pause
docs/ops/loop-runner.stop
docs/ops/loop-runner.HALT
docs/ops/loop-runner-logs/
docs/ops/loop-runner.force-retry.json
docs/ops/worker-sessions.db*
docs/ops/critic-*.log*
"""
    if gitignore.exists():
        content = gitignore.read_text()
        if "forge-loop runtime" not in content:
            gitignore.write_text(content.rstrip() + snippet)
            created.append(".gitignore (appended)")
    else:
        # Don't create one — many projects already have one at repo root.
        # Caller can wire .gitignore separately if needed.
        pass

    _ensure_control_plane_stores(target_dir, created=created, skipped=skipped, force=force)

    outcome, hint = ensure_precommit_hook(
        target_dir,
        runner=precommit_runner,
    )
    precommit.append(outcome.value)
    if hint:
        precommit_hint.append(hint)

    return {
        "created": created,
        "skipped": skipped,
        "precommit": precommit,
        "precommit_hint": precommit_hint,
    }


def _ensure_control_plane_stores(
    target_dir: Path,
    *,
    created: list[str],
    skipped: list[str],
    force: bool,
) -> None:
    forge_dir = target_dir / ".forge"
    ops_dir = target_dir / "docs" / "ops"
    forge_dir.mkdir(parents=True, exist_ok=True)
    ops_dir.mkdir(parents=True, exist_ok=True)

    event_log_path = forge_dir / "events.db"
    _record_path(event_log_path, target_dir, created=created, skipped=skipped, force=force)
    SqliteEventLog(event_log_path)

    frontier_path = forge_dir / "frontier.yaml"
    if frontier_path.exists() and not force:
        skipped.append(str(frontier_path.relative_to(target_dir)))
    else:
        FrontierStore(frontier_path).save(_default_frontier_cursor())
        created.append(str(frontier_path.relative_to(target_dir)))

    memory_path = forge_dir / "memory.db"
    _record_path(memory_path, target_dir, created=created, skipped=skipped, force=force)
    SqliteMemoryStore(memory_path)

    tasks_path = forge_dir / "tasks.db"
    _record_path(tasks_path, target_dir, created=created, skipped=skipped, force=force)
    SqliteTaskSagaStore(tasks_path)

    sessions_path = ops_dir / "worker-sessions.db"
    _record_path(sessions_path, target_dir, created=created, skipped=skipped, force=force)
    WorkerSessionStore(sessions_path).close()


def _record_path(
    path: Path,
    target_dir: Path,
    *,
    created: list[str],
    skipped: list[str],
    force: bool,
) -> None:
    relative = str(path.relative_to(target_dir))
    if path.exists() and not force:
        skipped.append(relative)
    else:
        created.append(relative)


def _default_frontier_cursor() -> FrontierCursor:
    return FrontierCursor(
        product_goal="Make this repository resumable for long-running agent work.",
        current_problem="Control-plane stores have just been initialized.",
        next_expansion="Run a bounded milestone and promote durable events, memory, tasks, and frontier facts as real work happens.",
        why_now="A reset should boot from explicit durable state instead of missing files.",
        active_decisions=(
            "Context windows are working memory, not durable project state.",
            "Workers are disposable; control-plane state must be external and replayable.",
        ),
        hot_files=(),
        hot_tests=(),
        open_questions=("Which frontier axis should the next milestone advance?",),
    )


def detect_github_repo(target_dir: Path) -> str:
    """Best-effort: derive `owner/repo` from git remote origin."""
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(target_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return "owner/repo"
    url = r.stdout.strip()
    # Match git@github.com:owner/repo(.git) and https://github.com/owner/repo(.git)
    import re

    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return "owner/repo"


def ensure_labels_via_gh(repo: str, labels: list[tuple[str, str, str]]) -> list[str]:
    """Create the loop's vocabulary labels in the GH repo (idempotent).

    Each label is ``(name, color_hex, description)``. Returns the list of
    label names that were CREATED (already-existing ones are silently skipped).
    """
    import subprocess

    created: list[str] = []
    for name, color, desc in labels:
        r = subprocess.run(
            [
                "gh",
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                desc,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            created.append(name)
    return created


DEFAULT_LABELS: list[tuple[str, str, str]] = [
    ("loop:ready", "FFD700", "Sprint loop will autonomously attempt this issue"),
    ("loop:triage", "DBAB09", "Needs maintenance attention (vague title, missing context, stale)"),
    ("loop:blocked", "D93F0B", "Worker attempted and reported a real blocker"),
    ("risk:high", "B60205", "Workers stop at PR-open; human review required before merge"),
]


def maybe_print(line: str, quiet: bool = False) -> None:
    if not quiet:
        # Using stdout so init's output is consumable as a script.
        # Don't import print — just use it directly.
        os.write(1, (line + "\n").encode())
