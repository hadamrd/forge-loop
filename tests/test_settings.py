"""Tests for :mod:`forge_loop.settings` (issue #84).

Covers the precedence matrix (env > yaml > defaults), validation errors,
the YAML round-trip path for ``forge-loop config``, and the architectural
regression that no production module outside ``settings.py`` reads
``os.environ`` for ``LOOP_*`` knobs directly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from forge_loop.settings import (
    DEFAULT_ALLOWED_MCP_SERVERS,
    ConfigError,
    Settings,
)


@pytest.fixture(autouse=True)
def _clear_loop_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any LOOP_* / FORGE_LOOP_* env from the host so tests start
    from a clean slate. Without this, the operator's shell env (e.g.
    LOOP_GH_REPO from a real run) leaks into every test case."""
    for k in list(os.environ):
        if k.startswith(("LOOP_", "FORGE_LOOP_")):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("forge_loop.settings._repo_root", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Precedence matrix — the contract is uniform: env > yaml > defaults.
# ---------------------------------------------------------------------------


def test_defaults_apply_when_no_yaml_no_env(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    s = Settings.load()
    assert s.scheduling.parallel == 3
    assert s.scheduling.tick_interval_s == 60
    assert s.worker.model == "claude-opus-4-7"
    assert s.worker.thinking == "medium"
    assert s.critic.enabled is True
    assert s.po.enabled is True
    assert s.iteration.max_iterations == 3
    assert s.misc.events_rotate_bytes == 0


def test_yaml_overrides_defaults(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (fake_repo / "forge-loop.yaml").write_text(dedent("""
        repo:
          github: owner/from-yaml
        scheduling:
          parallel: 7
          tick_interval_s: 90
        worker:
          model: claude-sonnet-4-6
          thinking: low
        iteration:
          max_iterations: 5
    """))
    s = Settings.load()
    assert s.repo.github == "owner/from-yaml"
    assert s.scheduling.parallel == 7
    assert s.scheduling.tick_interval_s == 90
    assert s.worker.model == "claude-sonnet-4-6"
    assert s.worker.thinking == "low"
    assert s.iteration.max_iterations == 5


def test_env_overrides_yaml_overrides_defaults(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline precedence guarantee — env beats yaml beats defaults."""
    (fake_repo / "forge-loop.yaml").write_text(dedent("""
        repo:
          github: owner/from-yaml
        scheduling:
          parallel: 7
        worker:
          model: claude-sonnet-4-6
    """))
    monkeypatch.setenv("LOOP_GH_REPO", "owner/from-env")
    monkeypatch.setenv("LOOP_PARALLEL", "13")
    monkeypatch.setenv("LOOP_WORKER_MODEL", "claude-haiku-4-5")

    s = Settings.load()
    assert s.repo.github == "owner/from-env"  # env beats yaml
    assert s.scheduling.parallel == 13  # env beats yaml
    assert s.worker.model == "claude-haiku-4-5"  # env beats yaml
    # tick_interval_s neither in env nor yaml → default
    assert s.scheduling.tick_interval_s == 60


# ---------------------------------------------------------------------------
# Validation — invalid values raise ConfigError with the field name.
# ---------------------------------------------------------------------------


def test_invalid_thinking_value_raises_with_field_name(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    monkeypatch.setenv("LOOP_WORKER_THINKING", "ultra")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load()
    msg = str(excinfo.value)
    assert "worker.thinking" in msg
    assert "ultra" in msg


def test_invalid_provider_raises_with_field_name(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    monkeypatch.setenv("LOOP_WORKER_PROVIDER", "gemini")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load()
    assert "worker.provider" in str(excinfo.value)


def test_invalid_int_value_raises_with_field_name(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    monkeypatch.setenv("LOOP_PARALLEL", "not-a-number")
    with pytest.raises(ConfigError) as excinfo:
        Settings.load()
    assert "LOOP_PARALLEL" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Codex provider — model field defaults to "" (CLI default) when provider
# is codex AND no model was supplied. Without this, the claude default
# would leak through and the role would dispatch a Claude name to Codex.
# ---------------------------------------------------------------------------


def test_codex_provider_no_model_defaults_to_empty(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    (fake_repo / "forge-loop.yaml").write_text(dedent("""
        agent:
          provider: codex
    """))
    s = Settings.load()
    assert s.worker.provider == "codex"
    assert s.worker.model == ""


# ---------------------------------------------------------------------------
# YAML round-trip — `forge-loop config` writes yaml that loads back the
# same. Pin this so a future field addition doesn't break operator dumps.
# ---------------------------------------------------------------------------


def test_dump_yaml_round_trips(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/round-trip")
    monkeypatch.setenv("LOOP_PARALLEL", "5")
    s1 = Settings.load()
    rendered = s1.dump_yaml()

    # Write the rendered yaml as the new config file, drop all env, reload.
    (fake_repo / "forge-loop.yaml").write_text(rendered)
    for k in list(os.environ):
        if k.startswith(("LOOP_", "FORGE_LOOP_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOOP_GH_REPO", "owner/round-trip")
    s2 = Settings.load()

    assert s2.scheduling.parallel == 5
    assert s2.repo.github == "owner/round-trip"
    assert s2.worker.model == s1.worker.model
    assert s2.critic.model == s1.critic.model


# ---------------------------------------------------------------------------
# Bool coercion — yaml 1.1 parses `off` as False; the loader must coerce
# that back to the string the thinking-budget validator expects.
# ---------------------------------------------------------------------------


def test_yaml_off_thinking_is_coerced_to_off(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    (fake_repo / "forge-loop.yaml").write_text(dedent("""
        critic:
          thinking: off
    """))
    s = Settings.load()
    assert s.critic.thinking == "off"


# ---------------------------------------------------------------------------
# Allowed-MCP-tools — empty value falls back to bundled default, never
# an empty list (which would break worker brief rendering).
# ---------------------------------------------------------------------------


def test_allowed_mcp_tools_empty_env_falls_back(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    monkeypatch.setenv("LOOP_WORKER_ALLOWED_MCP_TOOLS", "")
    s = Settings.load()
    assert s.worker.allowed_mcp_tools == DEFAULT_ALLOWED_MCP_SERVERS


def test_allowed_mcp_tools_csv_env_parses(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    monkeypatch.setenv("LOOP_WORKER_ALLOWED_MCP_TOOLS", "lumen, custom-mcp")
    s = Settings.load()
    assert s.worker.allowed_mcp_tools == ("lumen", "custom-mcp")


# ---------------------------------------------------------------------------
# Architectural regression — every LOOP_*/FORGE_LOOP_* env knob lives in
# settings.py's ENV_MAP. Production code outside settings.py + config.py
# should not be reading os.environ for these prefixes (a few legitimate
# exceptions are listed in the allowlist).
# ---------------------------------------------------------------------------


_ALLOWED_LEGACY_SITES = {
    # log.py reads FORGE_LOOP_LOG_JSON at every TTY-check (called from
    # configure_logging which runs once at process boot). Could be moved
    # into Settings but the override is exactly one bool and the call
    # sits before Settings is loadable (Settings imports trigger logging),
    # so the dependency would invert. Justified standalone.
    "src/forge_loop/log.py",
    # Dynamic env-var name (computed at call time, not a fixed knob).
    "src/forge_loop/briefs/__init__.py",
    "src/forge_loop/mcp_server.py",  # per-tool LOOP_MCP_CAP_<TOOL> + ENV pass-through
    # Observability extras live behind FORGE_LOOP_EXPERIMENTAL gate and read
    # their own LOOP_PROM_* / LOOP_OTEL_* knobs — out of scope for #84's
    # core cleanup; will be folded in when observability stabilises.
    "src/forge_loop/observability/__init__.py",
    # Async orchestrator (replay) uses dynamic env var keys passed in.
    "src/forge_loop/runner_async.py",
    # TUI-force opt-in for tests is FORGE_LOOP_TUI_FORCE — a single op-mode
    # knob that doesn't fit the Settings tree shape.
    "src/forge_loop/cli_tui.py",
}


def test_no_loop_env_reads_outside_settings() -> None:
    """All canonical LOOP_* knobs must route through forge_loop.settings.

    Catches regressions where a new feature reaches for ``os.environ.get``
    instead of adding the knob to Settings + ENV_MAP. The allowlist above
    pins the few legit exceptions (dynamic keys, experimental extras).
    """
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "src" / "forge_loop"
    proc = subprocess.run(
        ["grep", "-rln", "os.environ.get", str(src)],
        capture_output=True,
        text=True,
        check=False,
    )
    hits = {
        str(Path(p).relative_to(repo_root))
        for p in proc.stdout.strip().splitlines()
        if p.strip() and not p.endswith((".pyc", ".pyo"))
    }
    # settings.py + config.py are allowed by definition.
    hits.discard("src/forge_loop/settings.py")
    hits.discard("src/forge_loop/config.py")
    unexpected = hits - _ALLOWED_LEGACY_SITES
    assert not unexpected, (
        f"Unexpected os.environ.get sites outside settings.py:\n"
        f"  {sorted(unexpected)}\n"
        "Add the knob to Settings + ENV_MAP, or extend _ALLOWED_LEGACY_SITES "
        "with a comment justifying why it's dynamic."
    )
