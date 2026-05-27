"""Tests for the multirepo loader, disable flag, and tick driver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_loop.config import Config
from forge_loop.multirepo import (
    RepoLoadError,
    RepoSpec,
    build_config_for_repo,
    disable_repo,
    enable_repo,
    is_disabled,
    load_repos,
    validate_checkout,
)
from forge_loop.multirepo.runner import MultirepoRunState, run_multirepo_tick


def _write_repo_yaml(
    dir_: Path,
    name: str,
    *,
    github: str | None = None,
    checkout: str | None = None,
    extra: dict | None = None,
) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "github": github or f"org/{name}",
        "checkout": checkout or str(dir_ / "checkouts" / name),
        "labels": {"ready": "loop:ready", "blocked": "loop:blocked"},
        "budget_usd_per_day": 50,
    }
    if extra:
        payload.update(extra)
    import yaml
    p = dir_ / f"{name}.yaml"
    p.write_text(yaml.safe_dump(payload))
    return p


def _make_fake_git_checkout(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Loader unit tests
# ---------------------------------------------------------------------------

def test_load_repos_happy_path_three_repos(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    for n in ["alpha", "beta", "gamma"]:
        _write_repo_yaml(repos_dir, n)
    specs = load_repos(repos_dir)
    assert [s.name for s in specs] == ["alpha", "beta", "gamma"]
    assert all(s.github.startswith("org/") for s in specs)
    assert all(isinstance(s, RepoSpec) for s in specs)
    assert all(s.budget_usd_per_day == 50.0 for s in specs)
    assert specs[0].source_path is not None
    assert specs[0].labels.ready == "loop:ready"


def test_load_repos_missing_directory_returns_empty(tmp_path: Path) -> None:
    assert load_repos(tmp_path / "does-not-exist") == []


def test_load_repos_rejects_missing_required_fields(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    repos_dir.mkdir(parents=True)
    (repos_dir / "bad.yaml").write_text("name: x\n")  # no github, no checkout
    with pytest.raises(RepoLoadError):
        load_repos(repos_dir)


def test_load_repos_rejects_duplicate_names(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    _write_repo_yaml(repos_dir, "dup")
    # second file, same `name:`, different filename
    import yaml
    (repos_dir / "dup2.yaml").write_text(yaml.safe_dump({
        "name": "dup", "github": "org/dup",
        "checkout": str(tmp_path / "elsewhere"),
    }))
    with pytest.raises(RepoLoadError, match="duplicate"):
        load_repos(repos_dir)


def test_load_repos_rejects_malformed_github(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    repos_dir.mkdir(parents=True)
    (repos_dir / "weird.yaml").write_text(
        "name: weird\ngithub: not-a-slug\ncheckout: /tmp/x\n"
    )
    with pytest.raises(RepoLoadError, match="owner/repo"):
        load_repos(repos_dir)


# ---------------------------------------------------------------------------
# Enable / disable flag
# ---------------------------------------------------------------------------

def test_disable_then_enable_roundtrip(tmp_path: Path) -> None:
    checkout = _make_fake_git_checkout(tmp_path / "co")
    spec = RepoSpec(name="x", github="o/x", checkout=checkout)
    assert not is_disabled(spec)
    flag = disable_repo(spec, reason="manual hold")
    assert flag.exists()
    assert is_disabled(spec)
    assert "manual hold" in flag.read_text()
    # idempotent re-disable
    disable_repo(spec, reason="again")
    assert is_disabled(spec)
    # enable clears
    cleared = enable_repo(spec)
    assert cleared
    assert not is_disabled(spec)
    # enable on already-enabled is a no-op
    assert enable_repo(spec) is False


# ---------------------------------------------------------------------------
# Checkout validation
# ---------------------------------------------------------------------------

def test_validate_checkout_detects_missing(tmp_path: Path) -> None:
    spec = RepoSpec(name="ghost", github="o/x", checkout=tmp_path / "nope")
    assert "missing" in (validate_checkout(spec) or "")


def test_validate_checkout_detects_non_git_dir(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    spec = RepoSpec(name="plain", github="o/x", checkout=tmp_path / "plain")
    assert "not a git repo" in (validate_checkout(spec) or "")


def test_validate_checkout_passes_on_good_repo(tmp_path: Path) -> None:
    co = _make_fake_git_checkout(tmp_path / "ok")
    spec = RepoSpec(name="ok", github="o/x", checkout=co)
    assert validate_checkout(spec) is None


# ---------------------------------------------------------------------------
# build_config_for_repo
# ---------------------------------------------------------------------------

def test_build_config_uses_template_scheduling_and_spec_labels(tmp_path: Path) -> None:
    from forge_loop.config import Labels
    co = _make_fake_git_checkout(tmp_path / "ck")
    spec = RepoSpec(
        name="a", github="o/a", checkout=co,
        labels=Labels(ready="ready-a"),
    )
    template = Config(repo=tmp_path, github_repo="ignored/here",
                      parallel=7, tick_interval_s=120)
    cfg = build_config_for_repo(spec, template=template)
    assert cfg.repo == co
    assert cfg.github_repo == "o/a"
    assert cfg.parallel == 7
    assert cfg.tick_interval_s == 120
    assert cfg.labels.ready == "ready-a"
    # state_dir lives under THIS repo's checkout, not the template's
    assert cfg.state_dir == co / "docs" / "ops"


# ---------------------------------------------------------------------------
# run_multirepo_tick — disable + invalid checkout + iteration
# ---------------------------------------------------------------------------

def test_run_multirepo_tick_iterates_all_enabled_repos(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    for n in ["one", "two"]:
        co = _make_fake_git_checkout(tmp_path / "co" / n)
        _write_repo_yaml(repos_dir, n, checkout=str(co))
    specs = load_repos(repos_dir)
    state = MultirepoRunState()
    events = tmp_path / "events.jsonl"

    ticked: list[str] = []

    def fake_tick(cfg: Config, tick: int) -> None:
        ticked.append(cfg.github_repo or "?")
        # emit a per-repo event so the integration check below works
        (cfg.state_dir).mkdir(parents=True, exist_ok=True)
        with open(cfg.events_file, "a") as f:
            f.write(json.dumps({"kind": "tick_start", "tick": tick}) + "\n")

    run_multirepo_tick(specs, 1, state=state, template=None,
                       events_file=events, tick_fn=fake_tick)
    assert ticked == ["org/one", "org/two"]
    # Both repos got their own per-repo events file populated
    for n in ["one", "two"]:
        per_repo = tmp_path / "co" / n / "docs" / "ops" / "loop-runner-events.jsonl"
        assert per_repo.exists()
        body = per_repo.read_text()
        assert "tick_start" in body
    # Sidecar log records the orchestration trail
    sidecar = events.read_text()
    assert "multirepo_tick_start" in sidecar
    assert "repo_tick_done" in sidecar


def test_run_multirepo_tick_skips_disabled_repo_but_continues(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    co_a = _make_fake_git_checkout(tmp_path / "co" / "a")
    co_b = _make_fake_git_checkout(tmp_path / "co" / "b")
    _write_repo_yaml(repos_dir, "a", checkout=str(co_a))
    _write_repo_yaml(repos_dir, "b", checkout=str(co_b))
    specs = load_repos(repos_dir)
    # Disable 'a'
    disable_repo(specs[0], reason="under-maintenance")
    assert is_disabled(specs[0])

    ticked: list[str] = []

    def fake_tick(cfg: Config, tick: int) -> None:
        ticked.append(cfg.github_repo or "?")

    state = MultirepoRunState()
    events = tmp_path / "events.jsonl"
    run_multirepo_tick(specs, 1, state=state, template=None,
                       events_file=events, tick_fn=fake_tick)

    # 'a' was skipped; 'b' still ran
    assert ticked == ["org/b"]
    body = events.read_text()
    assert '"repo": "a"' in body and '"reason": "disabled"' in body
    assert state.activity["a"].last_action == "skipped_disabled"
    assert state.activity["b"].last_action == "ticked"


def test_run_multirepo_tick_adversarial_missing_checkout(tmp_path: Path) -> None:
    """A repo with a checkout that doesn't exist on disk must NOT abort
    the tick — surface a clear reason, skip the bad repo, run the rest.
    """
    repos_dir = tmp_path / ".forge" / "repos"
    _write_repo_yaml(repos_dir, "ghost",
                     checkout=str(tmp_path / "this-path-does-not-exist"))
    co_ok = _make_fake_git_checkout(tmp_path / "co" / "ok")
    _write_repo_yaml(repos_dir, "ok", checkout=str(co_ok))
    specs = load_repos(repos_dir)

    ticked: list[str] = []

    def fake_tick(cfg: Config, tick: int) -> None:
        ticked.append(cfg.github_repo or "?")

    state = MultirepoRunState()
    events = tmp_path / "events.jsonl"
    run_multirepo_tick(specs, 1, state=state, template=None,
                       events_file=events, tick_fn=fake_tick)

    assert ticked == ["org/ok"]
    body = events.read_text()
    # The bad repo's skip is reported with a useful diagnostic
    assert "checkout_invalid" in body
    assert "ghost" in body
    assert state.activity["ghost"].last_action == "skipped_invalid"
    assert "missing" in state.activity["ghost"].last_reason


def test_run_multirepo_tick_continues_when_tick_fn_raises(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    co_a = _make_fake_git_checkout(tmp_path / "co" / "a")
    co_b = _make_fake_git_checkout(tmp_path / "co" / "b")
    _write_repo_yaml(repos_dir, "a", checkout=str(co_a))
    _write_repo_yaml(repos_dir, "b", checkout=str(co_b))
    specs = load_repos(repos_dir)

    seen: list[str] = []

    def flaky_tick(cfg: Config, tick: int) -> None:
        seen.append(cfg.github_repo or "?")
        if cfg.github_repo == "org/a":
            raise RuntimeError("simulated crash")

    state = MultirepoRunState()
    events = tmp_path / "events.jsonl"
    run_multirepo_tick(specs, 1, state=state, template=None,
                       events_file=events, tick_fn=flaky_tick)
    # The error in 'a' did not stop 'b'
    assert seen == ["org/a", "org/b"]
    body = events.read_text()
    assert "repo_tick_error" in body


# ---------------------------------------------------------------------------
# CLI smoke: `repos list` returns a structured JSON listing
# ---------------------------------------------------------------------------

def test_cli_repos_list_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from forge_loop.cli import main

    repos_dir = tmp_path / ".forge" / "repos"
    co_a = _make_fake_git_checkout(tmp_path / "co" / "a")
    _write_repo_yaml(repos_dir, "a", checkout=str(co_a))
    # one disabled
    co_b = _make_fake_git_checkout(tmp_path / "co" / "b")
    _write_repo_yaml(repos_dir, "b", checkout=str(co_b))
    specs = load_repos(repos_dir)
    disable_repo(specs[1])

    rc = main(["repos", "list", "--repos-dir", str(repos_dir), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    names = {r["name"]: r for r in out["repos"]}
    assert set(names) == {"a", "b"}
    assert names["a"]["disabled"] is False
    assert names["b"]["disabled"] is True
    assert names["a"]["checkout_invalid"] is None


def test_cli_repos_disable_and_enable(tmp_path: Path) -> None:
    from forge_loop.cli import main

    repos_dir = tmp_path / ".forge" / "repos"
    co = _make_fake_git_checkout(tmp_path / "co" / "x")
    _write_repo_yaml(repos_dir, "x", checkout=str(co))
    rc = main(["repos", "disable", "x", "--repos-dir", str(repos_dir),
               "--reason", "needs-rebase"])
    assert rc == 0
    assert (co / ".forge" / "disabled").exists()
    rc = main(["repos", "enable", "x", "--repos-dir", str(repos_dir)])
    assert rc == 0
    assert not (co / ".forge" / "disabled").exists()


def test_cli_repos_disable_unknown_name(tmp_path: Path) -> None:
    from forge_loop.cli import main

    repos_dir = tmp_path / ".forge" / "repos"
    co = _make_fake_git_checkout(tmp_path / "co" / "real")
    _write_repo_yaml(repos_dir, "real", checkout=str(co))
    rc = main(["repos", "disable", "nope", "--repos-dir", str(repos_dir)])
    assert rc == 2
