"""Role dataclass + Trigger schema for the pluggable role system (#17).

A ``Role`` is the validated, in-memory shape of a ``.forge/roles/*.yaml``
file. Validation is intentionally strict-but-friendly: every error names
the offending field and (where applicable) the offending value so the
operator can fix the YAML without reading source.

Schema (top-level):

  name             str, required, matches r"^[a-zA-Z0-9_-]+$"
  brief_template   str, required — either a path to a template OR inline
                   text if `brief_inline: true`.
  brief_inline     bool, optional (default False). When True, the value of
                   `brief_template` is used directly as the brief body.
  model            str, required — e.g. "claude-opus-4-8".
  timeout_s        int, required, > 0
  budget_usd       float, optional, >= 0 (omit/None == unlimited)
  triggers         list[Trigger], required, may be empty for manual roles
  actions          list[Action], required, may be empty (read-only role)
  output_schema    str, optional, e.g. "critic" or "po"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Triggers we know how to dispatch. Loader will accept others (forward-
# compat) but warn — see RoleLoadError. The runner is free to ignore
# triggers it doesn't understand yet.
KNOWN_TRIGGER_EVENTS = frozenset(
    {
        "pr_opened",
        "pr_updated",
        "pr_labeled",
        "issue_opened",
        "issue_labeled",
        "tick",  # every loop tick (used by po/worker/critic built-ins)
    }
)


class RoleSchemaError(ValueError):
    """Raised when a role YAML fails validation. Message names the field."""


@dataclass(frozen=True)
class Trigger:
    """When a role should fire.

    ``on`` names the event (see ``KNOWN_TRIGGER_EVENTS``). ``filter`` is a
    free-form dict whose interpretation depends on ``on``:

    - ``labels``  -> list[str] : ALL of these labels must be present.
    - ``branch``  -> str        : exact branch name match.
    - ``author``  -> str        : exact author match.
    """

    on: str
    filter: dict[str, Any] = field(default_factory=dict)

    def matches(self, event: str, payload: dict[str, Any]) -> bool:
        """Return True iff this trigger should fire for ``event`` + ``payload``.

        ``payload`` is the canonical event dict — for ``pr_*`` events it
        carries ``{"labels": [...], "branch": "...", "author": "..."}``.
        """
        if self.on != event:
            return False
        want_labels = self.filter.get("labels")
        if want_labels:
            have = set(payload.get("labels") or [])
            if not set(want_labels).issubset(have):
                return False
        for key in ("branch", "author"):
            want = self.filter.get(key)
            if want is not None and payload.get(key) != want:
                return False
        return True


@dataclass(frozen=True)
class Action:
    """What a role is allowed to do.

    Currently models two surfaces:

    - ``mcp_tools``: list of MCP tool names the role may call.
    - ``shell``: whether the role may invoke a shell (default False — most
      roles are review/comment-only).
    """

    mcp_tools: tuple[str, ...] = ()
    shell: bool = False


@dataclass(frozen=True)
class Role:
    name: str
    brief_template: str
    model: str
    timeout_s: int
    triggers: tuple[Trigger, ...]
    actions: tuple[Action, ...]
    brief_inline: bool = False
    budget_usd: float | None = None
    output_schema: str | None = None
    source_path: str | None = None  # filesystem origin for diagnostics

    # ------------------------------------------------------------------
    # Construction / validation
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Any, *, source: str | None = None) -> Role:
        """Validate a parsed YAML mapping and return a Role.

        Raises :class:`RoleSchemaError` with the field name on any failure.
        """
        if not isinstance(data, dict):
            raise RoleSchemaError(
                f"role yaml must be a mapping at top level, got {type(data).__name__}"
            )

        def _req(field_name: str, expected_type: type | tuple[type, ...]) -> Any:
            if field_name not in data:
                raise RoleSchemaError(f"missing required field {field_name!r}")
            value = data[field_name]
            if not isinstance(value, expected_type):
                wanted = (
                    expected_type.__name__
                    if isinstance(expected_type, type)
                    else "/".join(t.__name__ for t in expected_type)
                )
                raise RoleSchemaError(
                    f"field {field_name!r} must be {wanted}, "
                    f"got {type(value).__name__} (value={value!r})"
                )
            return value

        name = _req("name", str)
        if not _NAME_RE.match(name):
            raise RoleSchemaError(
                f"field 'name' {name!r} must match {_NAME_RE.pattern} "
                "(letters, digits, dash, underscore)"
            )

        brief_template = _req("brief_template", str)
        brief_inline = bool(data.get("brief_inline", False))

        model = _req("model", str)
        if not model.strip():
            raise RoleSchemaError("field 'model' must be non-empty")

        timeout_s = _req("timeout_s", int)
        if timeout_s <= 0:
            raise RoleSchemaError(f"field 'timeout_s' must be > 0, got {timeout_s}")

        budget_raw = data.get("budget_usd")
        budget_usd: float | None
        if budget_raw is None:
            budget_usd = None
        elif isinstance(budget_raw, bool) or not isinstance(budget_raw, int | float):
            raise RoleSchemaError(
                f"field 'budget_usd' must be a number or null, got {type(budget_raw).__name__}"
            )
        else:
            budget_usd = float(budget_raw)
            if budget_usd < 0:
                raise RoleSchemaError(f"field 'budget_usd' must be >= 0, got {budget_usd}")

        triggers_raw = data.get("triggers", [])
        if not isinstance(triggers_raw, list):
            raise RoleSchemaError(
                f"field 'triggers' must be a list, got {type(triggers_raw).__name__}"
            )
        triggers: list[Trigger] = []
        for i, t in enumerate(triggers_raw):
            if not isinstance(t, dict):
                raise RoleSchemaError(f"triggers[{i}] must be a mapping with key 'on' (got {t!r})")
            # PyYAML safe_load treats bare ``on:``/``off:`` as bool keys
            # (YAML 1.1). Accept both spellings so operators are not forced
            # to quote the key; recommend quoting in docs.
            if "on" in t:
                on = t["on"]
            elif True in t:
                on = t[True]
            else:
                raise RoleSchemaError(
                    f"triggers[{i}] must have an 'on' key (got keys {list(t)!r}) — "
                    'tip: quote the key as "on" to avoid YAML 1.1 boolean coercion'
                )
            if not isinstance(on, str):
                raise RoleSchemaError(f"triggers[{i}].on must be str, got {type(on).__name__}")
            filt = t.get("filter") or {}
            if not isinstance(filt, dict):
                raise RoleSchemaError(
                    f"triggers[{i}].filter must be a mapping, got {type(filt).__name__}"
                )
            triggers.append(Trigger(on=on, filter=dict(filt)))

        actions_raw = data.get("actions", [])
        if not isinstance(actions_raw, list):
            raise RoleSchemaError(
                f"field 'actions' must be a list, got {type(actions_raw).__name__}"
            )
        actions: list[Action] = []
        for i, a in enumerate(actions_raw):
            if not isinstance(a, dict):
                raise RoleSchemaError(f"actions[{i}] must be a mapping, got {type(a).__name__}")
            mcp_tools_raw = a.get("mcp_tools") or []
            if not isinstance(mcp_tools_raw, list) or not all(
                isinstance(x, str) for x in mcp_tools_raw
            ):
                raise RoleSchemaError(f"actions[{i}].mcp_tools must be a list[str]")
            shell = bool(a.get("shell", False))
            actions.append(Action(mcp_tools=tuple(mcp_tools_raw), shell=shell))

        output_schema = data.get("output_schema")
        if output_schema is not None and not isinstance(output_schema, str):
            raise RoleSchemaError(
                f"field 'output_schema' must be str or null, got {type(output_schema).__name__}"
            )

        return cls(
            name=name,
            brief_template=brief_template,
            brief_inline=brief_inline,
            model=model,
            timeout_s=timeout_s,
            budget_usd=budget_usd,
            triggers=tuple(triggers),
            actions=tuple(actions),
            output_schema=output_schema,
            source_path=source,
        )

    # ------------------------------------------------------------------
    # Dispatch helpers
    # ------------------------------------------------------------------
    def matches_event(self, event: str, payload: dict[str, Any]) -> bool:
        """Return True iff ANY of the role's triggers fires for the event."""
        return any(t.matches(event, payload) for t in self.triggers)

    def allowed_mcp_tools(self) -> tuple[str, ...]:
        """Flatten all mcp_tools entries across the role's actions."""
        out: list[str] = []
        for a in self.actions:
            out.extend(a.mcp_tools)
        return tuple(out)

    def shell_allowed(self) -> bool:
        return any(a.shell for a in self.actions)
