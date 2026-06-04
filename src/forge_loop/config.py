"""Backwards-compat Config view built from :mod:`forge_loop.settings` (issue #84).

The single source of truth for runtime config is now :class:`Settings` in
``settings.py``. This module preserves the historical :class:`Config`
dataclass shape so the ~80 call sites that read ``cfg.worker.model`` /
``cfg.critic.timeout_s`` / etc. keep working unchanged.

New code should prefer ``from forge_loop.settings import get_settings``
and read ``get_settings().<group>.<field>`` directly. Loader-side
validation now raises :class:`ConfigError` (alias of
:class:`forge_loop.settings.ConfigError`) at startup, not mid-tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Re-export so legacy importers (`from forge_loop.config import
# ModelConfigError`) keep working. ModelConfigError used to be the
# validation type; ConfigError supersedes it but we alias for compat.
from forge_loop import settings as _settings_mod
from forge_loop.settings import (
    DEFAULT_ALLOWED_MCP_SERVERS,
    ConfigError,
    Settings,
)

ModelConfigError = ConfigError

# Default heartbeat interval (seconds). Mirrors runner.dispatch's
# _HEARTBEAT_INTERVAL_S; kept here so the field default is documented
# at its source-of-truth.
_DEFAULT_HEARTBEAT_INTERVAL_S = 60.0

# Re-export legacy private helpers so test monkeypatches keep targeting
# ``config._repo_root`` / ``config._yaml_config_path`` etc. The Settings
# loader reads these via the module attribute, so swapping them here
# affects ``Settings.load()`` too.
_repo_root = _settings_mod._repo_root
_yaml_config_path = _settings_mod._yaml_config_path
_load_yaml = _settings_mod._load_yaml
_parse_mcp_server_list = _settings_mod._coerce_str_tuple


def _resolve_allowed_mcp_tools(worker_block: dict[str, Any]) -> tuple[str, ...]:
    """Back-compat shim used by tests + a few legacy callers.

    The canonical resolution lives inside :class:`WorkerSettings`; this
    wrapper preserves the function signature that test_worker_mcp_filter
    + a couple of older brief renderers still import.
    """
    import os as _os

    raw_env = _os.environ.get("LOOP_WORKER_ALLOWED_MCP_TOOLS")
    if raw_env is not None:
        parsed = _settings_mod._coerce_str_tuple(raw_env)
        return parsed or DEFAULT_ALLOWED_MCP_SERVERS
    if "allowed_mcp_tools" in worker_block:
        parsed = _settings_mod._coerce_str_tuple(worker_block["allowed_mcp_tools"])
        return parsed or DEFAULT_ALLOWED_MCP_SERVERS
    return DEFAULT_ALLOWED_MCP_SERVERS


@dataclass(frozen=True)
class Briefs:
    worker_preamble: str | None = None
    maintenance: str | None = None


@dataclass(frozen=True)
class Labels:
    ready: str = "loop:ready"
    triage: str = "loop:triage"
    blocked: str = "loop:blocked"
    risk_gate: str = "risk:high"


@dataclass(frozen=True)
class CriticConfig:
    enabled: bool = True
    timeout_s: int = 600
    block_on_sev2: bool = False
    min_findings_for_approve: int = 50
    model: str = "claude-sonnet-4-6"
    thinking: str = "off"
    provider: str = "claude"


@dataclass(frozen=True)
class POConfig:
    enabled: bool = True
    timeout_s: int = 480
    max_to_expand_per_tick: int = 2
    model: str = "claude-opus-4-7"
    thinking: str = "high"
    provider: str = "claude"


@dataclass(frozen=True)
class WorkerConfig:
    model: str = "claude-opus-4-7"
    thinking: str = "medium"
    provider: str = "claude"
    allowed_mcp_tools: tuple[str, ...] = DEFAULT_ALLOWED_MCP_SERVERS
    rescue_format_cmd: str = ""
    load_timeout_ms: int = 180000
    strict_mcp_config: bool = True
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    permissions: str = "full"


@dataclass(frozen=True)
class AttemptsConfig:
    enabled: bool = True
    max_history_in_brief: int = 5


@dataclass(frozen=True)
class LumenConfig:
    top_k: int = 3


@dataclass(frozen=True)
class Config:
    repo: Path
    github_repo: str | None = None
    base_branch: str = "trunk"
    coauthor: str = ""
    lumen_test_pattern: str = "**/*Test.*"
    worktree_root: Path = field(default_factory=lambda: Path("/tmp"))

    parallel: int = 3
    tick_interval_s: int = 60
    max_ticks: int = 0
    worker_timeout_s: int = 7200
    # Interval (seconds) at which the background heartbeat thread renews
    # each worker's task-saga lease (see runner.dispatch). The lease TTL
    # is derived as interval * _HEARTBEAT_LEASE_FACTOR. Sourced (env >
    # yaml > default) from LOOP_WORKER_HEARTBEAT_INTERVAL_S /
    # ``scheduling.worker_heartbeat_interval_s``.
    worker_heartbeat_interval_s: float = 60.0
    maintenance_every_n_ticks: int = 0

    deploy_task: str = ""

    labels: Labels = field(default_factory=Labels)
    briefs: Briefs = field(default_factory=Briefs)
    critic: CriticConfig = field(default_factory=CriticConfig)
    po: POConfig = field(default_factory=POConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    attempts: AttemptsConfig = field(default_factory=AttemptsConfig)
    lumen: LumenConfig = field(default_factory=LumenConfig)

    worker_max_iterations: int = 3

    # Stuck-issue sweep (issue #129). Minimum consecutive
    # ``worker_iterations_exhausted`` events before the per-tick sweep
    # demotes the issue from ``loop:ready`` to ``loop:needs-human``.
    # Sourced from settings.maintenance.stuck_threshold_attempts.
    stuck_threshold_attempts: int = 2
    stuck_tail_events: int = 100

    @property
    def state_dir(self) -> Path:
        return self.repo / "docs" / "ops"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "loop-runner.json"

    @property
    def events_file(self) -> Path:
        return self.state_dir / "loop-runner-events.jsonl"

    @property
    def summaries_file(self) -> Path:
        return self.state_dir / "loop-runner-summaries.jsonl"

    @property
    def pause_file(self) -> Path:
        return self.state_dir / "loop-runner.pause"

    @property
    def stop_file(self) -> Path:
        return self.state_dir / "loop-runner.stop"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "loop-runner.pid"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "loop-runner-logs"


def _resolve_heartbeat_interval_s() -> float:
    """Resolve the worker-heartbeat interval with env > yaml > default.

    Mirrors the Settings loader's precedence for neighbouring scheduling
    knobs (e.g. ``worker_timeout_s``). The value lives under the YAML
    ``scheduling`` block and the ``LOOP_WORKER_HEARTBEAT_INTERVAL_S`` env
    var; both are optional and fall back to ``_DEFAULT_HEARTBEAT_INTERVAL_S``.
    """
    import os as _os

    raw_env = _os.environ.get("LOOP_WORKER_HEARTBEAT_INTERVAL_S")
    if raw_env is not None and raw_env != "":
        try:
            return float(raw_env)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"LOOP_WORKER_HEARTBEAT_INTERVAL_S={raw_env!r}: {e}") from e

    # Resolve via the live settings-module attributes (not the import-time
    # aliases) so test monkeypatches of ``settings._repo_root`` — which the
    # Settings loader also honours — apply here too.
    repo = _settings_mod._repo_root()
    if path := _settings_mod._yaml_config_path(repo):
        scheduling = _settings_mod._load_yaml(path).get("scheduling") or {}
        if "worker_heartbeat_interval_s" in scheduling:
            try:
                return float(scheduling["worker_heartbeat_interval_s"])
            except (TypeError, ValueError) as e:
                raise ConfigError(
                    f"scheduling.worker_heartbeat_interval_s="
                    f"{scheduling['worker_heartbeat_interval_s']!r}: {e}"
                ) from e

    return _DEFAULT_HEARTBEAT_INTERVAL_S


def _from_settings(s: Settings) -> Config:
    """Materialise the legacy Config dataclass from a Settings instance.

    Mechanical translation — every field maps 1:1 onto the corresponding
    Settings group. The frozen dataclass is what the rest of the runner
    consumes; Settings is the source-of-truth loader.
    """
    if not s.repo.github:
        raise ConfigError(
            "github_repo not configured: set LOOP_GH_REPO env var or the "
            "`repo.github` field in your config YAML (e.g. owner/repo)"
        )
    return Config(
        repo=s.repo_path,
        github_repo=s.repo.github,
        base_branch=s.repo.base_branch,
        coauthor=s.misc.coauthor,
        lumen_test_pattern=s.lumen.test_pattern,
        worktree_root=s.repo.worktree_root,
        parallel=s.scheduling.parallel,
        tick_interval_s=s.scheduling.tick_interval_s,
        max_ticks=s.scheduling.max_ticks,
        worker_timeout_s=s.scheduling.worker_timeout_s,
        worker_heartbeat_interval_s=_resolve_heartbeat_interval_s(),
        maintenance_every_n_ticks=s.scheduling.maintenance_every_n_ticks,
        deploy_task=s.deploy.task,
        labels=Labels(
            ready=s.labels.ready,
            triage=s.labels.triage,
            blocked=s.labels.blocked,
            risk_gate=s.labels.risk_gate,
        ),
        briefs=Briefs(
            worker_preamble=s.briefs.worker_preamble,
            maintenance=s.briefs.maintenance,
        ),
        critic=CriticConfig(
            enabled=s.critic.enabled,
            timeout_s=s.critic.timeout_s,
            block_on_sev2=s.critic.block_on_sev2,
            min_findings_for_approve=s.critic.min_findings_for_approve,
            model=s.critic.model,
            thinking=s.critic.thinking,
            provider=s.critic.provider,
        ),
        po=POConfig(
            enabled=s.po.enabled,
            timeout_s=s.po.timeout_s,
            max_to_expand_per_tick=s.po.max_to_expand_per_tick,
            model=s.po.model,
            thinking=s.po.thinking,
            provider=s.po.provider,
        ),
        worker=WorkerConfig(
            model=s.worker.model,
            thinking=s.worker.thinking,
            provider=s.worker.provider,
            allowed_mcp_tools=s.worker.allowed_mcp_tools,
            rescue_format_cmd=s.worker.rescue_format_cmd,
            load_timeout_ms=s.worker.load_timeout_ms,
            strict_mcp_config=s.worker.strict_mcp_config,
            mcp_servers=dict(s.worker.mcp_servers),
            permissions=s.worker.permissions,
        ),
        attempts=AttemptsConfig(
            enabled=s.attempts.enabled,
            max_history_in_brief=s.attempts.max_history_in_brief,
        ),
        lumen=LumenConfig(top_k=s.lumen.top_k),
        worker_max_iterations=s.iteration.max_iterations,
        stuck_threshold_attempts=s.maintenance.stuck_threshold_attempts,
        stuck_tail_events=s.maintenance.stuck_tail_events,
    )


def load() -> Config:
    """Load + validate runtime config. Backwards-compat wrapper around
    :func:`forge_loop.settings.get_settings`. Raises :class:`ConfigError`
    on validation failure.
    """
    return _from_settings(Settings.load())


__all__ = [
    "AttemptsConfig",
    "Briefs",
    "Config",
    "ConfigError",
    "CriticConfig",
    "DEFAULT_ALLOWED_MCP_SERVERS",
    "Labels",
    "LumenConfig",
    "ModelConfigError",
    "POConfig",
    "WorkerConfig",
    "load",
]
