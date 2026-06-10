"""Tests for worker worktree preparation."""

from __future__ import annotations

import subprocess

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


def test_policy_hash_covers_network_dimension() -> None:
    """AC5 (#282): the attestation hash already covers ``network``.

    ``CapabilityPolicy.to_json_obj`` serialises the network dimension, so
    ``policy_hash`` flips when ``allow_domains`` changes and is stable when it
    does not — no second hash is fabricated.
    """
    base = CapabilityPolicy(network=NetworkPolicy(allow_domains=("github.com",)))
    same = CapabilityPolicy(network=NetworkPolicy(allow_domains=("github.com",)))
    widened = CapabilityPolicy(
        network=NetworkPolicy(allow_domains=("github.com", "evil.example.com"))
    )

    # network IS serialised into the canonical JSON that the hash covers.
    assert base.to_json_obj()["network"]["allow_domains"] == ["github.com"]

    assert policy_hash(base) == policy_hash(same)
    assert policy_hash(base) != policy_hash(widened)


def test_policy_hash_flips_on_deny_by_default_flag() -> None:
    """The deny_by_default flag is load-bearing and must move the digest."""
    closed = CapabilityPolicy(network=NetworkPolicy(allow_domains=(), deny_by_default=True))
    open_default = CapabilityPolicy(network=NetworkPolicy(allow_domains=(), deny_by_default=False))
    assert policy_hash(closed) != policy_hash(open_default)


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


def test_policy_hash_sensitive_to_secret_names() -> None:
    """``policy_hash`` already differs when ``secret_names`` differs (#283).

    The secret dimension is in ``to_json_obj``; the hash must reflect it so a
    re-leased secret set flips the attestation. Do NOT re-derive the hash —
    just assert the existing canonicalisation covers the dimension.
    """
    a = CapabilityPolicy(secret_names=("GITHUB_TOKEN",))
    a2 = CapabilityPolicy(secret_names=("GITHUB_TOKEN",))
    b = CapabilityPolicy(secret_names=("GITHUB_TOKEN", "ANTHROPIC_API_KEY"))
    none = CapabilityPolicy()

    assert policy_hash(a) == policy_hash(a2)
    assert policy_hash(a) != policy_hash(b)
    assert policy_hash(a) != policy_hash(none)
    # Round-trip preserves the dimension and thus the hash.
    assert policy_hash(CapabilityPolicy.from_json_obj(a.to_json_obj())) == policy_hash(a)


def test_plant_worker_settings_records_withheld_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attestation carries the withheld secret NAMES (never values) (#283)."""
    monkeypatch.setattr(
        "forge_loop.worker_worktree.os.environ",
        {
            "GITHUB_TOKEN": "gh",
            "ANTHROPIC_API_KEY": "sk",
            "PATH": "/usr/bin",
        },
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    events_file = tmp_path / "events.jsonl"
    policy = CapabilityPolicy(secret_names=("GITHUB_TOKEN",))

    plant_worker_settings(wt, policy, events_file=events_file)

    lines = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
    enforced = [rec for rec in lines if rec.get("kind") == "worker_policy_enforced"]
    assert len(enforced) == 1
    # Only the UNLEASED secret-shaped key is recorded — by name, never value.
    assert enforced[0]["withheld_secrets"] == ["ANTHROPIC_API_KEY"]
    assert "sk" not in json.dumps(enforced[0])
    assert enforced[0]["policy_hash"] == policy_hash(policy)


def test_plant_worker_settings_none_policy_withholds_all_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: a ``None`` lease withholds (and records) ALL secret keys."""
    monkeypatch.setattr(
        "forge_loop.worker_worktree.os.environ",
        {"GITHUB_TOKEN": "gh", "DB_PASSWORD": "p", "PATH": "/usr/bin"},
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    events_file = tmp_path / "events.jsonl"

    plant_worker_settings(wt, None, events_file=events_file)

    lines = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
    enforced = [rec for rec in lines if rec.get("kind") == "worker_policy_enforced"]
    assert enforced[0]["withheld_secrets"] == ["DB_PASSWORD", "GITHUB_TOKEN"]


class TestPlantShieldedFromGit:
    """#449 — the planted settings file must never reach a worker commit.

    A getadaptiq worker committed the plant over the operator's TRACKED
    .claude/settings.json (hooks + permissions destroyed on merge). The
    plant now shields the path at the git level, so even `git add -A`
    inside the worker session stages nothing for it."""

    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        run = lambda *a: subprocess.run(  # noqa: E731
            ["git", *a], cwd=repo, capture_output=True, text=True, check=True
        )
        run("init", "-b", "main")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        return repo, run

    def test_tracked_settings_overwrite_invisible_to_git(self, tmp_path):
        repo, run = self._repo(tmp_path)
        cdir = repo / ".claude"
        cdir.mkdir()
        (cdir / "settings.json").write_text('{"operator": "hooks"}')
        (repo / "README.md").write_text("x")
        run("add", "-A")
        run("commit", "-m", "operator config")

        plant_worker_settings(repo, None)

        run2 = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert ".claude/settings.json" not in run2.stdout
        # and a worker-style add-all stages nothing for it
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert ".claude/settings.json" not in staged.stdout

    def test_untracked_plant_excluded_from_add_all(self, tmp_path):
        repo, run = self._repo(tmp_path)
        (repo / "README.md").write_text("x")
        run("add", "-A")
        run("commit", "-m", "init")

        plant_worker_settings(repo, None)

        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert ".claude/settings.json" not in staged.stdout
