"""Object-oriented command handlers for the forge-loop CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_loop.cli_operator_commands import OperatorCommandsMixin
from forge_loop.cli_product_commands import ProductCommandsMixin
from forge_loop.cli_repo_commands import RepoCommandsMixin
from forge_loop.cli_status_commands import StatusCommandsMixin
from forge_loop.cli_workflow_commands import WorkflowCommandsMixin


def _default_memory_store_factory(repo_path: Any) -> Any:
    """Construct the durable memory store at ``.forge/memory.db``."""
    from forge_loop.memory import SqliteMemoryStore

    return SqliteMemoryStore(Path(repo_path) / ".forge" / "memory.db")


class CliCommands(
    OperatorCommandsMixin,
    StatusCommandsMixin,
    ProductCommandsMixin,
    WorkflowCommandsMixin,
    RepoCommandsMixin,
):
    """Command implementation object used by the thin Typer facade."""

    def __init__(
        self,
        *,
        load_fn: Callable[[], Any],
        run_loop_fn: Callable[[Any], int],
        operator_cfg_fn: Callable[[], tuple[Any, str | None]],
        brainstormer_factory: Callable[..., Any],
        gh_client_factory: Callable[[], Any],
        subprocess_module: Any,
        memory_store_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.load = load_fn
        self.run_loop = run_loop_fn
        self.operator_cfg = operator_cfg_fn
        self.brainstormer_factory = brainstormer_factory
        self.gh_client_factory = gh_client_factory
        self.memory_store_factory = memory_store_factory or _default_memory_store_factory
        self.subprocess = subprocess_module
