"""Unit coverage for the operational-entropy metric (issue #402).

The metric assembles four cheap, read-only divergence counts:
``open_branches``, ``live_worktrees``, ``open_epics``, ``backlog_age_days``.
These tests feed a fake git adapter (branch list + ``git worktree list
--porcelain`` output) and a label-aware fake gh client (epics + dated tickets)
and assert the counts, the oldest-issue age math, and — per the testing
manifesto (T2 external-dep false case, T3 returncode!=0) — that every source
degrades to ``None``/``0`` instead of raising when git or GitHub fails.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_loop.adapters.git import FakeGitClient, GitResult
from forge_loop.control.status import collect_control_plane_status
from forge_loop.gh_client import Issue, MockGhClient

# A two-worktree porcelain blob: each worktree is one ``worktree <path>`` header.
_PORCELAIN = (
    "worktree /repo\n"
    "HEAD 1111111111111111111111111111111111111111\n"
    "branch refs/heads/trunk\n"
    "\n"
    "worktree /tmp/wt-loop-1\n"
    "HEAD 2222222222222222222222222222222222222222\n"
    "branch refs/heads/loop/1\n"
)


class _LabelAwareGh(MockGhClient):
    """gh fake that honours the ``epic`` label so ``list_open_backlog`` can
    split epics from tickets (the real ``MockGhClient`` is label-blind)."""

    def __init__(self, epics: list[Issue], tickets: list[Issue]) -> None:
        super().__init__()
        self._epics = epics
        self._tickets = tickets

    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Issue]:
        self._record("issues_by_label", owner=owner, repo=repo, label=label, limit=limit)
        src = self._epics if label == "epic" else (self._epics + self._tickets)
        return list(src[:limit])


def _git(branches: GitResult, worktrees: GitResult) -> FakeGitClient:
    return FakeGitClient(
        results_by_method={"branch_list": branches, "worktree_list": worktrees}
    )


def _ok(stdout: str) -> GitResult:
    return GitResult(argv=["git"], returncode=0, stdout=stdout)


def _fail() -> GitResult:
    return GitResult(argv=["git"], returncode=128, stderr="fatal: not a git repository")


def _entropy(tmp_path, now, *, git, gh, github_repo="o/r"):
    return collect_control_plane_status(
        tmp_path, now, github_repo=github_repo, git=git, gh=gh
    )["operational_entropy"]


class TestHappyPath:
    def test_counts_and_oldest_age_math(self, tmp_path):
        now = datetime(2026, 6, 8, tzinfo=UTC)
        epics = [
            Issue(number=1, title="epic-a", labels=["epic"],
                  created_at=(now - timedelta(days=5)).isoformat()),
            Issue(number=2, title="epic-b", labels=["epic"],
                  created_at=(now - timedelta(days=3)).isoformat()),
        ]
        tickets = [
            Issue(number=3, title="t-old", created_at=(now - timedelta(days=14)).isoformat()),
            Issue(number=4, title="t-new", created_at=(now - timedelta(days=2)).isoformat()),
        ]
        git = _git(_ok("* trunk\n  loop/1\n  loop/2\n"), _ok(_PORCELAIN))

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh(epics, tickets))

        assert oe == {
            "open_branches": 2,  # only loop/<n> branches — trunk is not loop exhaust
            "live_worktrees": 2,
            "open_epics": 2,
            "backlog_age_days": 14,  # oldest of epics+tickets is the 14-day ticket
        }


class TestOpenBranchesIsLoopOnly:
    """Issue #415 review — ``open_branches`` is a *loop-branch* convergence gauge,
    so non-``loop/`` lines (trunk, feature, detached HEAD) must not inflate it."""

    def test_only_loop_prefixed_branches_are_counted(self, tmp_path):
        now = datetime.now(UTC)
        git = _git(
            _ok("* trunk\n  feature/x\n  loop/7\n  loop/12\n  (HEAD detached at abc)\n"),
            _ok(_PORCELAIN),
        )

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], []))

        assert oe["open_branches"] == 2  # loop/7 and loop/12 only

    def test_no_loop_branches_is_zero_not_total(self, tmp_path):
        now = datetime.now(UTC)
        git = _git(_ok("* trunk\n  feature/x\n"), _ok(_PORCELAIN))

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], []))

        assert oe["open_branches"] == 0


class TestBacklogAgeEdges:
    def test_empty_backlog_age_is_zero_not_a_crash(self, tmp_path):
        # Reachable backlog with no issues → 0 (the ``min()`` of empty guard, T1).
        now = datetime.now(UTC)
        git = _git(_ok(""), _ok(""))

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], []))

        assert oe["open_epics"] == 0
        assert oe["backlog_age_days"] == 0

    def test_issue_with_no_created_at_is_ignored(self, tmp_path):
        now = datetime(2026, 6, 8, tzinfo=UTC)
        tickets = [
            Issue(number=3, title="undated", created_at=None),
            Issue(number=4, title="dated", created_at=(now - timedelta(days=6)).isoformat()),
        ]
        git = _git(_ok("trunk\n"), _ok(_PORCELAIN))

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], tickets))

        assert oe["backlog_age_days"] == 6


class TestAdversarialDegradation:
    def test_git_branch_failure_yields_none_branches(self, tmp_path):
        # T3: returncode != 0 must NOT fall through to a bogus count.
        now = datetime.now(UTC)
        git = _git(_fail(), _ok(_PORCELAIN))

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], []))

        assert oe["open_branches"] is None
        assert oe["live_worktrees"] == 2  # the other source is unaffected

    def test_git_worktree_failure_yields_none_worktrees(self, tmp_path):
        now = datetime.now(UTC)
        git = _git(_ok("* trunk\n  loop/1\n"), _fail())

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], []))

        assert oe["live_worktrees"] is None
        assert oe["open_branches"] == 1  # one loop/<n> branch; trunk excluded

    def test_garbage_porcelain_does_not_overcount_or_raise(self, tmp_path):
        now = datetime.now(UTC)
        git = _git(_ok("trunk\n"), _ok("garbage not porcelain\n\xff\x00 nonsense\n"))

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], []))

        assert oe["live_worktrees"] == 0  # no ``worktree `` header lines

    def test_github_query_raising_yields_none_epics_and_age(self, tmp_path):
        # T2: the GitHub source's false/failure case is exercised explicitly.
        now = datetime.now(UTC)
        gh = MockGhClient(raise_on={"issues_by_label": _gh_error()})
        git = _git(_ok("* trunk\n  loop/1\n"), _ok(_PORCELAIN))

        oe = _entropy(tmp_path, now, git=git, gh=gh)

        assert oe["open_epics"] is None
        assert oe["backlog_age_days"] is None
        # git-derived counts still resolve — sources degrade independently.
        assert oe["open_branches"] == 1  # one loop/<n> branch; trunk excluded
        assert oe["live_worktrees"] == 2

    def test_unconfigured_repo_slug_yields_none_gh_counts(self, tmp_path):
        now = datetime.now(UTC)
        git = _git(_ok("trunk\n"), _ok(_PORCELAIN))

        oe = _entropy(tmp_path, now, git=git, gh=_LabelAwareGh([], []), github_repo=None)

        assert oe["open_epics"] is None
        assert oe["backlog_age_days"] is None

    def test_full_blackout_emits_block_without_raising(self, tmp_path):
        # Both sources down at once: the status path must still return the block.
        now = datetime.now(UTC)
        gh = MockGhClient(raise_on={"issues_by_label": _gh_error()})
        git = _git(_fail(), _fail())

        oe = _entropy(tmp_path, now, git=git, gh=gh)

        assert oe == {
            "open_branches": None,
            "live_worktrees": None,
            "open_epics": None,
            "backlog_age_days": None,
        }


def _gh_error():
    from forge_loop.gh_client import GhError

    return GhError("issues_by_label", 500, "boom")
