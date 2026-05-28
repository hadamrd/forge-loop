"""Repo-spec loader for multirepo mode.

YAML schema (one file per repo at ``<loop_home>/.forge/repos/<name>.yaml``)::

    name: my-app
    github: org/my-app
    checkout: /var/forge/my-app
    labels:
      ready: loop:ready
      blocked: loop:blocked
    pipeline: ../pipeline.default.yaml   # optional, currently informational
    budget_usd_per_day: 50               # optional, default 50

Per-repo enable/disable:
    A flag file at ``<checkout>/.forge/disabled`` skips that repo on every
    tick until removed. The CLI ``forge-loop repos {enable,disable}`` writes
    or deletes this file. Disable is per-repo and survives loop restarts.

State + event files for each repo land under ``<checkout>/docs/ops/`` (the
existing single-repo layout, unchanged) — so removing a repo from
``.forge/repos/`` does not lose its history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)

DEFAULT_BUDGET_USD_PER_DAY: float = 50.0
DISABLED_FLAG = ".forge/disabled"


class RepoLoadError(ValueError):
    """Raised when a repo spec file is unparsable or missing required fields."""


@dataclass(frozen=True)
class RepoSpec:
    """One loaded repo entry.

    ``source_path`` is the path to the YAML file the spec came from; useful
    for ``forge-loop repos list`` so the operator can find where to edit.
    """

    name: str
    github: str
    checkout: Path
    labels: Labels = field(default_factory=Labels)
    pipeline: str | None = None
    budget_usd_per_day: float = DEFAULT_BUDGET_USD_PER_DAY
    source_path: Path | None = None

    @property
    def disabled_flag_path(self) -> Path:
        return self.checkout / DISABLED_FLAG


def _parse_spec(path: Path, raw: dict[str, Any]) -> RepoSpec:
    name = raw.get("name")
    github = raw.get("github")
    checkout = raw.get("checkout")
    if not name or not isinstance(name, str):
        raise RepoLoadError(f"{path}: missing or invalid `name`")
    if not github or not isinstance(github, str) or "/" not in github:
        raise RepoLoadError(f"{path}: `github` must be 'owner/repo', got {github!r}")
    if not checkout or not isinstance(checkout, str):
        raise RepoLoadError(f"{path}: missing or invalid `checkout`")
    labels_block = raw.get("labels") or {}
    if not isinstance(labels_block, dict):
        raise RepoLoadError(f"{path}: `labels` must be a mapping")
    labels = Labels(
        ready=labels_block.get("ready", "loop:ready"),
        triage=labels_block.get("triage", "loop:triage"),
        blocked=labels_block.get("blocked", "loop:blocked"),
        risk_gate=labels_block.get("risk_gate", "risk:high"),
    )
    budget = raw.get("budget_usd_per_day", DEFAULT_BUDGET_USD_PER_DAY)
    try:
        budget_f = float(budget)
    except (TypeError, ValueError) as e:
        raise RepoLoadError(f"{path}: budget_usd_per_day not numeric: {budget!r}") from e
    pipeline = raw.get("pipeline")
    if pipeline is not None and not isinstance(pipeline, str):
        raise RepoLoadError(f"{path}: `pipeline` must be a string path or null")
    return RepoSpec(
        name=name,
        github=github,
        checkout=Path(checkout).expanduser(),
        labels=labels,
        pipeline=pipeline,
        budget_usd_per_day=budget_f,
        source_path=path,
    )


def load_repos(repos_dir: Path) -> list[RepoSpec]:
    """Load every ``*.yaml`` / ``*.yml`` file in ``repos_dir``.

    Sorted by name for deterministic round-robin order. Raises
    ``RepoLoadError`` on the first malformed file — fail loudly rather than
    silently skip a repo the operator thinks is configured.

    If ``repos_dir`` does not exist, returns an empty list (the loop falls
    back to single-repo mode).
    """
    if not repos_dir.exists():
        return []
    if not repos_dir.is_dir():
        raise RepoLoadError(f"{repos_dir} is not a directory")
    specs: list[RepoSpec] = []
    seen: set[str] = set()
    files = sorted([*repos_dir.glob("*.yaml"), *repos_dir.glob("*.yml")])
    for path in files:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise RepoLoadError(f"{path}: YAML parse error: {e}") from e
        if not isinstance(raw, dict):
            raise RepoLoadError(f"{path}: top-level must be a mapping")
        spec = _parse_spec(path, raw)
        if spec.name in seen:
            raise RepoLoadError(f"{path}: duplicate repo name {spec.name!r}")
        seen.add(spec.name)
        specs.append(spec)
    return specs


def is_disabled(spec: RepoSpec) -> bool:
    """Repo is skipped while ``<checkout>/.forge/disabled`` exists."""
    return spec.disabled_flag_path.exists()


def disable_repo(spec: RepoSpec, reason: str = "") -> Path:
    """Write the disable flag. Idempotent — calling twice is a no-op."""
    flag = spec.disabled_flag_path
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(reason or "disabled\n")
    return flag


def enable_repo(spec: RepoSpec) -> bool:
    """Remove the disable flag if present. Returns True if a flag was cleared."""
    flag = spec.disabled_flag_path
    if flag.exists():
        flag.unlink()
        return True
    return False


def validate_checkout(spec: RepoSpec) -> str | None:
    """Return a short human-readable reason if the checkout is unusable.

    Adversarial path: the YAML points at a checkout that has been moved /
    renamed / never existed. The runner skips the repo with this reason
    rather than crashing the whole loop.
    """
    if not spec.checkout.exists():
        return f"checkout path missing: {spec.checkout}"
    if not spec.checkout.is_dir():
        return f"checkout path is not a directory: {spec.checkout}"
    if not (spec.checkout / ".git").exists():
        return f"checkout is not a git repo (no .git): {spec.checkout}"
    return None


def build_config_for_repo(spec: RepoSpec, *, template: Config | None = None) -> Config:
    """Materialize a per-repo ``Config`` from a ``RepoSpec``.

    Scheduling / brief / critic / PO defaults come from ``template`` (the
    operator's global ``forge-loop.yaml``); per-repo overrides live in the
    repo spec. This means a multirepo operator still configures cadence /
    timeouts / agent toggles ONCE in the loop home, and only the repo-shaped
    knobs (github slug, labels, budget) get split per repo.
    """
    tmpl = template
    if tmpl is None:
        # A neutral default so callers that only need the path layout work
        # without a real forge-loop.yaml on disk (used by tests).
        tmpl = Config(repo=spec.checkout, github_repo=spec.github)
    return Config(
        repo=spec.checkout,
        github_repo=spec.github,
        coauthor=tmpl.coauthor,
        lumen_test_pattern=tmpl.lumen_test_pattern,
        worktree_root=tmpl.worktree_root,
        parallel=tmpl.parallel,
        tick_interval_s=tmpl.tick_interval_s,
        max_ticks=tmpl.max_ticks,
        worker_timeout_s=tmpl.worker_timeout_s,
        maintenance_every_n_ticks=tmpl.maintenance_every_n_ticks,
        deploy_task=tmpl.deploy_task,
        labels=spec.labels,
        briefs=Briefs(
            worker_preamble=tmpl.briefs.worker_preamble,
            maintenance=tmpl.briefs.maintenance,
        ),
        critic=CriticConfig(
            enabled=tmpl.critic.enabled,
            timeout_s=tmpl.critic.timeout_s,
            block_on_sev2=tmpl.critic.block_on_sev2,
            min_findings_for_approve=tmpl.critic.min_findings_for_approve,
            model=tmpl.critic.model,
            thinking=tmpl.critic.thinking,
            provider=tmpl.critic.provider,
        ),
        po=POConfig(
            enabled=tmpl.po.enabled,
            timeout_s=tmpl.po.timeout_s,
            max_to_expand_per_tick=tmpl.po.max_to_expand_per_tick,
            model=tmpl.po.model,
            thinking=tmpl.po.thinking,
            provider=tmpl.po.provider,
        ),
        worker=tmpl.worker,
        attempts=AttemptsConfig(
            enabled=tmpl.attempts.enabled,
            max_history_in_brief=tmpl.attempts.max_history_in_brief,
        ),
        lumen=LumenConfig(top_k=tmpl.lumen.top_k),
    )
