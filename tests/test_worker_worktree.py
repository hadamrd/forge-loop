"""Tests for worker worktree preparation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge_loop.sandbox import (
    CapabilityPolicy,
    FilesystemScope,
    McpGrant,
    NetworkPolicy,
    policy_hash,
)
from forge_loop.worker import _prep_worktree
from forge_loop.worker_worktree import (
    plant_worker_settings,
    render_worker_settings,
    worktree_base,
    worktree_path,
)


def test_prep_worktree_uses_configured_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stderr = ""

    def fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr("forge_loop.worker_worktree.subprocess.run", fake_run)
    monkeypatch.setattr("forge_loop.worker_worktree.plant_worker_settings", lambda *_a, **_k: None)

    worktree, err = _prep_worktree(tmp_path, 12, "loop/12-demo", base_branch="main")

    assert err is None
    # Worktrees are namespaced per-repo (/tmp/forge-<repo>/wt-loop-<n>) so two
    # loops on different checkouts never collide or reap each other's worktrees.
    assert worktree == worktree_path(tmp_path, 12)
    assert [
        "git",
        "fetch",
        "--prune",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    ] in calls
    assert ["git", "worktree", "add", str(worktree), "-B", "loop/12-demo", "origin/main"] in calls


def test_prep_worktree_quarantines_undeletable_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry after uid-mismatch cleanup failure by quarantining stale worktrees."""
    import shutil as _real_shutil

    real_rmtree = _real_shutil.rmtree

    base = worktree_base(tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    blocking = worktree_path(tmp_path, 9999)
    for q in base.glob("wt-loop-9999*"):
        real_rmtree(q, ignore_errors=True)
    blocking.mkdir(exist_ok=True)
    (blocking / "marker").write_text("planted")

    class _Completed:
        returncode = 0
        stderr = ""

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        calls.append(cmd)
        return _Completed()

    def boom_rmtree(_path: str | Path) -> None:
        raise PermissionError("simulated: planted by another uid")

    monkeypatch.setattr("forge_loop.worker_worktree.subprocess.run", fake_run)
    monkeypatch.setattr("forge_loop.worker_worktree.shutil.rmtree", boom_rmtree)
    monkeypatch.setattr("forge_loop.worker_worktree.plant_worker_settings", lambda *_a, **_k: None)

    try:
        worktree, err = _prep_worktree(tmp_path, 9999, "loop/9999-demo")
        assert err is None
        assert any(cmd[:3] == ["git", "worktree", "add"] for cmd in calls), (
            f"worktree add was not called: {calls!r}"
        )
        assert not blocking.exists(), "blocking dir should have been quarantined"
        quarantined = sorted(
            base.glob("wt-loop-9999.stale-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        assert quarantined, "quarantine dir was not created"
        assert (quarantined[0] / "marker").read_text() == "planted"
    finally:
        for q in base.glob("wt-loop-9999*"):
            real_rmtree(q, ignore_errors=True)


def test_worktree_paths_are_namespaced_per_repo() -> None:
    """Two repos with the SAME issue number must not share a worktree path.

    Regression for the cross-project clash: under the old flat
    ``/tmp/wt-loop-<issue>`` scheme, repo A's #155 and repo B's #155
    collided, and the boot reaper's ``/tmp/wt-loop-*`` glob would reap the
    other loop's live worktrees.
    """
    repo_a = Path("/home/x/project-alpha")
    repo_b = Path("/home/x/project-beta")

    assert worktree_path(repo_a, 155) != worktree_path(repo_b, 155)
    # Each repo's reaper glob is confined to its own base, so it can never
    # match the other repo's worktrees.
    assert worktree_path(repo_b, 155).parent != worktree_base(repo_a)
    assert not str(worktree_path(repo_a, 155)).startswith(str(worktree_base(repo_b)))


# ---------------------------------------------------------------------------
# render_worker_settings — deny-by-default from the saga CapabilityPolicy (#200)
# ---------------------------------------------------------------------------


def _allow(settings_json: str) -> list[str]:
    return json.loads(settings_json)["permissions"]["allow"]


def test_render_settings_mcp_github_only_excludes_other_servers() -> None:
    """A github-only grant emits ``mcp__github__*`` and NOTHING for lumen.

    The effective MCP surface equals the lease: a server absent from
    ``policy.mcp`` must not appear, and there is never a blanket ``mcp__*``.
    """
    policy = CapabilityPolicy(mcp=(McpGrant(server="github", tools=("*",)),))
    out = render_worker_settings(policy)
    allow = _allow(out)

    assert "mcp__github__*" in allow
    assert "mcp__lumen__*" not in allow
    assert "mcp__*" not in allow
    assert not any(entry.startswith("mcp__lumen") for entry in allow)


def test_render_settings_mcp_tool_allowlist_is_not_wildcarded() -> None:
    """A tool allowlist emits ``mcp__lumen__search``, not ``mcp__lumen__*``."""
    policy = CapabilityPolicy(mcp=(McpGrant(server="lumen", tools=("search",)),))
    allow = _allow(render_worker_settings(policy))

    assert "mcp__lumen__search" in allow
    assert "mcp__lumen__*" not in allow


def test_render_settings_write_scoped_to_worktree_no_blanket() -> None:
    """``write_roots=(worktree,)`` yields no ``Write(*)``/``Edit(*)``.

    Write entries are scoped to the worktree path glob only — write outside
    the lease cannot be granted.
    """
    wt = "/tmp/forge-repo/wt-loop-200"
    policy = CapabilityPolicy(filesystem=FilesystemScope(write_roots=(wt,)))
    allow = _allow(render_worker_settings(policy))

    assert "Write(*)" not in allow
    assert "Edit(*)" not in allow
    assert f"Write({wt}/**)" in allow
    assert f"Edit({wt}/**)" in allow
    # every write entry stays under the worktree glob
    for entry in allow:
        if entry.startswith(("Write(", "Edit(")):
            assert entry.endswith(f"({wt}/**)"), entry


def test_render_settings_read_scoped_to_read_roots() -> None:
    """Read/Grep/Glob are scoped to ``read_roots``; never bare ``Read(*)``."""
    repo = "/tmp/forge-repo"
    wt = "/tmp/forge-repo/wt-loop-200"
    policy = CapabilityPolicy(filesystem=FilesystemScope(read_roots=(repo, wt)))
    allow = _allow(render_worker_settings(policy))

    assert "Read(*)" not in allow
    assert f"Read({repo}/**)" in allow
    assert f"Grep({wt}/**)" in allow
    assert f"Glob({repo}/**)" in allow


def test_render_settings_empty_policy_is_closed() -> None:
    """An empty ``CapabilityPolicy`` renders a CLOSED file — fail safe.

    deny-by-default: empty ``allow``, ``defaultMode`` is NOT
    ``bypassPermissions``. Never the old permissive blob.
    """
    out = render_worker_settings(CapabilityPolicy())
    parsed = json.loads(out)

    assert parsed["permissions"]["allow"] == []
    assert parsed["permissions"]["defaultMode"] != "bypassPermissions"
    assert "Bash(*)" not in out
    assert "mcp__*" not in out


def test_render_settings_no_write_root_grants_no_bash() -> None:
    """Adversarial: a read-only grant must NOT grant ``Bash(*)``.

    Bash is write-capable, so it is gated on the lease including write
    access. A grant with only ``read_roots`` gets no Bash entry.
    """
    policy = CapabilityPolicy(filesystem=FilesystemScope(read_roots=("/tmp/forge-repo",)))
    allow = _allow(render_worker_settings(policy))

    assert "Bash(*)" not in allow


def test_render_settings_is_valid_json() -> None:
    """Property-ish: output always parses and carries the trust flags."""
    policy = CapabilityPolicy(
        filesystem=FilesystemScope(read_roots=("/r",), write_roots=("/w",)),
        network=NetworkPolicy(allow_domains=("github.com",)),
        mcp=(McpGrant(server="github"), McpGrant(server="lumen", tools=("search", "index"))),
    )
    parsed = json.loads(render_worker_settings(policy))
    assert parsed["hasTrustDialogAccepted"] is True
    assert parsed["permissions"]["deny"] == []


def test_policy_hash_stable_and_sensitive() -> None:
    """``policy_hash`` is stable across equal policies, flips on a server change.

    Mirrors the acceptance-criteria hash matrix: two renders of an equal
    policy hash equal; swapping a granted server changes the digest.
    """
    a = CapabilityPolicy(mcp=(McpGrant(server="github", tools=("*",)),))
    a2 = CapabilityPolicy(mcp=(McpGrant(server="github", tools=("*",)),))
    b = CapabilityPolicy(mcp=(McpGrant(server="lumen", tools=("*",)),))

    assert policy_hash(a) == policy_hash(a2)
    assert policy_hash(a) != policy_hash(b)


def test_plant_worker_settings_is_read_only(tmp_path: Path) -> None:
    """Planted file is mode ``0o444`` and its ``.claude`` dir ``0o555``."""
    wt = tmp_path / "wt"
    wt.mkdir()
    policy = CapabilityPolicy(filesystem=FilesystemScope(write_roots=(str(wt),)))

    plant_worker_settings(wt, policy)

    settings = wt / ".claude" / "settings.json"
    cdir = wt / ".claude"
    assert (settings.stat().st_mode & 0o777) == 0o444
    assert (cdir.stat().st_mode & 0o777) == 0o555
    # content equals the rendered policy
    assert json.loads(settings.read_text()) == json.loads(render_worker_settings(policy))


def test_plant_worker_settings_none_policy_is_closed(tmp_path: Path) -> None:
    """Adversarial: ``None`` policy plants the CLOSED file, not the blob."""
    wt = tmp_path / "wt"
    wt.mkdir()

    plant_worker_settings(wt, None)

    parsed = json.loads((wt / ".claude" / "settings.json").read_text())
    assert parsed["permissions"]["allow"] == []
    assert parsed["permissions"]["defaultMode"] != "bypassPermissions"


def test_plant_worker_settings_emits_typed_event(tmp_path: Path) -> None:
    """A ``worker_policy_enforced`` event is appended via the typed emit() path.

    Boot/replay reads this to confirm each worker ran within its grant.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    events_file = tmp_path / "events.jsonl"
    policy = CapabilityPolicy(mcp=(McpGrant(server="github", tools=("*",)),))

    plant_worker_settings(wt, policy, events_file=events_file)

    lines = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
    enforced = [rec for rec in lines if rec.get("kind") == "worker_policy_enforced"]
    assert len(enforced) == 1
    assert enforced[0]["worktree_path"] == str(wt)
    assert enforced[0]["policy_hash"] == policy_hash(policy)


def test_plant_worker_settings_no_events_file_is_silent(tmp_path: Path) -> None:
    """Adversarial (T2): ``events_file=None`` plants settings but emits nothing."""
    wt = tmp_path / "wt"
    wt.mkdir()

    plant_worker_settings(wt, CapabilityPolicy(), events_file=None)

    assert (wt / ".claude" / "settings.json").exists()
    # no events file was created anywhere under the worktree
    assert not list(tmp_path.glob("*.jsonl"))
