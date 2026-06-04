"""Single source of truth for runtime config (issue #84).

Replaces the 35-callsite ``os.environ.get(...)`` sprawl + the layered
:func:`forge_loop.config.load` loader with one pydantic-settings model.

Precedence (highest first), uniform across all fields:
    1. Env vars (``LOOP_*`` / ``FORGE_LOOP_*``)
    2. ``forge-loop.yaml`` (repo root or ``LOOP_CONFIG_PATH``)
    3. Built-in defaults declared on the Settings model.

Public entrypoint: :func:`get_settings` (memoised) — call sites that used
to read ``os.environ.get("LOOP_X")`` now read
``get_settings().<group>.<field>``. The legacy :class:`forge_loop.config.Config`
dataclass is built FROM a Settings instance and kept as a backwards-compat
view so the runner / worker / dispatch path don't have to be rewritten in
one shot.

Validation errors surface as :class:`ConfigError` at load time with the
field name in the message — operators no longer get cryptic mid-tick
crashes from a typo in a yaml file.
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Recognised Claude model aliases — same as the legacy loader.
_MODEL_PATTERN = re.compile(r"^claude-(opus|sonnet|haiku)-\d+-\d+(-[a-z0-9.-]+)?$")
_CODEX_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_AGENT_PROVIDERS = frozenset({"claude", "codex"})
_THINKING_VALUES = frozenset({"off", "low", "medium", "high"})

DEFAULT_ALLOWED_MCP_SERVERS: tuple[str, ...] = ("forge-loop", "lumen", "github")


class ConfigError(RuntimeError):
    """Raised at settings load time when a config value is invalid.

    The message names the field and value so operators can fix the typo
    without grepping the source.

    Inherits from :class:`RuntimeError` (not ValueError) because the legacy
    ``load()`` raised ``RuntimeError`` for missing-repo and the legacy
    ``ModelConfigError`` raised ``ValueError`` — picking RuntimeError keeps
    the most common catch site (the runner boot path) backward-compatible,
    and ``ModelConfigError = ConfigError`` keeps the alias working.
    """


def _coerce_str_tuple(raw: Any) -> tuple[str, ...]:
    """Normalise a list/tuple/csv-string into a tuple of stripped strings."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        parts = [str(raw).strip()]
    return tuple(p for p in parts if p)


def _source_hint(field_name: str) -> str:
    """Return ``" (LOOP_FOO)"`` when ``field_name`` has a known env-var.

    Used in error messages so operators know exactly which knob to fix
    when an alias is wrong, without grepping the source tree.
    """
    for env_var, dotted, _coercer in ENV_MAP:
        if dotted == field_name:
            return f" (set via {env_var} or yaml {dotted})"
    return ""


def _validate_model_for_provider(model: str, provider: str, field_name: str) -> None:
    """Cross-field model alias check — provider drives which pattern applies.

    Codex accepts an empty model (= CLI default) or a safe identifier.
    Claude requires the canonical ``claude-<family>-<major>-<minor>`` shape.
    """
    hint = _source_hint(field_name)
    if provider == "codex":
        if model == "" or _CODEX_MODEL_PATTERN.match(model):
            return
        raise ConfigError(
            f"{field_name}={model!r}{hint}: unknown Codex model alias — expected an "
            "empty value for the Codex CLI default or a safe model name"
        )
    if not _MODEL_PATTERN.match(model):
        raise ConfigError(
            f"{field_name}={model!r}{hint}: unknown model alias — expected something "
            "like 'claude-opus-4-8' or 'claude-sonnet-4-6'"
        )


