"""Object-oriented command handlers for the forge-loop CLI."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forge_loop.cli_operator_commands import OperatorCommandsMixin
from forge_loop.cli_product_commands import ProductCommandsMixin
from forge_loop.cli_repo_commands import RepoCommandsMixin
from forge_loop.cli_status_commands import StatusCommandsMixin
from forge_loop.cli_workflow_commands import WorkflowCommandsMixin


def _default_memory_store_factory(repo_path: Any) -> Any:
    """Construct the durable memory store at ``.forge/memory.db``."""
    from forge_loop.memory import open_memory_store

    return open_memory_store(repo_path)


def _default_manifesto_suggester_factory(
    repo_path: Any,
    owner: str,
    repo: str,
    *,
    gh_client: Any = None,
    model: Any = None,
    timeout_s: int = 300,
) -> Any:
    """Construct the default ManifestoSuggester (issue #134). Tests stub this."""
    from forge_loop import gh as gh_module
    from forge_loop.manifesto_suggest import ManifestoSuggester

    return ManifestoSuggester(
        repo_path=repo_path,
        owner=owner,
        repo=repo,
        gh_client=gh_client,
        gh_module=gh_module,
        timeout_s=timeout_s,
        model=model,
    )


def _default_manifesto_pr_opener(plan: Any, **kwargs: Any) -> str:
    """Open the manifesto-suggest PR (issue #134). Tests stub this."""
    from forge_loop.manifesto_suggest import open_manifesto_pr

    return open_manifesto_pr(plan, **kwargs)


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
        manifesto_suggester_factory: Callable[..., Any] | None = None,
        manifesto_pr_opener: Callable[..., Any] | None = None,
    ) -> None:
        self.load = load_fn
        self.run_loop = run_loop_fn
        self.operator_cfg = operator_cfg_fn
        self.brainstormer_factory = brainstormer_factory
        self.gh_client_factory = gh_client_factory
        self.memory_store_factory = memory_store_factory or _default_memory_store_factory
        self.manifesto_suggester_factory = (
            manifesto_suggester_factory or _default_manifesto_suggester_factory
        )
        self.manifesto_pr_opener = manifesto_pr_opener or _default_manifesto_pr_opener
        self.subprocess = subprocess_module
