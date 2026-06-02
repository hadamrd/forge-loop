"""File-backed storage for frontier cursors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from forge_loop.frontier.cursor import FrontierCursor, HotArtifact, RejectedPath

_REQUIRED_FIELDS = ("product_goal", "current_problem", "next_expansion", "why_now")


class FrontierStore:
    """Load and save a frontier cursor as operator-readable YAML."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> FrontierCursor:
        """Load a frontier cursor from YAML."""
        raw = yaml.safe_load(self.path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("frontier cursor yaml must be a mapping")

        for field in _REQUIRED_FIELDS:
            value = raw.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"frontier cursor missing required field: {field}")

        return FrontierCursor(
            product_goal=raw["product_goal"],
            current_problem=raw["current_problem"],
            next_expansion=raw["next_expansion"],
            why_now=raw["why_now"],
            active_decisions=_string_tuple(raw.get("active_decisions", ()), "active_decisions"),
            rejected_paths=_rejected_paths(raw.get("rejected_paths", ())),
            hot_files=_hot_artifacts(raw.get("hot_files", ()), "hot_files"),
            hot_tests=_hot_artifacts(raw.get("hot_tests", ()), "hot_tests"),
            open_questions=_string_tuple(raw.get("open_questions", ()), "open_questions"),
            external_sources=_string_tuple(raw.get("external_sources", ()), "external_sources"),
            version=_version(raw.get("version", 1)),
        )

    def save(self, cursor: FrontierCursor) -> None:
        """Save a frontier cursor to YAML, creating parent directories."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(_to_yaml_data(cursor), sort_keys=False))


def _to_yaml_data(cursor: FrontierCursor) -> dict[str, Any]:
    return {
        "product_goal": cursor.product_goal,
        "current_problem": cursor.current_problem,
        "next_expansion": cursor.next_expansion,
        "why_now": cursor.why_now,
        "active_decisions": list(cursor.active_decisions),
        "rejected_paths": [
            {
                "idea": path.idea,
                "reason": path.reason,
                "revisit_if": path.revisit_if,
            }
            for path in cursor.rejected_paths
        ],
        "hot_files": [
            {
                "ref": artifact.ref,
                "why_hot": artifact.why_hot,
            }
            for artifact in cursor.hot_files
        ],
        "hot_tests": [
            {
                "ref": artifact.ref,
                "why_hot": artifact.why_hot,
            }
            for artifact in cursor.hot_tests
        ],
        "open_questions": list(cursor.open_questions),
        "external_sources": list(cursor.external_sources),
        "version": cursor.version,
    }


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"frontier cursor field {field} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"frontier cursor field {field} must contain only strings")
    return tuple(value)


def _rejected_paths(value: object) -> tuple[RejectedPath, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("frontier cursor field rejected_paths must be a list")
    return tuple(_rejected_path(item) for item in value)


def _rejected_path(value: object) -> RejectedPath:
    if not isinstance(value, dict):
        raise ValueError("frontier cursor field rejected_paths must contain mappings")
    idea = value.get("idea")
    reason = value.get("reason")
    revisit_if = value.get("revisit_if", "")
    if not isinstance(idea, str):
        raise ValueError("frontier cursor rejected_paths entry missing field: idea")
    if not isinstance(reason, str):
        raise ValueError("frontier cursor rejected_paths entry missing field: reason")
    if not isinstance(revisit_if, str):
        raise ValueError("frontier cursor rejected_paths entry field revisit_if must be a string")
    return RejectedPath(idea=idea, reason=reason, revisit_if=revisit_if)


def _hot_artifacts(value: object, field: str) -> tuple[HotArtifact, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"frontier cursor field {field} must be a list")
    return tuple(_hot_artifact(item, field) for item in value)


def _hot_artifact(value: object, field: str) -> HotArtifact:
    if not isinstance(value, dict):
        raise ValueError(f"frontier cursor field {field} must contain mappings")
    ref = value.get("ref")
    why_hot = value.get("why_hot")
    if not isinstance(ref, str):
        raise ValueError(f"frontier cursor {field} entry missing field: ref")
    if not isinstance(why_hot, str):
        raise ValueError(f"frontier cursor {field} entry missing field: why_hot")
    return HotArtifact(ref=ref, why_hot=why_hot)


def _version(value: object) -> int:
    if not isinstance(value, int):
        raise ValueError("frontier cursor field version must be an integer")
    return value
