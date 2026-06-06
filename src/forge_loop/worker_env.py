"""Worker toolchain/environment contract — provision + preflight (issue: the
2026-06-05 silent-toolchain incident).

Root cause that motivated this module: workers run in a ``/tmp`` git worktree
and inherit the orchestrator's ambient env (``_worker_sdk._clean_sdk_env`` =
``dict(os.environ)``). The project ``.venv`` was NOT on that PATH (workers saw
``VIRTUAL_ENV=/usr`` and the system python), so ``pyright`` / ``mypy`` /
``pytest`` silently failed with "command not found"; the worker retried
variants for ~20 minutes with NO error surfaced. The worker's toolchain was an
IMPLICIT, unverified, silently-degrading dependency.

This module makes the contract EXPLICIT:

* :func:`build_worker_env` — pure provisioning. Given the cleaned base env plus
  the declared ``path_prepend`` / ``vars``, it returns a new env dict with the
  declared dirs prepended to ``PATH`` and the declared vars set (repo-relative
  path values resolved to absolute against the repo root).
* :func:`missing_tools` — pure preflight. Given an env and the list of required
  tools, returns the tools that do NOT resolve on that env's ``PATH``.

Both are pure (no global state, no mutation of the input) so the dispatch path
can build the env, verify it, and fail LOUD before driving a doomed session.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge_loop.sandbox.policy import CapabilityPolicy

__all__ = [
    "build_worker_env",
    "missing_tools",
    "scope_secrets",
    "secret_shaped_keys",
]

# Case-insensitive substrings that mark an env key as carrying a secret value
# (issue #283). A key matching ANY of these is withheld from the worker child
# env unless it is named in the lease's ``secret_names``.
_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)


def _is_secret_shaped(name: str) -> bool:
    """Does ``name`` look like it carries a secret (token/key/password/…)?

    Case-insensitive substring match against :data:`_SECRET_KEY_PATTERNS` so
    ``ANTHROPIC_API_KEY``, ``GITHUB_TOKEN``, ``AWS_SECRET_ACCESS_KEY`` and
    ``DB_PASSWORD`` are all flagged while ``PATH``/``HOME``/``LANG`` are not.
    """
    upper = name.upper()
    return any(pattern in upper for pattern in _SECRET_KEY_PATTERNS)


def secret_shaped_keys(base: Mapping[str, str]) -> tuple[str, ...]:
    """Every secret-shaped key present in ``base`` (sorted, stable).

    The "keep all my secrets" lease for a TRUSTED caller (e.g. the critic
    reviewer, not a sandboxed worker): pass the result as ``secret_names`` so
    :func:`scope_secrets`'s closed fail-safe default does not strip the
    credentials the trusted process was launched with (issue #283 / PR #289
    review). Returns key NAMES only — never values.
    """
    return tuple(sorted(k for k in base if _is_secret_shaped(k)))


def scope_secrets(
    base: Mapping[str, str],
    policy: CapabilityPolicy | None,
) -> tuple[dict[str, str], list[str]]:
    """Scope ``base`` to the secrets the lease grants (issue #283).

    Enforces the secret dimension of the capability lease at spawn so a
    least-privilege worker no longer inherits the operator's ENTIRE secret env.

    * Every key named in ``policy.secret_names`` that exists in ``base`` is
      preserved.
    * Every secret-shaped key (see :func:`_is_secret_shaped`) NOT in the lease
      is withheld from the returned env.
    * Every non-secret-shaped key (``PATH``, ``HOME``, ``VIRTUAL_ENV``, …)
      passes through untouched.

    A ``None`` (or empty) policy withholds ALL secret-shaped keys — fail safe,
    not open. The input mapping is never mutated; a brand-new ``dict`` is
    returned alongside the sorted list of withheld key NAMES (never values).
    """
    leased = frozenset(policy.secret_names) if policy is not None else frozenset()
    env: dict[str, str] = {}
    withheld: list[str] = []
    for key, value in base.items():
        if _is_secret_shaped(key) and key not in leased:
            withheld.append(key)
            continue
        env[key] = value
    return env, sorted(withheld)


def _looks_like_repo_relative_path(value: str, repo: Path) -> bool:
    """Heuristic: does ``value`` look like a repo-relative path to resolve?

    We resolve a var value against the repo root when it has no absolute
    anchor AND either:
      * contains a path separator (".venv/bin", "a/b"), or
      * names an entry that actually exists under the repo (so a bare
        ``VIRTUAL_ENV: ".venv"`` — the documented contract example — rebases
        to the absolute venv path), or
      * is a leading-dot dir name (".venv", ".tox") — a strong path signal
        even before the dir exists.

    A bare token like ``"1"`` or ``"true"`` is left verbatim. Absolute values
    are never touched.
    """
    if not value or os.path.isabs(value):
        return False
    if os.sep in value or (os.altsep is not None and os.altsep in value):
        return True
    if value.startswith("."):
        return True
    return (repo / value).exists()


def build_worker_env(
    base: Mapping[str, str],
    *,
    repo: Path,
    path_prepend: Iterable[str] = (),
    vars: Mapping[str, str] | Iterable[tuple[str, str]] = (),
) -> dict[str, str]:
    """Provision the worker env from ``base`` + the declared contract.

    * Starts from a *copy* of ``base`` (the cleaned ``os.environ``) — the
      input mapping is never mutated.
    * Sets each declared var. A value that looks like a repo-relative path
      (see :func:`_looks_like_repo_relative_path`) is resolved to an absolute
      path against ``repo`` so e.g. ``VIRTUAL_ENV: ".venv"`` lands as the
      worktree-independent absolute venv path.
    * Prepends each ``path_prepend`` dir (resolved absolute against ``repo``)
      to ``PATH`` using :data:`os.pathsep`, de-duplicated, earliest-wins.
      Dirs are prepended regardless of whether they exist on disk — the
      preflight (:func:`missing_tools`) is what fails loud on a missing tool,
      so provisioning stays a pure string operation with no filesystem race.

    Returns a brand-new ``dict[str, str]``.
    """
    repo = Path(repo)
    env: dict[str, str] = dict(base)

    # Normalise vars to a concrete pair list (accept Mapping or iterable of
    # pairs). Materialised explicitly so the loop type is unambiguous.
    pairs: list[tuple[str, str]]
    if isinstance(vars, Mapping):
        pairs = [(str(k), str(v)) for k, v in vars.items()]
    else:
        pairs = [(str(k), str(v)) for k, v in vars]
    for key, value in pairs:
        if _looks_like_repo_relative_path(value, repo):
            env[key] = str((repo / value).resolve())
        else:
            env[key] = value

    # Prepend declared dirs (absolute, repo-rebased) to PATH, de-duped.
    prepend_abs: list[str] = []
    seen: set[str] = set()
    for raw in path_prepend:
        if not raw:
            continue
        p = Path(raw)
        abs_dir = str((p if p.is_absolute() else repo / p).resolve())
        if abs_dir not in seen:
            seen.add(abs_dir)
            prepend_abs.append(abs_dir)

    if prepend_abs:
        existing = env.get("PATH", "")
        existing_parts = [p for p in existing.split(os.pathsep) if p]
        # Drop any existing entry that we are about to prepend so the declared
        # dir wins precedence rather than appearing twice.
        tail = [p for p in existing_parts if p not in seen]
        env["PATH"] = os.pathsep.join([*prepend_abs, *tail])

    return env


def missing_tools(env: Mapping[str, str], require: Iterable[str]) -> list[str]:
    """Return the required tools that do NOT resolve on ``env``'s PATH.

    Resolution uses :func:`shutil.which` scoped to ``env["PATH"]`` (not the
    process PATH) so the check reflects exactly the env the worker will run
    with. Order-preserving; a satisfied requirement set yields ``[]``.
    """
    path = env.get("PATH", "")
    missing: list[str] = []
    for tool in require:
        if not tool:
            continue
        if shutil.which(tool, path=path) is None:
            missing.append(tool)
    return missing
