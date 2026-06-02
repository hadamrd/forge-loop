"""Product frontier cursor.

The frontier cursor is not a backlog. It is the compact, boot-loadable theory
of where the product should expand next and why.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HotArtifact:
    """A file, test, command, or index that should be loaded early on boot."""

    ref: str
    why_hot: str


@dataclass(frozen=True)
class RejectedPath:
    """An approach that should not be rediscovered without new evidence."""

    idea: str
    reason: str
    revisit_if: str = ""


@dataclass(frozen=True)
class FrontierCursor:
    """Compact durable cursor for maestro boot and planning."""

    product_goal: str
    current_problem: str
    next_expansion: str
    why_now: str
    active_decisions: tuple[str, ...] = ()
    rejected_paths: tuple[RejectedPath, ...] = ()
    hot_files: tuple[HotArtifact, ...] = ()
    hot_tests: tuple[HotArtifact, ...] = ()
    open_questions: tuple[str, ...] = ()
    external_sources: tuple[str, ...] = ()
    version: int = 1

    def boot_summary(self) -> str:
        """Render the minimum useful human/agent summary for reset recovery."""
        parts = [
            f"goal: {self.product_goal}",
            f"current: {self.current_problem}",
            f"next: {self.next_expansion}",
            f"why_now: {self.why_now}",
        ]
        if self.active_decisions:
            parts.append("decisions: " + "; ".join(self.active_decisions))
        if self.open_questions:
            parts.append("open_questions: " + "; ".join(self.open_questions))
        return "\n".join(parts)
