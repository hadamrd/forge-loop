"""Tests for auto-rescue's settings-only guard (issue #366).

The loop plants ``.claude/settings.json`` into every worker worktree, and SDK
sessions mutate it as a pure side-effect. Before #366, ``rescue_uncommitted_work``
shipped a "+1" junk PR whose entire diff was that planted file. These tests pin:

* a settings-only dirty worktree is SKIPPED (no commit, no push, no PR) and emits
  an observable ``rescue_skipped_settings_only`` event;
* a mixed worktree is still rescued but the planted file is EXCLUDED from the commit;
* a real-change-only worktree is rescued exactly as before (no regression);
* the porcelain ``??`` / `` M`` / ``M `` columns are all classified settings-only;
* over-eager path matching is guarded (``.bak`` / nested ``settings.json`` siblings
  are REAL changes and get rescued).

Fixture style mirrors ``tests/test_state_rotation.py`` (tmp_path + direct calls)
and ``tests/conftest.py`` (small typed factories).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge_loop import gh_issues
from forge_loop.config import Config
from forge_loop.runner.rescue import (
    _diff_is_settings_only,
    _dirty_paths,
    rescue_uncommitted_work,
)
from forge_loop.worker import WorkerOutcome

_SETTINGS = ".claude/settings.json"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _init_worktree(path: Path, *, with_remote: bool = False) -> Path:
    """A git worktree on a non-trunk branch with one seed commit.

    When ``with_remote`` is set, an adjacent bare repo is wired as ``origin`` and
    ``trunk`` is pushed so the rescue push + ``origin/trunk`` diff resolve.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "trunk"], path)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "tester"], path)
    (path / "README.md").write_text("seed\n")
    _git(["add", "."], path)
    _git(["commit", "-m", "seed"], path)
    if with_remote:
        remote = path.parent / f"{path.name}-remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "trunk", str(remote)], check=True)
        _git(["remote", "add", "origin", str(remote)], path)
        _git(["push", "-u", "origin", "trunk"], path)
    _git(["checkout", "-b", "loop/366-x"], path)
    return path


def _plant_settings(path: Path, body: str = '{"permissions": {}}\n') -> None:
    cdir = path / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "settings.json").write_text(body)


def _outcome(issue: int = 366) -> WorkerOutcome:
    return WorkerOutcome(
        issue=issue,
        title="t",
        pr_url=None,
        status="no_pr",
        duration_s=1.0,
        stdout_tail="",
    )


