"""Filesystem discovery for ``.forge/roles/*.yaml`` (issue #17).

Discovery order:

1. Built-in roles bundled under :mod:`forge_loop.roles.builtin`.
2. Project-level overrides under ``<project_root>/.forge/roles/*.yaml``.

If a project-level YAML names the same role as a built-in, the project
file wins — operators can override any built-in by dropping a same-named
YAML in place.

Malformed YAML never crashes the loop: each load error is recorded on the
returned :class:`DiscoveryResult` so the CLI / runner can surface the
diagnostic, but discovery continues with the remaining files.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from forge_loop.roles.role import Role, RoleSchemaError

logger = logging.getLogger(__name__)


class RoleLoadError(Exception):
    """One YAML failed to load. Carries source + line/column when known."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message


@dataclass
class DiscoveryResult:
    roles: list[Role] = field(default_factory=list)
    errors: list[RoleLoadError] = field(default_factory=list)

    def by_name(self, name: str) -> Role | None:
        for r in self.roles:
            if r.name == name:
                return r
        return None


def _parse_yaml(path: Path) -> tuple[object | None, RoleLoadError | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, RoleLoadError(str(path), f"cannot read file: {e}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # Surface the mark (line/column) when PyYAML provides it.
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            msg = (
                f"malformed YAML at line {mark.line + 1} col {mark.column + 1}: "
                f"{getattr(e, 'problem', str(e))}"
            )
        else:
            msg = f"malformed YAML: {e}"
        return None, RoleLoadError(str(path), msg)
    return data, None


def load_role_file(path: Path) -> Role:
    """Parse + validate a single ``role.yaml``. Raises RoleLoadError on failure."""
    data, err = _parse_yaml(path)
    if err is not None:
        raise err
    try:
        return Role.from_dict(data, source=str(path))
    except RoleSchemaError as e:
        raise RoleLoadError(str(path), str(e)) from e


def _builtin_yaml_paths() -> list[Path]:
    """Return the bundled built-in role YAMLs as filesystem paths.

    Uses ``importlib.resources`` so it works from an installed wheel.
    """
    try:
        root = resources.files("forge_loop.roles.builtin")
    except (ModuleNotFoundError, FileNotFoundError):
        return []
    out: list[Path] = []
    for entry in root.iterdir():
        name = entry.name
        if name.endswith((".yaml", ".yml")) and not name.startswith("_"):
            # Resources may be inside a zip; materialize via as_file when
            # available. For the common editable-install / sdist case the
            # path is already a real Path.
            try:
                out.append(Path(str(entry)))
            except TypeError:
                continue
    return sorted(out)


def iter_role_files(project_dir: Path | None) -> Iterable[tuple[str, Path]]:
    """Yield (origin, path) pairs in discovery order.

    ``origin`` is one of ``"builtin"`` or ``"project"`` for diagnostics.
    """
    for p in _builtin_yaml_paths():
        yield "builtin", p
    if project_dir is not None:
        roles_dir = project_dir / ".forge" / "roles"
        if roles_dir.is_dir():
            for p in sorted(roles_dir.iterdir()):
                if p.suffix in (".yaml", ".yml") and not p.name.startswith("_"):
                    yield "project", p


def discover_roles(
    project_dir: Path | str | None = None,
    *,
    include_builtins: bool = True,
) -> DiscoveryResult:
    """Walk built-in + project role dirs and return resolved roles + errors.

    Project YAMLs with the same ``name`` as a built-in override the built-in
    silently — the override is the intended way to customize a default role.

    Malformed YAMLs are recorded on :attr:`DiscoveryResult.errors` and
    skipped; discovery never raises.
    """
    project = Path(project_dir) if project_dir is not None else None
    by_name: dict[str, Role] = {}
    errors: list[RoleLoadError] = []

    for origin, path in iter_role_files(project):
        if origin == "builtin" and not include_builtins:
            continue
        try:
            role = load_role_file(path)
        except RoleLoadError as e:
            logger.warning("role load failed: %s", e)
            errors.append(e)
            continue
        # Later sources (project) overwrite earlier (builtin) — this is the
        # intended override mechanism.
        by_name[role.name] = role

    return DiscoveryResult(roles=list(by_name.values()), errors=errors)
