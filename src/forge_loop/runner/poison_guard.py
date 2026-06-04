"""Boot-time guard against a poisoned operator Python environment.

Background (#144). A forge-loop worker that runs ``pip install -e .`` from
its ephemeral worktree creates an *editable* install in the **operator's**
system Python, e.g.::

    ~/.local/lib/python3.12/site-packages/forge_loop -> /tmp/forge-<repo>/wt-loop-124/src

This silently shadows the uv-managed install: ``import forge_loop`` then
resolves to the worker's half-finished code, and once the worktree is reaped
the editable ``.pth`` dangles and ``import forge_loop`` degrades to a
namespace package with no ``__file__``. The fix is two cheap guardrails: a
worker-brief prohibition (so workers don't do it) and *this* boot guard (so
an already-poisoned environment is detected and the loop refuses to start
with a copy-pasteable cleanup command).

Design (AC #144): the detection logic is **pure** and unit-testable. It
consumes a parsed ``pip show`` payload plus a worktree-root prefix and
returns a structured :class:`PoisonResult` — it never shells out itself.
The live ``pip show`` call is hidden behind the :class:`PipShowReader`
Protocol (manifesto Q2) with a real subprocess impl here and a
``FakePipShowReader`` under ``forge_loop/_testing/`` for tests.

The guard **reports and refuses**; it never mutates the operator's
site-packages (that is explicitly out of scope for #144).
"""

from __future__ import annotations

import re
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# A path segment shaped like a worker worktree: ``wt-loop-<issue>`` anywhere
# in the path. This catches the legacy flat ``/tmp/wt-loop-124`` scheme AND
# the per-repo ``/tmp/forge-<repo>/wt-loop-124`` scheme even when the caller's
# configured ``worktree_root`` prefix does not line up exactly.
_WT_LOOP_SEGMENT_RE = re.compile(r"(?:^|/)wt-loop-[^/]*(?:/|$)")

# ``pip show`` emits ``Location:`` for every package; modern pip (>=21.3) adds
# ``Editable project location:`` for editable installs. We prefer the editable
# line when present, else fall back to ``Location:``.
_EDITABLE_LOCATION_RE = re.compile(
    r"^Editable project location:\s*(?P<path>.+?)\s*$", re.MULTILINE
)
_LOCATION_RE = re.compile(r"^Location:\s*(?P<path>.+?)\s*$", re.MULTILINE)

DEFAULT_PACKAGE = "forge-loop"


@dataclass(frozen=True)
class PipShowPayload:
    """A parsed ``pip show <package>`` result.

    ``returncode`` is non-zero when the package is not installed on the
    inspected interpreter (``pip show`` exits 1). ``stdout`` is the raw
    metadata block. Keeping this a plain value object is what lets the
    detection logic stay pure and unit-testable without a live subprocess.
    """

    returncode: int
    stdout: str = ""


@dataclass(frozen=True)
class PoisonResult:
    """Structured outcome of the poison check.

    ``poisoned`` is the single discriminator the boot path branches on.
    ``offending_path`` is the exact editable ``Location:`` that points into a
    worktree (``None`` when not poisoned). ``cleanup_commands`` is the exact,
    copy-pasteable remediation the operator should run.
    """

    poisoned: bool
    offending_path: str | None = None
    cleanup_commands: tuple[str, ...] = field(default_factory=tuple)

    def render_error(self, *, package: str = DEFAULT_PACKAGE) -> str:
        """A clear, copy-pasteable refuse-to-start message for stderr."""
        lines = [
            f"FATAL: operator Python environment is poisoned by an editable "
            f"{package!r} install pointing into a worker worktree.",
            f"  offending Location: {self.offending_path}",
            "",
            "This shadows the uv-managed install with a worker's half-finished",
            "code (see issue #144). forge-loop refuses to start until it is",
            "cleaned up. Run:",
            "",
            *(f"    {cmd}" for cmd in self.cleanup_commands),
            "",
        ]
        return "\n".join(lines)


class EnvironmentPoisonedError(RuntimeError):
    """Raised/surfaced when the boot guard detects a poisoned environment."""

    def __init__(self, result: PoisonResult, *, package: str = DEFAULT_PACKAGE) -> None:
        self.result = result
        super().__init__(result.render_error(package=package))