def _config(tmp_path: Path, *, github_repo: str | None = "owner/repo") -> Config:
    return Config(repo=tmp_path / "repo", github_repo=github_repo, base_branch="trunk")


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Stub the GitHub PR surface; record calls so tests can assert on them."""
    calls: dict[str, list] = {"create_pull": [], "add_pr_label": [], "auto_merge": []}

    def _create_pull(title, body, branch, base, repo, draft=False):  # noqa: ANN001
        calls["create_pull"].append((title, branch, base, repo, draft))
        return "https://github.com/owner/repo/pull/1"

    def _add_pr_label(pr, labels, repo=None):  # noqa: ANN001
        calls["add_pr_label"].append((pr, labels, repo))
        return True

    def _enable(pr, repo=None):  # noqa: ANN001
        calls["auto_merge"].append((pr, repo))

    monkeypatch.setattr(gh_issues, "create_pull", _create_pull)
    monkeypatch.setattr(gh_issues, "add_pr_label", _add_pr_label)
    monkeypatch.setattr(gh_issues, "enable_pr_auto_merge", _enable)
    return calls


def _patch_worktree(monkeypatch: pytest.MonkeyPatch, wt: Path) -> None:
    monkeypatch.setattr(
        "forge_loop.worker_worktree.worktree_path", lambda repo, issue: wt
    )


def _head(wt: Path) -> str:
    return _git(["rev-parse", "HEAD"], wt).stdout.strip()


def _read_events(cfg: Config) -> list[dict]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(ln) for ln in cfg.events_file.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Unit: pure classifier — porcelain states + over-eager-match guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    ["?? ", " M ", "M  "],  # untracked, unstaged-modified, staged-modified
    ids=["untracked", "modified", "staged"],
)
def test_classifier_settings_only_across_porcelain_states(
    tmp_path: Path, column: str
) -> None:
    wt = _init_worktree(tmp_path / "wt")
    # Drive the real porcelain output for each state instead of hand-faking it.
    _plant_settings(wt)
    if column.strip() in {"M", ""}:  # needs to be tracked first
        _git(["add", _SETTINGS], wt)
        _git(["commit", "-m", "track settings"], wt)
        (wt / ".claude" / "settings.json").write_text('{"permissions": {"x": 1}}\n')
    if column == "M  ":  # staged
        _git(["add", _SETTINGS], wt)

    dirty = _dirty_paths(wt)
    assert dirty == [_SETTINGS]
    assert _diff_is_settings_only(dirty) is True


def test_classifier_empty_is_not_settings_only() -> None:
    # Adversarial default-branch: an empty dirty list must NOT classify as
    # settings-only (would otherwise skip a clean worktree spuriously).
    assert _diff_is_settings_only([]) is False


@pytest.mark.parametrize(
    "sibling",
    [".claude/settings.json.bak", "src/app/settings.json"],
)
def test_classifier_rejects_lookalike_siblings(sibling: str) -> None:
    # Over-eager path matching guard: a real file whose name merely *contains*
    # settings.json is NOT settings-only even alongside the planted file.
    assert _diff_is_settings_only([_SETTINGS, sibling]) is False
    assert _diff_is_settings_only([sibling]) is False


# ---------------------------------------------------------------------------
# Unit: rescue_uncommitted_work — skip / exclude / unchanged
# ---------------------------------------------------------------------------


def test_skip_when_only_settings_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: dict[str, list]
) -> None:
    wt = _init_worktree(tmp_path / "wt", with_remote=True)
    _patch_worktree(monkeypatch, wt)
    cfg = _config(tmp_path)
    _plant_settings(wt)  # only dirty path

    before = _head(wt)
    assert rescue_uncommitted_work(_outcome(), cfg) is None
    # No commit, no push, no PR.
    assert _head(wt) == before
    assert fake_gh["create_pull"] == []
    # Observable skip signal emitted for operators.
    kinds = [e.get("kind") for e in _read_events(cfg)]
    assert "rescue_skipped_settings_only" in kinds


def test_mixed_diff_rescues_and_excludes_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: dict[str, list]
) -> None:
    wt = _init_worktree(tmp_path / "wt", with_remote=True)
    _patch_worktree(monkeypatch, wt)
    cfg = _config(tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "foo.py").write_text("x = 1\n")
    _plant_settings(wt)

    before = _head(wt)
    url = rescue_uncommitted_work(_outcome(), cfg)
    assert url == "https://github.com/owner/repo/pull/1"
    assert _head(wt) != before  # a commit was made
    assert len(fake_gh["create_pull"]) == 1

    committed = _git(["show", "--name-only", "--pretty=format:", "HEAD"], wt).stdout
    files = {f for f in committed.splitlines() if f.strip()}
    assert "src/foo.py" in files
    assert _SETTINGS not in files  # planted file excluded from the rescue commit


def test_normal_rescue_unchanged_when_no_settings_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: dict[str, list]
) -> None:
    wt = _init_worktree(tmp_path / "wt", with_remote=True)
    _patch_worktree(monkeypatch, wt)
    cfg = _config(tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "foo.py").write_text("x = 1\n")

    url = rescue_uncommitted_work(_outcome(), cfg)
    assert url == "https://github.com/owner/repo/pull/1"
    committed = _git(["show", "--name-only", "--pretty=format:", "HEAD"], wt).stdout
    assert "src/foo.py" in {f for f in committed.splitlines() if f.strip()}
    # No spurious skip event on the normal path.
    kinds = [e.get("kind") for e in _read_events(cfg)]
    assert "rescue_skipped_settings_only" not in kinds


def test_lookalike_sibling_is_rescued_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: dict[str, list]
) -> None:
    # Sad-path: settings.json PLUS a lookalike sibling must be rescued, and the
    # sibling (a real change) must land in the commit.
    wt = _init_worktree(tmp_path / "wt", with_remote=True)
    _patch_worktree(monkeypatch, wt)
    cfg = _config(tmp_path)
    _plant_settings(wt)
    (wt / ".claude" / "settings.json.bak").write_text("real change\n")

    url = rescue_uncommitted_work(_outcome(), cfg)
    assert url == "https://github.com/owner/repo/pull/1"
    committed = _git(["show", "--name-only", "--pretty=format:", "HEAD"], wt).stdout
    files = {f for f in committed.splitlines() if f.strip()}
    assert ".claude/settings.json.bak" in files
    assert _SETTINGS not in files


# --------------------------------------------------------------------------- #
# #453 — rescue automerge must respect the risk gate
# --------------------------------------------------------------------------- #


def _write_tested_change(wt: Path) -> None:
    (wt / "src").mkdir(exist_ok=True)
    (wt / "src" / "foo.py").write_text("x = 1\n")
    (wt / "tests").mkdir(exist_ok=True)
    (wt / "tests" / "test_foo.py").write_text("def test_x():\n    assert True\n")


def test_risk_gated_issue_rescue_never_automerges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: dict[str, list]
) -> None:
    """#453: two live incidents — rescue PRs from risk:high issues
    auto-merged into a live-prod repo with zero review (the 'has_tests'
    signal was a Dockerfile text lint). A rescue from a risk-gated
    issue stops at PR-open like any worker PR."""
    wt = _init_worktree(tmp_path / "wt", with_remote=True)
    _patch_worktree(monkeypatch, wt)
    cfg = _config(tmp_path)
    _write_tested_change(wt)
    monkeypatch.setattr(
        gh_issues, "fetch_issue",
        lambda issue, repo=None: {"labels": [{"name": "risk:high"}]},
    )

    url = rescue_uncommitted_work(_outcome(), cfg)
    assert url is not None  # the PR still opens — work is preserved
    assert fake_gh["auto_merge"] == []  # but NEVER automerged
    kinds = [e.get("kind") for e in _read_events(cfg)]
    assert "rescue_automerge_blocked_risk_gate" in kinds


def test_ungated_issue_with_tests_still_automerges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: dict[str, list]
) -> None:
    wt = _init_worktree(tmp_path / "wt", with_remote=True)
    _patch_worktree(monkeypatch, wt)
    cfg = _config(tmp_path)
    _write_tested_change(wt)
    monkeypatch.setattr(
        gh_issues, "fetch_issue",
        lambda issue, repo=None: {"labels": [{"name": "backend"}]},
    )

    url = rescue_uncommitted_work(_outcome(), cfg)
    assert url is not None
    assert len(fake_gh["auto_merge"]) == 1  # existing behavior preserved


def test_label_fetch_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: dict[str, list]
) -> None:
    """Unknown risk = gated. Automerge is the dangerous path; an API
    hiccup must not open it."""
    wt = _init_worktree(tmp_path / "wt", with_remote=True)
    _patch_worktree(monkeypatch, wt)
    cfg = _config(tmp_path)
    _write_tested_change(wt)
    monkeypatch.setattr(gh_issues, "fetch_issue", lambda issue, repo=None: None)

    url = rescue_uncommitted_work(_outcome(), cfg)
    assert url is not None
    assert fake_gh["auto_merge"] == []
