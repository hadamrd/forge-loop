"""Brief templates for the PO, worker, and critic subagents.

Briefs live as ``.md.tmpl`` files in this package (Python ``str.format``
syntax — no Jinja). Operators can inspect them directly without reading
source, and override any of them by pointing an env var at a custom file:

    LOOP_WORKER_BRIEF  → overrides ``worker.md.tmpl``
    LOOP_PO_BRIEF      → overrides ``po.md.tmpl``
    LOOP_CRITIC_BRIEF  → overrides ``critic.md.tmpl``

Use :func:`load_template` to read the (possibly overridden) raw template
and :func:`render_brief` to render it with placeholder substitution.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Any

KINDS = ("worker", "po", "critic")

_ENV_OVERRIDES = {
    "worker": "LOOP_WORKER_BRIEF",
    "po": "LOOP_PO_BRIEF",
    "critic": "LOOP_CRITIC_BRIEF",
}


def _validate_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown brief kind: {kind!r} (expected one of {KINDS})")


def load_template(kind: str) -> str:
    """Return the raw template text for ``kind``.

    Honours ``LOOP_<KIND>_BRIEF`` env override; if the override points at a
    missing file, raises :class:`FileNotFoundError` with the env var name
    and resolved path so operators can see exactly what was attempted.

    Otherwise reads the bundled template via :mod:`importlib.resources` so
    it works from an installed wheel without depending on ``__file__``.
    """
    _validate_kind(kind)
    env_var = _ENV_OVERRIDES[kind]
    override = os.environ.get(env_var)
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"{env_var}={override!r} does not point to a readable file (resolved: {path})"
            )
        return path.read_text(encoding="utf-8")

    return (
        resources.files("forge_loop.briefs").joinpath(f"{kind}.md.tmpl").read_text(encoding="utf-8")
    )


def render_brief(kind: str, /, **kwargs: Any) -> str:
    """Render the ``kind`` brief with placeholder substitution.

    Uses Python ``str.format``. An unknown ``{placeholder}`` in the template
    raises :class:`KeyError` naming the missing key — this is intentional so
    operators editing a template can't silently ship a brief with a stray
    ``{thing}`` left over.
    """
    template = load_template(kind)
    try:
        return template.format(**kwargs)
    except KeyError as e:
        missing = e.args[0] if e.args else "?"
        raise KeyError(
            f"brief template {kind!r} references unknown placeholder "
            f"{{{missing}}} — provide it as a kwarg to render_brief() or "
            f"remove it from the template"
        ) from e
