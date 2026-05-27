"""Pluggable role system (issue #17).

Roles live as YAML files in ``.forge/roles/*.yaml``. Each defines a
name, brief template path (or inline brief), model id, timeout, optional
budget cap, triggers, and allowed actions.

Built-in roles (po, worker, critic) ship as default YAMLs under
:mod:`forge_loop.roles.builtin` so the loop is self-documenting. Operators
override a built-in by dropping a same-named YAML into ``.forge/roles/``.

Public entry points:

- :func:`forge_loop.roles.discover.discover_roles` — walks the built-in
  + project directories and returns the resolved list of :class:`Role`.
- :class:`forge_loop.roles.role.Role` — the validated dataclass.

The CLI ``forge-loop roles list`` (see :mod:`forge_loop.cli`) prints the
loaded roles, their triggers, and the next firing time.
"""

from forge_loop.roles.discover import (
    RoleLoadError,
    discover_roles,
    iter_role_files,
    load_role_file,
)
from forge_loop.roles.role import Action, Role, Trigger

__all__ = [
    "Action",
    "Role",
    "RoleLoadError",
    "Trigger",
    "discover_roles",
    "iter_role_files",
    "load_role_file",
]
