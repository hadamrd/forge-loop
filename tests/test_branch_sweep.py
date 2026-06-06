"""Tests for the stale-branch sweep (issue #146).

Covers the testing manifesto rules that apply:

* T1 (state machine ⇒ one test per edge + a fallthrough adversarial test):
  every PR-state arm of ``sweep_branches`` — orphan/None, OPEN, MERGED-old,
  MERGED-recent, CLOSED-old, and an UNKNOWN/garbage state default arm.
* T2 (external yes/no ⇒ test the false case): ``find_pr_by_head`` returns None.
* T3 (subprocess.returncode ⇒ both ==0 and !=0): the local git sweep.
* Adversarial: gh rate-limit mid-sweep backs off (does not hammer); a failed
  delete is recorded; ``list_branches`` failure degrades cleanly.
* Regression: a branch with an OPEN PR is NEVER deleted.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_loop.branch_sweep import (
    BranchSweepReport,
    sweep_branches,
    sweep_local_branches,
)
from forge_loop.events import read_events
from forge_loop.gh_client import GhError, MockGhClient

NOW = datetime(2026, 6, 6, tzinfo=UTC)
OWNER, REPO = "o", "r"


def _pr(
    state: str,
    *,
    merged: str = "",
    closed: str = "",
    updated: str = "",
) -> dict[str, Any]:
    return {
        "url": "https://github.com/o/r/pull/1",
        "number": 1,
        "state": state,
        "mergedAt": merged,
        "closedAt": closed,
        "updatedAt": updated,
    }


def _old() -> str:
    return "2026-05-20T00:00:00Z"  # 17 days before NOW


def _recent() -> str:
    return "2026-06-05T00:00:00Z"  # 1 day before NOW


def _deleted_branches(gh: MockGhClient) -> list[str]:
    return [kw["branch"] for (name, kw) in gh.calls if name == "delete_branch"]


# ---------------------------------------------------------------------------
# sweep_branches — per-state edges (T1)
# ---------------------------------------------------------------------------


def test_deletes_old_merged_branch() -> None:
    gh = MockGhClient(branches_response=["loop/10"])
    gh.pr_by_head["loop/10"] = _pr("MERGED", merged=_old())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.deleted == ["loop/10"]
    assert _deleted_branches(gh) == ["loop/10"]


def test_deletes_old_closed_branch() -> None:
    gh = MockGhClient(branches_response=["fix/9"])
    gh.pr_by_head["fix/9"] = _pr("CLOSED", closed=_old())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.deleted == ["fix/9"]


def test_open_pr_never_deleted() -> None:
    # Regression: an OPEN PR's branch must NEVER be deleted.
    gh = MockGhClient(branches_response=["feat/3"])
    gh.pr_by_head["feat/3"] = _pr("OPEN", updated=_old())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.deleted == []
    assert report.skipped == ["feat/3"]
    assert _deleted_branches(gh) == []


def test_recent_merged_not_deleted() -> None:
    # Age gate: merged but too recent (< min_age_days) is skipped.
    gh = MockGhClient(branches_response=["loop/5"])
    gh.pr_by_head["loop/5"] = _pr("MERGED", merged=_recent())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, min_age_days=7, now=NOW)
    assert report.deleted == []
    assert report.skipped == ["loop/5"]


def test_orphan_branch_no_pr_is_skipped_not_crashed() -> None:
    # T2 false-case + adversarial: a branch with NO PR at all (find_pr_by_head
    # returns None) must be logged + skipped, never deleted, never crash.
    gh = MockGhClient(branches_response=["loop/77"])  # no pr_by_head entry
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.skipped == ["loop/77"]
    assert report.deleted == []


def test_unknown_state_falls_through_to_skip() -> None:
    # T1 fallthrough/default arm: a state that is neither OPEN nor a deletable
    # MERGED/CLOSED (garbage / future GitHub value) must NOT delete.
    gh = MockGhClient(branches_response=["chore/1"])
    gh.pr_by_head["chore/1"] = _pr("LOCKED", updated=_old())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.deleted == []
    assert report.skipped == ["chore/1"]


# ---------------------------------------------------------------------------
# sweep_branches — selection / protection
# ---------------------------------------------------------------------------


def test_non_prefixed_branch_ignored() -> None:
    gh = MockGhClient(branches_response=["random-wip", "loop/2"])
    gh.pr_by_head["loop/2"] = _pr("MERGED", merged=_old())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    # random-wip is never even looked up (not scanned).
    assert report.scanned == 1
    assert report.deleted == ["loop/2"]
    assert ("find_pr_by_head", {"owner": OWNER, "repo": REPO, "head": "random-wip"}) not in gh.calls


def test_base_branch_protected_even_if_prefixed() -> None:
    gh = MockGhClient(branches_response=["loop/main"])
    gh.pr_by_head["loop/main"] = _pr("MERGED", merged=_old())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, base_branch="loop/main", now=NOW)
    assert report.scanned == 0
    assert report.deleted == []


def test_mixed_batch_only_old_merged_deleted() -> None:
    gh = MockGhClient(branches_response=["loop/a", "feat/b", "fix/c", "docs/d"])
    gh.pr_by_head["loop/a"] = _pr("MERGED", merged=_old())  # delete
    gh.pr_by_head["feat/b"] = _pr("OPEN", updated=_old())  # keep
    gh.pr_by_head["fix/c"] = _pr("CLOSED", closed=_recent())  # too recent, keep
    # docs/d -> orphan, skip
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.deleted == ["loop/a"]
    assert set(report.skipped) == {"feat/b", "fix/c", "docs/d"}


# ---------------------------------------------------------------------------
# sweep_branches — adversarial / failure modes
# ---------------------------------------------------------------------------


def test_delete_failure_recorded_as_error() -> None:
    gh = MockGhClient(branches_response=["loop/8"], delete_branch_fails=True)
    gh.pr_by_head["loop/8"] = _pr("MERGED", merged=_old())
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.deleted == []
    assert "loop/8" in report.errors


def test_rate_limit_mid_sweep_backs_off_and_does_not_hammer() -> None:
    # Adversarial: GitHub rate-limits the FIRST find_pr_by_head. The sweep must
    # set rate_limited + STOP — not keep hammering the remaining branches.
    gh = MockGhClient(branches_response=["loop/1", "loop/2", "loop/3"])
    gh.raise_on["find_pr_by_head"] = GhError("find_pr_by_head", 403, "API rate limit exceeded")
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.rate_limited is True
    lookups = [c for c in gh.calls if c[0] == "find_pr_by_head"]
    assert len(lookups) == 1  # stopped after the first 403 — did not hammer


def test_list_branches_failure_degrades_cleanly() -> None:
    gh = MockGhClient(branches_response=["loop/1"])
    gh.raise_on["list_branches"] = GhError("list_branches", 500, "boom")
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.deleted == []
    assert "*" in report.errors
    assert report.rate_limited is False


def test_list_branches_rate_limit_sets_flag() -> None:
    gh = MockGhClient(branches_response=["loop/1"])
    gh.raise_on["list_branches"] = GhError("list_branches", 429, "secondary rate limit")
    report = sweep_branches(gh, owner=OWNER, repo=REPO, now=NOW)
    assert report.rate_limited is True


# ---------------------------------------------------------------------------
# sweep_local_branches (T3: returncode ==0 and !=0)
# ---------------------------------------------------------------------------


class _FakeRun:
    """Programmable RunFn for the local sweep. Maps argv -> CompletedProcess."""

    def __init__(self, for_each_ref: tuple[int, str], head: str = "trunk") -> None:
        self.for_each_ref = for_each_ref
        self.head = head
        self.deleted: list[str] = []
        self.delete_rc = 0

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "for-each-ref"]:
            rc, out = self.for_each_ref
            return subprocess.CompletedProcess(args, rc, out, "err" if rc else "")
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(args, 0, self.head + "\n", "")
        if args[:3] == ["git", "branch", "-D"]:
            if self.delete_rc == 0:
                self.deleted.append(args[3])
            return subprocess.CompletedProcess(args, self.delete_rc, "", "err")
        return subprocess.CompletedProcess(args, 0, "", "")


def _ref_line(name: str, upstream: str, days_ago: float) -> str:
    unix = int(NOW.timestamp() - days_ago * 86400.0)
    return f"{name}\t{upstream}\t{unix}"


def test_local_deletes_old_untracked_branch() -> None:
    # T3 (==0): for-each-ref succeeds; an untracked, stale, prefixed branch
    # is deleted.
    out = "\n".join(
        [
            _ref_line("loop/old", "", 60),  # delete
            _ref_line("loop/fresh", "", 1),  # too recent, keep
            _ref_line("feat/tracked", "origin/feat/tracked", 60),  # tracked, keep
            _ref_line("trunk", "origin/trunk", 60),  # base, keep
            _ref_line("random-old", "", 60),  # no prefix, keep
        ]
    )
    run = _FakeRun((0, out), head="loop/current")
    deleted = sweep_local_branches(
        Path("/x"), base_branch="trunk", min_age_days=30, now=NOW, run=run
    )
    assert deleted == ["loop/old"]
    assert run.deleted == ["loop/old"]


def test_local_skips_current_branch() -> None:
    out = _ref_line("loop/current", "", 90)
    run = _FakeRun((0, out), head="loop/current")
    deleted = sweep_local_branches(
        Path("/x"), base_branch="trunk", min_age_days=30, now=NOW, run=run
    )
    assert deleted == []


def test_local_for_each_ref_failure_returns_empty() -> None:
    # T3 (!=0): for-each-ref fails -> no deletions, no crash.
    run = _FakeRun((128, ""), head="trunk")
    deleted = sweep_local_branches(
        Path("/x"), base_branch="trunk", min_age_days=30, now=NOW, run=run
    )
    assert deleted == []


def test_local_delete_returncode_nonzero_not_listed() -> None:
    # T3 (!=0 on the delete itself): branch matches all criteria but
    # ``git branch -D`` fails -> it is NOT reported as deleted.
    run = _FakeRun((0, _ref_line("loop/old", "", 90)), head="trunk")
    run.delete_rc = 1
    deleted = sweep_local_branches(
        Path("/x"), base_branch="trunk", min_age_days=30, now=NOW, run=run
    )
    assert deleted == []


# ---------------------------------------------------------------------------
# Seam test (Q10): run_branch_sweep hands counts to the typed event that
# actually lands in events.jsonl.
# ---------------------------------------------------------------------------


def test_run_branch_sweep_emits_event(tmp_path: Path, monkeypatch: Any) -> None:
    from forge_loop import gh_client
    from forge_loop.config import Config
    from forge_loop.runner import tick_checks

    gh = MockGhClient(branches_response=["loop/10", "feat/open"])
    gh.pr_by_head["loop/10"] = _pr("MERGED", merged=_old())
    gh.pr_by_head["feat/open"] = _pr("OPEN", updated=_old())
    monkeypatch.setattr(gh_client, "GithubkitClient", lambda *a, **k: gh)
    # Local sweep would touch real git in cfg.repo — stub it to a no-op.
    monkeypatch.setattr(
        "forge_loop.branch_sweep.sweep_local_branches",
        lambda *a, **k: [],
    )

    cfg = Config(repo=tmp_path, github_repo="o/r", base_branch="trunk")
    report = tick_checks.run_branch_sweep(cfg, 0)
    assert isinstance(report, BranchSweepReport)
    assert report.deleted == ["loop/10"]

    events = list(read_events(cfg.events_file))
    done = [e for e in events if e.get("kind") == "branch_sweep_done"]
    assert done, "branch_sweep_done event must land in events.jsonl"
    assert done[-1]["deleted"] == 1
    assert done[-1]["skipped"] == 1


def test_run_branch_sweep_no_repo_returns_none(tmp_path: Path) -> None:
    from forge_loop.config import Config
    from forge_loop.runner import tick_checks

    cfg = Config(repo=tmp_path, github_repo=None)
    assert tick_checks.run_branch_sweep(cfg, 0) is None
