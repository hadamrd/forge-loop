"""Deterministic branch garbage-collection (operational-convergence axis).

A ``loop/<n>`` branch whose issue ``#<n>`` is CLOSED is landed work — squash-merge
closed the issue, and squash also severs git's own "merged" signal, so nothing else
can safely prune it. This sweep deletes exactly those branches and nothing else:

* only ``loop/<n>`` branches are ever eligible (never feat/fix/docs/chore/trunk/main);
* a branch in ``protected`` (the base branch) is never touched;
* an issue that is OPEN, or whose state cannot be read, is preserved (fail-safe).

No LLM; pure Python. Mirrors :mod:`forge_loop.epic_sweep` (#367). The pure plan
(``loop_issue_number`` + the per-branch classification in :func:`sweep`) is unit-
tested directly; the runner adapter (``tick_checks.run_branch_sweep``) only supplies
the branch list and a real GhClient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

_LOOP_BRANCH = re.compile(r"^loop/(\d+)(?:-.*)?$")


class GhClientLike(Protocol):
    """Minimal subset of :class:`forge_loop.gh_client.GhClient` the sweep needs.

    Defined locally so tests pass a hand-rolled fake; both ``GithubkitClient`` and
    ``MockGhClient`` structurally match.
    """

    def get_issue_state(self, owner: str, repo: str, number: int) -> str | None: ...
    def delete_branch(self, owner: str, repo: str, branch: str) -> bool: ...


@dataclass
class BranchSweepReport:
    """Result of one :func:`sweep`. Each evaluated branch lands in exactly one list.

    ``deleted`` — loop/<n> whose issue was closed and the delete landed.
    ``skipped_open`` — loop/<n> whose issue is still open (preserved).
    ``skipped_unknown`` — not a loop/<n> branch, or the issue state couldn't be read.
    ``errors`` — branch → short reason a delete/read failed.
    """

    deleted: list[str] = field(default_factory=list)
    skipped_open: list[str] = field(default_factory=list)
    skipped_unknown: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def loop_issue_number(branch: str) -> int | None:
    """Return the issue number for a ``loop/<n>`` or ``loop/<n>-slug`` branch, else None."""
    m = _LOOP_BRANCH.match(branch.strip())
    return int(m.group(1)) if m else None


def sweep(
    gh_client: GhClientLike,
    *,
    owner: str,
    repo: str,
    branch_names: list[str],
    protected: frozenset[str] = frozenset(),
) -> BranchSweepReport:
    """Delete ``loop/<n>`` branches whose issue ``#<n>`` is closed.

    Conservative by construction (see module docstring). ``protected`` branches and
    any non-loop branch are never deleted; an open or unreadable issue is preserved.
    """
    report = BranchSweepReport()
    for branch in branch_names:
        if branch in protected:
            continue
        number = loop_issue_number(branch)
        if number is None:
            report.skipped_unknown.append(branch)
            continue
        try:
            state = gh_client.get_issue_state(owner, repo, number)
        except Exception as ex:  # noqa: BLE001 — best-effort GC, never crash the tick
            report.errors[branch] = str(ex)[:200]
            continue
        if state is None:
            report.skipped_unknown.append(branch)
            continue
        if state.strip().lower() != "closed":
            report.skipped_open.append(branch)
            continue
        try:
            ok = gh_client.delete_branch(owner, repo, branch)
        except Exception as ex:  # noqa: BLE001
            report.errors[branch] = str(ex)[:200]
            continue
        if ok:
            report.deleted.append(branch)
        else:
            report.errors[branch] = "delete returned False"
    return report
