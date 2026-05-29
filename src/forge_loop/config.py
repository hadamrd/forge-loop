"""Layered config — YAML file (forge-loop.yaml) overridden by LOOP_* env vars.

Precedence (highest first):
    1. Env vars (LOOP_*)
    2. forge-loop.yaml (in repo root OR at LOOP_CONFIG_PATH)
    3. Built-in defaults

Keep this module side-effect-free so tests can swap it cheaply.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Recognised Claude model aliases. Matches the canonical
# ``claude-<family>-<major>-<minor>`` pattern (e.g. ``claude-opus-4-7``,
# ``claude-sonnet-4-6``). Loader-side validation raises a clear error at
# startup if an operator sets ``LOOP_*_MODEL`` (or the yaml equivalent) to
# something that does not parse — much better than a cryptic SDK failure
# at first dispatch.
_MODEL_PATTERN = re.compile(r"^claude-(opus|sonnet|haiku)-\d+-\d+(-[a-z0-9.-]+)?$")
_CODEX_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_AGENT_PROVIDERS = frozenset({"claude", "codex"})

# Thinking-budget tiers we expose. ``off`` disables extended thinking
# entirely; ``low``/``medium``/``high`` map to ascending budgets the SDK
# can translate. Validated identically to the model knob.
_THINKING_VALUES = frozenset({"off", "low", "medium", "high"})


class ModelConfigError(ValueError):
    """Raised at config load time when a per-role model/thinking knob is invalid.

    The message names the offending value AND the source knob (env var name
    or yaml path) so the operator can fix the typo without grepping.
    """


def _validate_provider(value: str, source: str) -> str:
    if value not in _AGENT_PROVIDERS:
        raise ModelConfigError(
            f"unknown agent provider {value!r} for {source}: "
            f"expected one of {sorted(_AGENT_PROVIDERS)}"
        )
    return value


def _validate_model(value: str, source: str, provider: str = "claude") -> str:
    if provider == "codex":
        if value == "" or _CODEX_MODEL_PATTERN.match(value):
            return value
        raise ModelConfigError(
            f"unknown Codex model alias {value!r} for {source}: "
            "expected an empty value for the Codex CLI default or a safe model name"
        )
    if not _MODEL_PATTERN.match(value):
        raise ModelConfigError(
            f"unknown model alias {value!r} for {source}: "
            "expected something like 'claude-opus-4-7' or 'claude-sonnet-4-6'"
        )
    return value


def _validate_thinking(value: str, source: str) -> str:
    # YAML 1.1 parses bare ``off``/``on`` as booleans — accept that quirk
    # so operators don't have to remember to quote ``thinking: "off"``.
    if value == "False":
        value = "off"
    if value not in _THINKING_VALUES:
        raise ModelConfigError(
            f"unknown thinking value {value!r} for {source}: "
            f"expected one of {sorted(_THINKING_VALUES)}"
        )
    return value


def _repo_root() -> Path:
    if env := os.environ.get("LOOP_REPO_DIR"):
        return Path(env).resolve()
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


@dataclass(frozen=True)
class Briefs:
    worker_preamble: str | None = None
    maintenance: str | None = None


@dataclass(frozen=True)
class Labels:
    ready: str = "loop:ready"
    triage: str = "loop:triage"
    blocked: str = "loop:blocked"
    # Workers ON issues carrying this label DO NOT enable auto-merge —
    # they post the PR + comment "ready for human review" and exit.
    # Empty string = feature off.
    risk_gate: str = "risk:high"


@dataclass(frozen=True)
class CriticConfig:
    # Default ON — caught a class of regressions auto-merge alone misses
    # (issue's acceptance criteria not met, fix is too narrow, tests added but
    # don't actually exercise the change).
    enabled: bool = True
    timeout_s: int = 600
    # If True, sev2 findings ALSO block auto-merge (default: only sev1 blocks).
    block_on_sev2: bool = False
    # If a PR with > this many changed lines comes back from the critic with
    # zero findings AND ``overall=approve``, treat it as suspicious: do NOT
    # let auto-merge proceed, label the PR ``critic:suspicious``, and surface
    # a ``critic_suspicious_approve`` event for the operator.
    min_findings_for_approve: int = 50
    # Per-role model (issue #34). Critic is fine on Sonnet — the work is
    # rubric-checking against a spec, not novel synthesis.
    # NOTE: thinking-budget config for the critic is deferred until it
    # migrates from `claude -p` subprocess to the SDK; until then this is a
    # placeholder and the field is preserved only so the resolved-config
    # surface stays uniform across roles.
    model: str = "claude-sonnet-4-6"
    thinking: str = "off"
    provider: str = "claude"


@dataclass(frozen=True)
class POConfig:
    """PO (Product Owner) spec-expander — fattens thin issues to feature-grade.

    The worker's PR depth tracks the issue body's spec depth. Without this
    pass, the loop ships one-line PRs even for issues that are actually
    feature-shaped.

    NOTE: ``thinking`` is recorded here for completeness, but the PO still
    runs via ``claude -p`` subprocess (not the SDK), and the CLI does not
    yet expose a thinking-budget flag. The field is wired through and will
    activate once the PO migrates to the SDK (see follow-up of issue #34).
    """

    enabled: bool = True
    timeout_s: int = 480
    max_to_expand_per_tick: int = 2
    # Per-role model (issue #34). PO needs hard thinking about spec quality.
    model: str = "claude-opus-4-7"
    thinking: str = "high"
    provider: str = "claude"


# Bundled default of MCP servers the worker is allowed to call (issue #60).
# Operator-side Claude Code typically connects Gmail, Drive, Calendar, tutor
# stacks and so on — every one of those bloats the worker init system prompt
# with tool definitions the worker never uses. Default keeps only the three
# servers a worker brief actually exercises.
DEFAULT_ALLOWED_MCP_SERVERS: tuple[str, ...] = ("forge-loop", "lumen", "github")


@dataclass(frozen=True)
class WorkerConfig:
    """Per-role worker model + thinking-budget (issue #34).

    Workers do medium-effort implementation: a default of Opus with medium
    thinking is the sweet spot identified by the operators in the issue
    body. Both knobs are independently overridable via env or yaml.

    ``allowed_mcp_tools`` (issue #60) is the server-name allow-list that
    gates which MCP tool definitions get injected into the SDK init
    message. Empty values fall back to the bundled default — an empty
    list would break the worker since it relies on at least the
    forge-loop server.
    """

    model: str = "claude-opus-4-7"
    thinking: str = "medium"
    provider: str = "claude"
    allowed_mcp_tools: tuple[str, ...] = DEFAULT_ALLOWED_MCP_SERVERS


@dataclass(frozen=True)
class AttemptsConfig:
    enabled: bool = True
    max_history_in_brief: int = 5


@dataclass(frozen=True)
class LumenConfig:
    """Lumen semantic-search discovery of dependent tests in the worker brief.

    Cap at K=3 — the worker runs at most K+1 `--tests` invocations per sprint
    (K discovered + 1 authored). Graceful-degrade is non-negotiable: if Lumen
    is offline the brief still renders and the worker continues.
    """

    top_k: int = 3


@dataclass(frozen=True)
class Config:
    repo: Path
    github_repo: str | None = None
    base_branch: str = "trunk"
    coauthor: str = ""
    lumen_test_pattern: str = "**/*Test.*"
    worktree_root: Path = field(default_factory=lambda: Path("/tmp"))

    # Scheduling
    parallel: int = 3
    tick_interval_s: int = 60
    max_ticks: int = 0
    worker_timeout_s: int = (
        7200  # fail-safe wall ceiling; idle-kill is the primary killer (watchdog)
    )
    maintenance_every_n_ticks: int = 0  # 0 = off

    # Deploy (no default — operators set LOOP_DEPLOY_TASK or repo.deploy.task in YAML)
    deploy_task: str = ""

    # Vocabulary
    labels: Labels = field(default_factory=Labels)

    # Brief overrides (optional)
    briefs: Briefs = field(default_factory=Briefs)

    # Critic / reviewer agent
    critic: CriticConfig = field(default_factory=CriticConfig)

    # PO spec-expander
    po: POConfig = field(default_factory=POConfig)

    # Per-role worker model + thinking-budget (issue #34)
    worker: WorkerConfig = field(default_factory=WorkerConfig)

    # Per-issue attempt history
    attempts: AttemptsConfig = field(default_factory=AttemptsConfig)

    # Lumen-discovery cap for dependent tests (issue #1002)
    lumen: LumenConfig = field(default_factory=LumenConfig)

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


def _yaml_config_path(repo: Path) -> Path | None:
    """Locate `forge-loop.yaml` in priority order."""
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


def _env_int(key: str, fallback: int) -> int:
    val = os.environ.get(key)
    return int(val) if val is not None else fallback


def _env_str(key: str, fallback: str) -> str:
    return os.environ.get(key, fallback)


def _parse_mcp_server_list(raw: Any) -> tuple[str, ...]:
    """Normalise an allow-list of MCP server names (issue #60).

    Accepts either a Python list (yaml shape) or a comma-separated string
    (env-var shape). Strips whitespace and drops empty entries. Returns a
    tuple so it can live on the frozen ``WorkerConfig``.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        parts = [str(raw).strip()]
    return tuple(p for p in parts if p)


def _resolve_allowed_mcp_tools(worker_block: dict[str, Any]) -> tuple[str, ...]:
    """Resolve worker.allowed_mcp_tools (env > yaml > bundled default).

    Empty result (env var set to ``""``, yaml set to ``[]``) falls back
    to the bundled default — an empty allow-list would break the worker
    since the brief depends on at least the forge-loop server.
    """
    raw_env = os.environ.get("LOOP_WORKER_ALLOWED_MCP_TOOLS")
    if raw_env is not None:
        parsed = _parse_mcp_server_list(raw_env)
        return parsed or DEFAULT_ALLOWED_MCP_SERVERS
    if "allowed_mcp_tools" in worker_block:
        parsed = _parse_mcp_server_list(worker_block["allowed_mcp_tools"])
        return parsed or DEFAULT_ALLOWED_MCP_SERVERS
    return DEFAULT_ALLOWED_MCP_SERVERS


def _env_bool(key: str, fallback: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return fallback
    return val.strip().lower() in {"1", "true", "yes", "on"}


def load() -> Config:
    repo = _repo_root()
    y: dict[str, Any] = {}
    if path := _yaml_config_path(repo):
        y = _load_yaml(path)

    repo_block = y.get("repo") or {}
    sched_block = y.get("scheduling") or {}
    deploy_block = y.get("deploy") or {}
    labels_block = y.get("labels") or {}
    briefs_block = y.get("briefs") or {}
    critic_block = y.get("critic") or {}
    po_block = y.get("po") or {}
    worker_block = y.get("worker") or {}
    agent_block = y.get("agent") or {}
    attempts_block = y.get("attempts") or {}
    lumen_block = y.get("lumen") or {}

    def _resolve_role(
        env_model: str,
        env_thinking: str,
        env_provider: str,
        block: dict[str, Any],
        default_model: str,
        default_thinking: str,
    ) -> tuple[str, str, str]:
        raw_provider = os.environ.get(env_provider)
        provider_source = env_provider
        if raw_provider is None:
            raw_provider = os.environ.get("LOOP_AGENT_PROVIDER")
            provider_source = "LOOP_AGENT_PROVIDER"
        if raw_provider is None:
            raw_provider = block.get("provider", agent_block.get("provider", "claude"))
            provider_source = (
                f"yaml: {env_provider.lower().replace('loop_', '').replace('_provider', '')}"
                ".provider"
            )
        provider = _validate_provider(str(raw_provider), provider_source)
        raw_model = os.environ.get(env_model)
        model_source = env_model
        if raw_model is None:
            raw_model = block.get("model", default_model if provider == "claude" else "")
            model_source = (
                f"yaml: {env_model.lower().replace('loop_', '').replace('_model', '')}.model"
            )
        raw_thinking = os.environ.get(env_thinking)
        thinking_source = env_thinking
        if raw_thinking is None:
            raw_thinking = block.get("thinking", default_thinking)
            thinking_source = (
                f"yaml: {env_thinking.lower().replace('loop_', '').replace('_thinking', '')}"
                ".thinking"
            )
        return (
            provider,
            _validate_model(str(raw_model), model_source, provider),
            _validate_thinking(str(raw_thinking), thinking_source),
        )

    worker_provider, worker_model, worker_thinking = _resolve_role(
        "LOOP_WORKER_MODEL",
        "LOOP_WORKER_THINKING",
        "LOOP_WORKER_PROVIDER",
        worker_block,
        "claude-opus-4-7",
        "medium",
    )
    worker_allowed = _resolve_allowed_mcp_tools(worker_block)
    po_provider, po_model, po_thinking = _resolve_role(
        "LOOP_PO_MODEL",
        "LOOP_PO_THINKING",
        "LOOP_PO_PROVIDER",
        po_block,
        "claude-opus-4-7",
        "high",
    )
    critic_provider, critic_model, critic_thinking = _resolve_role(
        "LOOP_CRITIC_MODEL",
        "LOOP_CRITIC_THINKING",
        "LOOP_CRITIC_PROVIDER",
        critic_block,
        "claude-sonnet-4-6",
        "off",
    )

    github_repo = os.environ.get("LOOP_GH_REPO") or repo_block.get("github")
    if not github_repo:
        raise RuntimeError(
            "github_repo not configured: "
            "set LOOP_GH_REPO env var or the `repo.github` field in your config YAML "
            "(e.g. owner/repo)"
        )

    return Config(
        repo=repo,
        github_repo=github_repo,
        base_branch=_env_str("LOOP_BASE_BRANCH", repo_block.get("base_branch", "trunk")),
        coauthor=os.environ.get("LOOP_COAUTHOR", ""),
        lumen_test_pattern=lumen_block.get("test_pattern", "**/*Test.*"),
        worktree_root=Path(repo_block.get("worktree_root", "/tmp")),
        parallel=_env_int("LOOP_PARALLEL", sched_block.get("parallel", 3)),
        tick_interval_s=_env_int("LOOP_TICK_INTERVAL_S", sched_block.get("tick_interval_s", 60)),
        max_ticks=_env_int("LOOP_MAX_TICKS", sched_block.get("max_ticks", 0)),
        worker_timeout_s=_env_int(
            "LOOP_WORKER_TIMEOUT_S", sched_block.get("worker_timeout_s", 7200)
        ),
        maintenance_every_n_ticks=_env_int(
            "LOOP_MAINTENANCE_EVERY_N", sched_block.get("maintenance_every_n_ticks", 0)
        ),
        deploy_task=_env_str("LOOP_DEPLOY_TASK", deploy_block.get("task", "")),
        labels=Labels(
            ready=_env_str("LOOP_QUERY_LABEL", labels_block.get("ready", "loop:ready")),
            triage=labels_block.get("triage", "loop:triage"),
            blocked=labels_block.get("blocked", "loop:blocked"),
            risk_gate=labels_block.get("risk_gate", "risk:high"),
        ),
        briefs=Briefs(
            worker_preamble=briefs_block.get("worker_preamble"),
            maintenance=briefs_block.get("maintenance"),
        ),
        critic=CriticConfig(
            enabled=bool(critic_block.get("enabled", True)),
            timeout_s=int(critic_block.get("timeout_s", 600)),
            block_on_sev2=_env_bool(
                "LOOP_CRITIC_BLOCK_ON_SEV2",
                bool(critic_block.get("block_on_sev2", False)),
            ),
            min_findings_for_approve=_env_int(
                "LOOP_CRITIC_MIN_FINDINGS",
                int(critic_block.get("min_findings_for_approve", 50)),
            ),
            model=critic_model,
            thinking=critic_thinking,
            provider=critic_provider,
        ),
        po=POConfig(
            enabled=bool(po_block.get("enabled", True)),
            timeout_s=int(po_block.get("timeout_s", 480)),
            max_to_expand_per_tick=int(po_block.get("max_to_expand_per_tick", 2)),
            model=po_model,
            thinking=po_thinking,
            provider=po_provider,
        ),
        worker=WorkerConfig(
            model=worker_model,
            thinking=worker_thinking,
            provider=worker_provider,
            allowed_mcp_tools=worker_allowed,
        ),
        attempts=AttemptsConfig(
            enabled=bool(attempts_block.get("enabled", True)),
            max_history_in_brief=int(attempts_block.get("max_history_in_brief", 5)),
        ),
        lumen=LumenConfig(
            top_k=_env_int("LOOP_LUMEN_TOP_K", int(lumen_block.get("top_k", 3))),
        ),
    )
