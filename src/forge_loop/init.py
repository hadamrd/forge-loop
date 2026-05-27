"""Project scaffolding — `forge-loop init` creates a new project's config + manual.

Idempotent: never overwrites an existing file unless ``--force`` is passed.
"""

from __future__ import annotations

import os
from pathlib import Path

SAMPLE_YAML = """# forge-loop config — tune the loop for THIS project.
# All keys are optional; env vars (LOOP_*) override yaml values.

repo:
  # GitHub coordinates used by gh CLI calls.
  github: {github_repo}
  worktree_root: /tmp

deploy:
  # `task` target invoked when a tick lands any merged PR.
  # Use the deploy entry-point that's canonical for your project.
  task: deploy:k3s:trunk

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

# Optional: enable the critic agent. When true, a critic subagent reviews
# every PR a worker opens BEFORE auto-merge fires (~30-90s per PR).
critic:
  enabled: false

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
) -> dict[str, list[str]]:
    """Scaffold a fresh forge-loop config in ``target_dir``.

    Returns ``{"created": [...], "skipped": [...]}`` (paths relative to target).
    """
    created: list[str] = []
    skipped: list[str] = []

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
    snippet = "\n# forge-loop runtime\nloop-runner.pid\nloop-runner.pause\nloop-runner.stop\nloop-runner-logs/\n"
    if gitignore.exists():
        content = gitignore.read_text()
        if "forge-loop runtime" not in content:
            gitignore.write_text(content.rstrip() + snippet)
            created.append(".gitignore (appended)")
    else:
        # Don't create one — many projects already have one at repo root.
        # Caller can wire .gitignore separately if needed.
        pass

    return {"created": created, "skipped": skipped}


def detect_github_repo(target_dir: Path) -> str:
    """Best-effort: derive `owner/repo` from git remote origin."""
    import subprocess

    r = subprocess.run(
        ["git", "-C", str(target_dir), "remote", "get-url", "origin"],
        capture_output=True, text=True, check=False,
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
            ["gh", "label", "create", name,
             "--repo", repo, "--color", color, "--description", desc],
            capture_output=True, text=True, check=False,
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
