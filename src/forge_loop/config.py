"""Layered config — YAML file (forge-loop.yaml) overridden by LOOP_* env vars.

Precedence (highest first):
    1. Env vars (LOOP_*)
    2. forge-loop.yaml (in repo root OR at LOOP_CONFIG_PATH)
    3. Built-in defaults

Keep this module side-effect-free so tests can swap it cheaply.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


@dataclass(frozen=True)
class POConfig:
    """PO (Product Owner) spec-expander — fattens thin issues to feature-grade.

    The worker's PR depth tracks the issue body's spec depth. Without this
    pass, the loop ships one-line PRs even for issues that are actually
    feature-shaped.
    """
    enabled: bool = True
    timeout_s: int = 480
    max_to_expand_per_tick: int = 2


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
    coauthor: str = ""
    lumen_test_pattern: str = "**/*Test.*"
    worktree_root: Path = field(default_factory=lambda: Path("/tmp"))

    # Scheduling
    parallel: int = 3
    tick_interval_s: int = 60
    max_ticks: int = 0
    worker_timeout_s: int = 7200  # fail-safe wall ceiling; idle-kill is the primary killer (watchdog)
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

    @property
    def spend_ledger(self) -> Path:
        return self.state_dir / "loop-runner-spend.jsonl"


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
    attempts_block = y.get("attempts") or {}
    lumen_block = y.get("lumen") or {}

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
        ),
        po=POConfig(
            enabled=bool(po_block.get("enabled", True)),
            timeout_s=int(po_block.get("timeout_s", 480)),
            max_to_expand_per_tick=int(po_block.get("max_to_expand_per_tick", 2)),
        ),
        attempts=AttemptsConfig(
            enabled=bool(attempts_block.get("enabled", True)),
            max_history_in_brief=int(attempts_block.get("max_history_in_brief", 5)),
        ),
        lumen=LumenConfig(
            top_k=_env_int("LOOP_LUMEN_TOP_K", int(lumen_block.get("top_k", 3))),
        ),
    )
