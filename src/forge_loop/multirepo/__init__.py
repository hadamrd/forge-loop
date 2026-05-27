"""Multirepo support — one loop instance serves N repos.

A loop deployment discovers repos under ``.forge/repos/*.yaml`` (relative to
the loop's home dir) and ticks each enabled repo round-robin. Each repo's
state / events / attempt-history live under its own checkout, so adding or
removing a repo never loses state.

See ``loader`` for the YAML schema + disable-flag semantics.
"""


from __future__ import annotations


# Experimental gate (issue #39): refuse to import unless the [experimental]
# extra is installed. Stable surface only in the default install.
from forge_loop._extras import require_experimental as _require_experimental
_require_experimental('multirepo')
from forge_loop.multirepo.loader import (
    DEFAULT_BUDGET_USD_PER_DAY,
    RepoLoadError,
    RepoSpec,
    build_config_for_repo,
    disable_repo,
    enable_repo,
    is_disabled,
    load_repos,
    validate_checkout,
)

__all__ = [
    "DEFAULT_BUDGET_USD_PER_DAY",
    "RepoLoadError",
    "RepoSpec",
    "build_config_for_repo",
    "disable_repo",
    "enable_repo",
    "is_disabled",
    "load_repos",
    "validate_checkout",
]
