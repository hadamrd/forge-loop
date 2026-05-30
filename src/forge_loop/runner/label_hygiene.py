"""Best-effort GitHub label hygiene for completed issues."""

from __future__ import annotations

from typing import Protocol

from forge_loop.config import Config
from forge_loop.gh import unlabel
from forge_loop.state import append_event


class UnlabelFn(Protocol):
    def __call__(self, issue: int, label: str, *, repo: str | None = None) -> None: ...


def remove_ready_label(
    cfg: Config,
    issue: int,
    *,
    status: str,
    pr_url: str | None = None,
    unlabel_fn: UnlabelFn = unlabel,
) -> None:
    try:
        unlabel_fn(issue, cfg.labels.ready, repo=cfg.github_repo)
        append_event(
            cfg.events_file,
            "issue_ready_label_removed",
            issue=issue,
            status=status,
            pr_url=pr_url,
            label=cfg.labels.ready,
        )
    except Exception as ex_:  # noqa: BLE001
        append_event(
            cfg.events_file,
            "issue_ready_label_remove_failed",
            issue=issue,
            err=str(ex_)[:200],
        )