def _extract_location(stdout: str) -> str | None:
    """Return the editable location (preferred) or plain ``Location:`` value."""
    editable = _EDITABLE_LOCATION_RE.search(stdout)
    if editable is not None:
        value = editable.group("path").strip()
        if value:
            return value
    plain = _LOCATION_RE.search(stdout)
    if plain is not None:
        value = plain.group("path").strip()
        if value:
            return value
    return None


def _location_in_worktree(location: str, worktree_root: str | Path | None) -> bool:
    """True when ``location`` points into a worker worktree.

    Matches either the generic ``wt-loop-*`` path shape (existence-independent
    — the worktree may already be reaped) or a path under the configured
    ``worktree_root`` prefix.
    """
    norm = location.rstrip("/")
    if _WT_LOOP_SEGMENT_RE.search(norm):
        return True
    if worktree_root is None:
        return False
    root = str(worktree_root).rstrip("/")
    if not root:
        return False
    return norm == root or norm.startswith(root + "/")


def _cleanup_commands(
    offending_path: str,
    *,
    package: str,
    site_packages: str | None,
    reinstall_target: str,
) -> tuple[str, ...]:
    site = site_packages or "$(python -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])')"
    return (
        f"python -m pip uninstall -y {package}",
        f"rm -rf {site}/forge_loop {site}/roles",
        f"uv tool install --reinstall --force {reinstall_target}",
    )


def detect_poisoned_environment(
    payload: PipShowPayload,
    *,
    worktree_root: str | Path | None,
    package: str = DEFAULT_PACKAGE,
    site_packages: str | None = None,
    reinstall_target: str = ".",
) -> PoisonResult:
    """Pure poison detection over a parsed ``pip show`` payload.

    Returns ``poisoned=True`` only when the package IS installed on the
    inspected interpreter (returncode 0) AND its editable ``Location:`` points
    into a worker worktree. A non-zero ``returncode`` (package absent from the
    system interpreter — the healthy uv-managed-only case) is **not** an error:
    it returns ``poisoned=False``.
    """
    if payload.returncode != 0:
        return PoisonResult(poisoned=False)
    location = _extract_location(payload.stdout)
    if location is None:
        return PoisonResult(poisoned=False)
    if not _location_in_worktree(location, worktree_root):
        return PoisonResult(poisoned=False)
    return PoisonResult(
        poisoned=True,
        offending_path=location,
        cleanup_commands=_cleanup_commands(
            location,
            package=package,
            site_packages=site_packages,
            reinstall_target=reinstall_target,
        ),
    )


class PipShowReader(Protocol):
    """Typed boundary (manifesto Q2) around ``pip show`` metadata inspection."""

    def pip_show(self, package: str) -> PipShowPayload:
        """Return parsed ``pip show <package>`` for the inspected interpreter."""
        ...


@dataclass(frozen=True)
class SubprocessPipShowReader:
    """Real :class:`PipShowReader`: runs ``<python> -m pip show`` once.

    ``python_executable`` defaults to the running interpreter — i.e. the
    operator's system Python that launched ``forge-loop run`` — which is
    exactly the environment that can be poisoned.
    """

    python_executable: str = sys.executable
    timeout_s: float = 30.0

    def pip_show(self, package: str) -> PipShowPayload:
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [self.python_executable, "-m", "pip", "show", package],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # pip missing / interpreter gone / timeout → treat as "not
            # installed" rather than crashing the boot path.
            return PipShowPayload(returncode=1, stdout="")
        return PipShowPayload(returncode=proc.returncode, stdout=proc.stdout)


def current_site_packages() -> str | None:
    """Best-effort purelib of the running interpreter, for cleanup commands."""
    try:
        return sysconfig.get_paths()["purelib"]
    except Exception:  # noqa: BLE001 — best-effort hint only
        return None


def check_environment_not_poisoned(
    reader: PipShowReader,
    *,
    worktree_root: str | Path | None,
    package: str = DEFAULT_PACKAGE,
    reinstall_target: str = ".",
    site_packages: str | None = None,
) -> PoisonResult:
    """Inspect the live environment via ``reader`` and run pure detection."""
    payload = reader.pip_show(package)
    return detect_poisoned_environment(
        payload,
        worktree_root=worktree_root,
        package=package,
        site_packages=site_packages if site_packages is not None else current_site_packages(),
        reinstall_target=reinstall_target,
    )
