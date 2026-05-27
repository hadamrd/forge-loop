"""Redeploy hook — shells out to a project-defined `task` target.

Operators wire their deploy command via the ``LOOP_DEPLOY_TASK`` env var
(or the ``deploy.task`` field in ``forge-loop.yaml``). If unset, redeploy
is a no-op and emits no subprocess invocation.

Any secret-source / image-push / k8s-rollout details are the responsibility
of the operator's task target, not this module.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def redeploy(repo: Path, task_name: str = "") -> tuple[bool, str]:
    """Run ``task <task_name>`` from ``repo``. Returns (ok, tail).

    Contract:
      * ``task_name`` empty/falsy → returns ``(True, "...skipped...")`` and
        does NOT invoke any subprocess. Callers SHOULD guard before reaching
        this so they can skip the redeploy event entirely.
      * Subprocess returns non-zero → ``(False, tail)`` where ``tail`` is the
        last 800 chars of combined stdout/stderr.
      * ``task`` binary missing → ``(False, "...")`` with a clear message,
        NOT a ``FileNotFoundError`` propagated to the caller.
    """
    if not task_name:
        return True, "no deploy task configured — skipped (set LOOP_DEPLOY_TASK or deploy.task)"

    try:
        r = subprocess.run(
            ["task", task_name],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False, (
            "deploy: `task` binary not found on PATH "
            "(install go-task/task or unset LOOP_DEPLOY_TASK)"
        )

    combined = (r.stdout or "") + (r.stderr or "")
    tail = combined[-800:]
    return r.returncode == 0, tail
