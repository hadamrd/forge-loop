"""Boot context assembly for maestro reset recovery."""

from __future__ import annotations

from dataclasses import dataclass

from forge_loop.frontier import FrontierCursor


@dataclass(frozen=True)
class BootContext:
    """Minimum strategic context a maestro needs after a reset."""

    frontier: FrontierCursor
    active_memory_ids: tuple[str, ...] = ()
    in_flight_task_ids: tuple[str, ...] = ()
    last_event_sequence: int = 0

    def summary(self) -> str:
        """Human-readable reset context for logs, prompts, and status views."""
        lines = [self.frontier.boot_summary()]
        if self.active_memory_ids:
            lines.append("memory: " + ", ".join(self.active_memory_ids))
        if self.in_flight_task_ids:
            lines.append("in_flight: " + ", ".join(self.in_flight_task_ids))
        lines.append(f"event_sequence: {self.last_event_sequence}")
        return "\n".join(lines)