def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _repo_root() -> Path:
    """Locate the repo root via env override or git."""
    if env := os.environ.get("LOOP_REPO_DIR"):
        return Path(env).resolve()
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def _yaml_config_path(repo: Path) -> Path | None:
    """Locate ``forge-loop.yaml`` in priority order (env > repo root)."""
    explicit = os.environ.get("LOOP_CONFIG_PATH")
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    candidates = [
        repo / "forge-loop.yaml",
        repo / "dev" / "sprint-loop" / "forge-loop.yaml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text())
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Nested setting groups — one per logical concern. Names + defaults mirror
# the legacy dataclasses in config.py so the translation layer is 1:1.
# ---------------------------------------------------------------------------


class LabelsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    ready: str = "loop:ready"
    triage: str = "loop:triage"
    blocked: str = "loop:blocked"
    risk_gate: str = "risk:high"


class BriefsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    worker_preamble: str | None = None
    maintenance: str | None = None


class CriticSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    enabled: bool = True
    timeout_s: int = 600
    block_on_sev2: bool = False
    min_findings_for_approve: int = 50
    model: str = "claude-sonnet-4-6"
    thinking: str = "off"
    provider: str = "claude"

    @field_validator("provider")
    @classmethod
    def _provider_known(cls, v: str) -> str:
        if v not in _AGENT_PROVIDERS:
            raise ConfigError(f"critic.provider={v!r} — expected one of {sorted(_AGENT_PROVIDERS)}")
        return str(v)

    @field_validator("thinking", mode="before")
    @classmethod
    def _thinking_known(cls, v: Any) -> str:
        # YAML 1.1 parses bare ``off`` as bool False — accept that quirk
        # so operators don't have to remember to quote ``thinking: "off"``.
        if isinstance(v, bool) or v is False:
            v = "off"
        v = str(v)
        if v == "False":
            v = "off"
        if v not in _THINKING_VALUES:
            raise ConfigError(f"critic.thinking={v!r} — expected one of {sorted(_THINKING_VALUES)}")
        return str(v)

    @model_validator(mode="after")
    def _model_alias(self) -> CriticSettings:
        _validate_model_for_provider(self.model, self.provider, "critic.model")
        return self


class POSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    enabled: bool = True
    timeout_s: int = 480
    max_to_expand_per_tick: int = 2
    model: str = "claude-opus-4-8"
    thinking: str = "high"
    provider: str = "claude"

    @field_validator("provider")
    @classmethod
    def _provider_known(cls, v: str) -> str:
        if v not in _AGENT_PROVIDERS:
            raise ConfigError(f"po.provider={v!r} — expected one of {sorted(_AGENT_PROVIDERS)}")
        return str(v)

    @field_validator("thinking", mode="before")
    @classmethod
    def _thinking_known(cls, v: Any) -> str:
        if isinstance(v, bool) or v is False:
            v = "off"
        v = str(v)
        if v == "False":
            v = "off"
        if v not in _THINKING_VALUES:
            raise ConfigError(f"po.thinking={v!r} — expected one of {sorted(_THINKING_VALUES)}")
        return str(v)

    @model_validator(mode="after")
    def _model_alias(self) -> POSettings:
        _validate_model_for_provider(self.model, self.provider, "po.model")
        return self


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    model: str = "claude-opus-4-8"
    thinking: str = "medium"
    provider: str = "claude"
    allowed_mcp_tools: tuple[str, ...] = DEFAULT_ALLOWED_MCP_SERVERS
    rescue_format_cmd: str = ""
    load_timeout_ms: int = 180000
    strict_mcp_config: bool = True
    mcp_servers: dict[str, Any] = Field(default_factory=dict)
    # Permission profile: full (default) | standard | readonly. See
    # forge_loop.worker_permissions. 'full' = today's behaviour (full host
    # access, no sandbox); the others are opt-in confinement.
    permissions: str = "full"

    @field_validator("provider")
    @classmethod
    def _provider_known(cls, v: str) -> str:
        if v not in _AGENT_PROVIDERS:
            raise ConfigError(f"worker.provider={v!r} — expected one of {sorted(_AGENT_PROVIDERS)}")
        return str(v)

    @field_validator("permissions", mode="before")
    @classmethod
    def _permissions_known(cls, v: Any) -> str:
        from forge_loop.worker_permissions import PROFILES

        s = str(v or "full").strip().lower()
        if s not in PROFILES:
            raise ConfigError(f"worker.permissions={v!r} — expected one of {sorted(PROFILES)}")
        return s

    @field_validator("thinking", mode="before")
    @classmethod
    def _thinking_known(cls, v: Any) -> str:
        if isinstance(v, bool) or v is False:
            v = "off"
        v = str(v)
        if v == "False":
            v = "off"
        if v not in _THINKING_VALUES:
            raise ConfigError(f"worker.thinking={v!r} — expected one of {sorted(_THINKING_VALUES)}")
        return str(v)

    @model_validator(mode="after")
    def _model_alias(self) -> WorkerSettings:
        _validate_model_for_provider(self.model, self.provider, "worker.model")
        return self

    @field_validator("allowed_mcp_tools", mode="before")
    @classmethod
    def _coerce_allowed(cls, v: Any) -> tuple[str, ...]:
        if v is None or v == "" or v == [] or v == ():
            return DEFAULT_ALLOWED_MCP_SERVERS
        coerced = _coerce_str_tuple(v)
        return coerced or DEFAULT_ALLOWED_MCP_SERVERS


class AttemptsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    enabled: bool = True
    max_history_in_brief: int = 5
    # The retry-cooldown bucket (legacy LOOP_RETRY_COOLDOWN_S).
    cooldown_s: int = 3600


class LumenSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    top_k: int = 3
    test_pattern: str = "**/*Test.*"


class OperatorSettings(BaseSettings):
    """Operator-facing notifications + heartbeat (legacy LOOP_OPERATOR_*)."""

    model_config = SettingsConfigDict(extra="ignore")
    timeout_s: int = 0  # 0 = off
    webhook: str | None = None
    slack_webhook: str | None = None
    issue: int | None = None


class DashboardSettings(BaseSettings):
    """Dashboard server (legacy LOOP_DASHBOARD_*)."""

    model_config = SettingsConfigDict(extra="ignore")
    port: int = 8765
    token: str | None = None


class DeploySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    task: str = ""
    # Legacy LOOP_DEPLOY_DRIFT_HALT — opt-in halt on 3 consecutive deploy fails.
    drift_halt: bool = False


class SchedulingSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    parallel: int = 3
    tick_interval_s: int = 60
    max_ticks: int = 0
    worker_timeout_s: int = 7200
    maintenance_every_n_ticks: int = 0


class RepoSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    github: str | None = None
    base_branch: str = "trunk"
    worktree_root: Path = Path("/tmp")
    # Multi-repo discovery root (legacy LOOP_REPOS_DIR).
    repos_dir: Path | None = None


class IterationSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    max_iterations: int = 3
    # Legacy LOOP_PIPELINE_DRIVEN — experimental DAG-driven tick path.
    pipeline_driven: bool = False
    # Persistent worker session (issue #95). When True, the runner
    # routes every dispatch through forge_loop.worker_sessions and keeps
    # the SDK session_id alive across critic round-trips. Defaults off
    # in this PR — the FSM + session store ship as foundation; runner
    # integration is the follow-up gated by this flag.
    persistent_worker: bool = False
    # Cap on critic ping-pong rounds (issue #95). After N revisions
    # with the critic still asking for changes, the session is abandoned
    # with ``loop:needs-human``.
    max_critic_iterations: int = 3


class MaintenanceSettings(BaseSettings):
    """Knobs for the maintenance-tier sweeps that run alongside the LLM
    groomer (issue #129).

    ``stuck_threshold_attempts`` gates the stuck-issue sweep — an issue
    needs at least this many ``worker_iterations_exhausted`` events
    (without a recovery in between) before we demote it from
    ``loop:ready`` to ``loop:needs-human``. Default 2: one bad run is
    forgivable, two is a pattern.
    """

    model_config = SettingsConfigDict(extra="ignore")
    stuck_threshold_attempts: int = 2
    stuck_tail_events: int = 100


class MiscSettings(BaseSettings):
    """Misc knobs that don't fit a logical group cleanly."""

    model_config = SettingsConfigDict(extra="ignore")
    coauthor: str = ""
    # Legacy LOOP_EVENTS_ROTATE_BYTES — rotate events.jsonl past this size.
    events_rotate_bytes: int = 0
    # Legacy LOOP_MCP_CAP_DEFAULT — default per-tool result cap (chars).
    mcp_cap_default: int = 20
    # Legacy LOOP_QUEUE_URL — optional remote queue backend.
    queue_url: str | None = None
    # Legacy FORGE_LOOP_EXPERIMENTAL — extras gate.
    experimental_enabled: bool = False


# ---------------------------------------------------------------------------
# Root Settings — everything else hangs off this.
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Root settings tree. Built from env + yaml + defaults.

    Build via :func:`get_settings` (cached) or :func:`Settings.load` (fresh).
    Never instantiate the bare class — env/yaml layering won't apply.
    """

    model_config = SettingsConfigDict(extra="ignore")

    repo: RepoSettings = Field(default_factory=RepoSettings)
    scheduling: SchedulingSettings = Field(default_factory=SchedulingSettings)
    deploy: DeploySettings = Field(default_factory=DeploySettings)
    labels: LabelsSettings = Field(default_factory=LabelsSettings)
    briefs: BriefsSettings = Field(default_factory=BriefsSettings)
    critic: CriticSettings = Field(default_factory=CriticSettings)
    po: POSettings = Field(default_factory=POSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    attempts: AttemptsSettings = Field(default_factory=AttemptsSettings)
    lumen: LumenSettings = Field(default_factory=LumenSettings)
    operator: OperatorSettings = Field(default_factory=OperatorSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    iteration: IterationSettings = Field(default_factory=IterationSettings)
    maintenance: MaintenanceSettings = Field(default_factory=MaintenanceSettings)
    misc: MiscSettings = Field(default_factory=MiscSettings)

    # The repo path itself is resolved at load time (git toplevel or env
    # override) and pinned on the instance for cheap access downstream.
    repo_path: Path = Field(default_factory=Path)

    @classmethod
    def load(cls) -> Settings:
        """Build a fresh Settings instance from yaml + env + defaults.

        Precedence (uniform across every field): env > yaml > defaults.
        Validation errors raise :class:`ConfigError` with the field name.
        """
        repo = _repo_root()
        y: dict[str, Any] = {}
        if path := _yaml_config_path(repo):
            y = _load_yaml(path)

        # Build a layered dict: defaults are already in the field
        # declarations; we splat the yaml here and then env vars below
        # override on top.
        raw: dict[str, Any] = {
            "repo_path": repo,
            "repo": {**(y.get("repo") or {})},
            "scheduling": {**(y.get("scheduling") or {})},
            "deploy": {**(y.get("deploy") or {})},
            "labels": {**(y.get("labels") or {})},
            "briefs": {**(y.get("briefs") or {})},
            "critic": {**(y.get("critic") or {})},
            "po": {**(y.get("po") or {})},
            "worker": {**(y.get("worker") or {})},
            "attempts": {**(y.get("attempts") or {})},
            "lumen": {**(y.get("lumen") or {})},
            "operator": {**(y.get("operator") or {})},
            "dashboard": {**(y.get("dashboard") or {})},
            "iteration": {**(y.get("iteration") or {})},
            "maintenance": {**(y.get("maintenance") or {})},
            "misc": {**(y.get("misc") or {})},
        }

        # ENV OVERLAY — each LOOP_* env var maps to one nested field. Done
        # explicitly (not via env_prefix auto-mapping) because the existing
        # var names are not uniformly named after their settings field; we
        # preserve the established operator vocabulary.
        _apply_env_overrides(raw)

        # Apply the agent-block legacy fallback for provider (worker/po/critic
        # all default to the agent.provider value if their own is unset).
        agent_block = y.get("agent") or {}
        agent_provider = os.environ.get("LOOP_AGENT_PROVIDER") or agent_block.get("provider")
        if agent_provider:
            for role in ("worker", "po", "critic"):
                raw[role].setdefault("provider", agent_provider)

        # Codex provider with no explicit model = empty string (CLI default).
        # Without this override, the pydantic class default
        # ``claude-opus-4-8`` would leak through and the role would try to
        # dispatch a Claude model name to the Codex CLI.
        for role in ("worker", "po", "critic"):
            block = raw[role]
            if block.get("provider") == "codex" and "model" not in block:
                block["model"] = ""

        try:
            return cls.model_validate(raw)
        except ValidationError as e:
            # Reformat the first error into a ConfigError so callers see the
            # field path + offending value without diving into pydantic guts.
            first = e.errors()[0]
            loc = ".".join(str(p) for p in first["loc"])
            val = first.get("input")
            msg = first.get("msg", "invalid value")
            raise ConfigError(f"{loc}={val!r}: {msg}") from e

    def dump_yaml(self) -> str:
        """Render the resolved settings tree as YAML (for `forge-loop config`)."""
        # ``mode="json"`` collapses Path -> str so yaml is round-trippable.
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude={"repo_path"}),
            sort_keys=False,
            default_flow_style=False,
        )


# ---------------------------------------------------------------------------
# Env overlay — preserves the LOOP_* / FORGE_LOOP_* names operators know.
# Each entry: (env_var, dotted_path, coercer). Done as data so the
# `forge-loop config` command can list the full mapping for the operator.
# ---------------------------------------------------------------------------


def _set_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


ENV_MAP: tuple[tuple[str, str, Any], ...] = (
    # Repo
    ("LOOP_GH_REPO", "repo.github", str),
    ("LOOP_BASE_BRANCH", "repo.base_branch", str),
    ("LOOP_REPOS_DIR", "repo.repos_dir", str),
    # Scheduling
    ("LOOP_PARALLEL", "scheduling.parallel", int),
    ("LOOP_TICK_INTERVAL_S", "scheduling.tick_interval_s", int),
    ("LOOP_MAX_TICKS", "scheduling.max_ticks", int),
    ("LOOP_WORKER_TIMEOUT_S", "scheduling.worker_timeout_s", int),
    ("LOOP_MAINTENANCE_EVERY_N", "scheduling.maintenance_every_n_ticks", int),
    # Deploy
    ("LOOP_DEPLOY_TASK", "deploy.task", str),
    ("LOOP_DEPLOY_DRIFT_HALT", "deploy.drift_halt", _coerce_bool),
    # Labels
    ("LOOP_QUERY_LABEL", "labels.ready", str),
    # Critic
    ("LOOP_CRITIC_MODEL", "critic.model", str),
    ("LOOP_CRITIC_THINKING", "critic.thinking", str),
    ("LOOP_CRITIC_PROVIDER", "critic.provider", str),
    ("LOOP_CRITIC_BLOCK_ON_SEV2", "critic.block_on_sev2", _coerce_bool),
    ("LOOP_CRITIC_MIN_FINDINGS", "critic.min_findings_for_approve", int),
    # PO
    ("LOOP_PO_MODEL", "po.model", str),
    ("LOOP_PO_THINKING", "po.thinking", str),
    ("LOOP_PO_PROVIDER", "po.provider", str),
    # Worker
    ("LOOP_WORKER_MODEL", "worker.model", str),
    ("LOOP_WORKER_THINKING", "worker.thinking", str),
    ("LOOP_WORKER_PROVIDER", "worker.provider", str),
    ("LOOP_WORKER_ALLOWED_MCP_TOOLS", "worker.allowed_mcp_tools", _coerce_str_tuple),
    ("LOOP_WORKER_LOAD_TIMEOUT_MS", "worker.load_timeout_ms", int),
    ("LOOP_WORKER_STRICT_MCP", "worker.strict_mcp_config", _coerce_bool),
    ("LOOP_WORKER_RESCUE_FORMAT_CMD", "worker.rescue_format_cmd", str),
    # Lumen
    ("LOOP_LUMEN_TOP_K", "lumen.top_k", int),
    # Attempts
    ("LOOP_RETRY_COOLDOWN_S", "attempts.cooldown_s", int),
    # Iteration
    ("LOOP_WORKER_MAX_ITERATIONS", "iteration.max_iterations", int),
    ("LOOP_PIPELINE_DRIVEN", "iteration.pipeline_driven", _coerce_bool),
    ("LOOP_PERSISTENT_WORKER", "iteration.persistent_worker", _coerce_bool),
    ("LOOP_MAX_CRITIC_ITERATIONS", "iteration.max_critic_iterations", int),
    # Operator
    ("LOOP_OPERATOR_TIMEOUT_S", "operator.timeout_s", int),
    ("LOOP_OPERATOR_WEBHOOK", "operator.webhook", str),
    ("LOOP_OPERATOR_SLACK_WEBHOOK", "operator.slack_webhook", str),
    ("LOOP_OPERATOR_ISSUE", "operator.issue", int),
    # Dashboard
    ("LOOP_DASHBOARD_PORT", "dashboard.port", int),
    ("LOOP_DASHBOARD_TOKEN", "dashboard.token", str),
    # Misc
    ("LOOP_COAUTHOR", "misc.coauthor", str),
    ("LOOP_EVENTS_ROTATE_BYTES", "misc.events_rotate_bytes", int),
    ("LOOP_MCP_CAP_DEFAULT", "misc.mcp_cap_default", int),
    ("LOOP_QUEUE_URL", "misc.queue_url", str),
    ("FORGE_LOOP_EXPERIMENTAL", "misc.experimental_enabled", _coerce_bool),
)


def _apply_env_overrides(raw: dict[str, Any]) -> None:
    """Walk ENV_MAP and overlay any set env vars onto the raw dict in-place."""
    for env_var, dotted, coercer in ENV_MAP:
        val = os.environ.get(env_var)
        if val is None or val == "":
            continue
        try:
            coerced = coercer(val)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{env_var}={val!r}: {e}") from e
        _set_path(raw, dotted, coerced)


# ---------------------------------------------------------------------------
# Memoised accessor. Call sites read `get_settings().<group>.<field>`. Tests
# call ``reset_settings()`` between cases to force a fresh load.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()


def reset_settings() -> None:
    """Drop the cached Settings instance — used by tests + reload paths."""
    get_settings.cache_clear()


__all__ = [
    "ConfigError",
    "DEFAULT_ALLOWED_MCP_SERVERS",
    "ENV_MAP",
    "Settings",
    "RepoSettings",
    "SchedulingSettings",
    "DeploySettings",
    "LabelsSettings",
    "BriefsSettings",
    "CriticSettings",
    "POSettings",
    "WorkerSettings",
    "AttemptsSettings",
    "LumenSettings",
    "OperatorSettings",
    "DashboardSettings",
    "IterationSettings",
    "MiscSettings",
    "get_settings",
    "reset_settings",
]
