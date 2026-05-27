"""Redeploy hook — shells out to a project-defined `task` target.

Operators wire their deploy command via the ``LOOP_DEPLOY_TASK`` env var
(or the ``deploy.task`` field in ``forge-loop.yaml``). If unset, redeploy
is a no-op.

Any secret-source / image-push / k8s-rollout details are the responsibility
of the operator's task target, not this module.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_TASK = os.environ.get("LOOP_DEPLOY_TASK", "")


def redeploy(repo: Path, task_name: str = DEFAULT_TASK) -> tuple[bool, str]:
    """Run ``task <task_name>`` from ``repo``. Returns (ok, tail).

    No-ops cleanly when no task is configured.
    """
    if not task_name:
        return True, "no deploy task configured (set LOOP_DEPLOY_TASK env)"

    r = subprocess.run(
        ["task", task_name],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = (r.stdout or "") + (r.stderr or "")
    tail = combined[-800:]
    return r.returncode == 0, tail
