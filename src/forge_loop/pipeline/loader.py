"""YAML loader for .forge/pipeline.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class PipelineLoadError(ValueError):
    """Raised when pipeline.yaml cannot be parsed into a PipelineSpec."""


@dataclass(frozen=True)
class Condition:
    labels: tuple[str, ...] = ()
    all_approve: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.labels and not self.all_approve


@dataclass(frozen=True)
class ChainStep:
    role: str
    after: tuple[str, ...] = ()
    on: str | None = None
    parallel: int = 1
    condition: Condition = field(default_factory=Condition)


@dataclass(frozen=True)
class PipelineSpec:
    steps: tuple[ChainStep, ...]
    source_path: Path | None = None

    def step(self, role: str) -> ChainStep:
        for s in self.steps:
            if s.role == role:
                return s
        raise KeyError(role)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            if not isinstance(v, str):
                raise PipelineLoadError(
                    f"expected string in list, got {type(v).__name__}: {v!r}"
                )
            out.append(v)
        return out
    raise PipelineLoadError(f"expected string or list of strings, got {type(value).__name__}")


def _parse_condition(raw: Any) -> Condition:
    if raw is None:
        return Condition()
    if not isinstance(raw, dict):
        raise PipelineLoadError(f"condition: must be a mapping, got {type(raw).__name__}")
    labels = tuple(_as_list(raw.get("labels")))
    all_approve = bool(raw.get("all_approve", False))
    unknown = set(raw) - {"labels", "all_approve"}
    if unknown:
        raise PipelineLoadError(f"condition: unknown keys: {sorted(unknown)}")
    return Condition(labels=labels, all_approve=all_approve)


_YAML11_BOOL_KEY_REMAP = {True: "on", False: "off"}


def _remap_yaml_bool_keys(raw: dict) -> dict:
    """YAML 1.1 (PyYAML default) parses bare ``on:``/``off:``/``yes:``/``no:``
    as booleans. We want them as literal strings so the example in the
    issue body parses without quoting. Remap True->"on" / False->"off"."""
    if not any(isinstance(k, bool) for k in raw):
        return raw
    out: dict = {}
    for k, v in raw.items():
        if isinstance(k, bool):
            new_k = _YAML11_BOOL_KEY_REMAP[k]
            out.setdefault(new_k, v)
        else:
            out[k] = v
    return out


def _parse_step(raw: Any, *, index: int) -> ChainStep:
    if not isinstance(raw, dict):
        raise PipelineLoadError(
            f"step #{index}: must be a mapping, got {type(raw).__name__}"
        )
    raw = _remap_yaml_bool_keys(raw)
    role = raw.get("role")
    if not isinstance(role, str) or not role.strip():
        raise PipelineLoadError(f"step #{index}: 'role' is required and must be a non-empty string")
    after = tuple(_as_list(raw.get("after")))
    on = raw.get("on")
    if on is not None and not isinstance(on, str):
        raise PipelineLoadError(f"step '{role}': 'on' must be a string if set")
    parallel = raw.get("parallel", 1)
    if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
        raise PipelineLoadError(
            f"step '{role}': 'parallel' must be a positive integer (got {parallel!r})"
        )
    condition = _parse_condition(raw.get("condition"))
    unknown = set(raw) - {"role", "after", "on", "parallel", "condition"}
    if unknown:
        raise PipelineLoadError(f"step '{role}': unknown keys: {sorted(unknown)}")
    return ChainStep(role=role, after=after, on=on, parallel=parallel, condition=condition)


def parse_pipeline(data: Any, *, source_path: Path | None = None) -> PipelineSpec:
    if not isinstance(data, dict):
        raise PipelineLoadError(
            f"pipeline: top-level must be a mapping, got {type(data).__name__}"
        )
    chain = data.get("default_chain")
    if chain is None:
        raise PipelineLoadError("pipeline: missing required key 'default_chain'")
    if not isinstance(chain, list) or not chain:
        raise PipelineLoadError("pipeline: 'default_chain' must be a non-empty list")
    steps = tuple(_parse_step(raw, index=i) for i, raw in enumerate(chain))
    seen: dict[str, int] = {}
    for i, s in enumerate(steps):
        if s.role in seen:
            raise PipelineLoadError(
                f"pipeline: duplicate role '{s.role}' "
                f"(positions {seen[s.role]} and {i})"
            )
        seen[s.role] = i
    return PipelineSpec(steps=steps, source_path=source_path)


def load_pipeline(path: str | Path) -> PipelineSpec:
    p = Path(path)
    if not p.exists():
        raise PipelineLoadError(f"pipeline: file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise PipelineLoadError(f"pipeline: YAML parse error in {p}: {e}") from e
    return parse_pipeline(raw, source_path=p)
