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

The #144 guard **reported and refused** — it never mutated the operator's
site-packages. Issue #315 turns that refuse into a **self-heal**: a
worker-caused poison is a zero-HITL bricking class, so boot now auto-removes
the stray editable artifacts (behind the :class:`HealFilesystem` Protocol,
manifesto Q2) and continues. The heal is strictly NARROWER than detection —
see :func:`is_worktree_shaped` / :func:`plan_heal` — so it can only ever
remove forge-loop editable artifacts pointing into a worktree.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Iterable, Mapping
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
_EDITABLE_LOCATION_RE = re.compile(r"^Editable project location:\s*(?P<path>.+?)\s*$", re.MULTILINE)
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
    site = (
        site_packages
        or "$(python -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])')"
    )
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
    except KeyError:
        # ``purelib`` absent from this interpreter's install scheme — a
        # display-only hint, so degrade to the generic sysconfig snippet in
        # the cleanup command rather than failing (error-handling.md#EH-001).
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


# ---------------------------------------------------------------------------
# Self-heal (#315). Detection above stays a PURE refuse-or-not decision over a
# ``PipShowPayload`` (#144 contract, unchanged). #144 only *refused* to start,
# which made a worker-caused poison a zero-HITL brick (2026-06-07: every
# ``forge-loop run`` exited 3 for hours). Boot now auto-cleans and continues.
# The heal is deliberately NARROWER than detection: it fires ONLY for an
# unambiguously worktree-shaped offending path AND removes ONLY forge-loop
# editable artifacts, so it can never delete a uv-managed / canonical install
# or an unrelated package (AC #3). The mutation lives behind the
# :class:`HealFilesystem` Protocol (manifesto Q2) so it is Fake-unit-testable.
# ---------------------------------------------------------------------------

# Site-packages entries that are forge-loop editable-install artifacts. The
# ``.pth`` / finder re-point ``import forge_loop`` at the worktree; the
# ``forge_loop`` / ``roles`` shim dirs are the legacy direct-path leak. Prefix
# matching covers the version-suffixed forms pip emits
# (``__editable__.forge_loop-0.1.0.pth``).
_EDITABLE_ARTIFACT_PREFIXES: tuple[str, ...] = (
    "_editable_impl_forge_loop",
    "__editable__.forge_loop",
    "__editable___forge_loop",
    "forge_loop.egg-link",
)
_SHIM_DIR_NAMES: frozenset[str] = frozenset({"forge_loop", "roles"})


def _is_forge_loop_editable_artifact(name: str) -> bool:
    """True only for forge-loop editable artifacts — never unrelated packages."""
    if name in _SHIM_DIR_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _EDITABLE_ARTIFACT_PREFIXES)


def is_worktree_shaped(path: str | None) -> bool:
    """True when ``path`` carries an unambiguous ``wt-loop-*`` worktree segment.

    Stricter than :func:`_location_in_worktree` (which also accepts anything
    under the configured ``worktree_root``): the heal DELETES files, so it only
    fires on the unambiguous worker-worktree shape — never on a path that
    merely happens to sit under a configured root (AC #3). A non-worktree
    poison therefore preserves the #144 refuse path.
    """
    if not path:
        return False
    return _WT_LOOP_SEGMENT_RE.search(path.rstrip("/")) is not None


@dataclass(frozen=True)
class HealPlan:
    """Pure plan: the exact set of site-packages paths to remove.

    ``healable`` is ``False`` (and ``remove_paths`` empty) when the offending
    path is not worktree-shaped — the caller then preserves the #144 refuse
    path instead of deleting anything.
    """

    healable: bool
    offending_path: str | None = None
    remove_paths: tuple[str, ...] = field(default_factory=tuple)


def plan_heal(
    offending_path: str | None,
    *,
    sites: Mapping[str, Iterable[str]],
) -> HealPlan:
    """Pure heal planner (AC #6).

    Given a worktree-pointing editable and a listing of each operator site dir
    (``site_dir -> entry names``), return the EXACT paths to remove. Returns
    ``healable=False`` with no paths for a uv-managed / canonical /
    non-worktree-shaped location — nothing is ever planned for deletion there.
    Only forge-loop editable artifacts are ever included, so a non-forge-loop
    entry that merely sits under the same site dir is never touched (AC #3).
    Existence-independent: a dangling (reaped-worktree) offending path still
    plans removal of the artifacts that remain in the operator site.
    """
    if not is_worktree_shaped(offending_path):
        return HealPlan(healable=False, offending_path=offending_path)
    remove: list[str] = []
    for site_dir, entries in sites.items():
        for name in entries:
            if _is_forge_loop_editable_artifact(name):
                remove.append(str(Path(site_dir) / name))
    return HealPlan(
        healable=True,
        offending_path=offending_path,
        remove_paths=tuple(sorted(remove)),
    )


@dataclass(frozen=True)
class HealResult:
    """Outcome of an attempted heal.

    * ``healable=False`` → not worktree-shaped; caller preserves refuse path
      (emit ``boot_environment_poisoned``, exit 3).
    * ``healable=True, healed=True`` → artifacts removed; boot continues.
    * ``healable=True, healed=False`` → cleanup raised; caller emits
      ``boot_environment_heal_failed`` and falls back to refuse.
    """

    healable: bool
    healed: bool
    offending_path: str | None = None
    removed: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None


class HealFilesystem(Protocol):
    """Typed boundary (manifesto Q2) around the mutating site-packages cleanup."""

    def list_site_entries(self, site_dir: str) -> tuple[str, ...]:
        """Return the entry names directly under ``site_dir`` (``()`` if absent)."""
        ...

    def remove(self, path: str) -> None:
        """Remove a file or directory tree at ``path`` (best-effort)."""
        ...


def heal_poison(
    offending_path: str | None,
    *,
    site_dirs: Iterable[str],
    fs: HealFilesystem,
) -> HealResult:
    """Plan + execute the heal behind the :class:`HealFilesystem` boundary.

    Best-effort like the #144 guard: any failure during listing/removal is
    captured into ``error`` (``healed=False``) rather than raised, so boot can
    fall back to refuse instead of crashing (AC #4). A dangling offending path
    (worktree already reaped) still heals — the artifacts live in the operator
    site, not the reaped worktree, so we never depend on the target existing.
    """
    if not is_worktree_shaped(offending_path):
        return HealResult(healable=False, healed=False, offending_path=offending_path)
    removed: list[str] = []
    try:
        sites = {site_dir: fs.list_site_entries(site_dir) for site_dir in site_dirs}
        plan = plan_heal(offending_path, sites=sites)
        for path in plan.remove_paths:
            fs.remove(path)
            removed.append(path)
    except Exception as exc:  # noqa: BLE001 — heal is best-effort; never crash boot
        return HealResult(
            healable=True,
            healed=False,
            offending_path=offending_path,
            removed=tuple(removed),
            error=f"{type(exc).__name__}: {exc}",
        )
    return HealResult(
        healable=True,
        healed=True,
        offending_path=offending_path,
        removed=tuple(removed),
    )


def operator_site_dirs() -> tuple[str, ...]:
    """Operator interpreter site dirs to scan for stray editable artifacts.

    Unions the global ``site.getsitepackages()``, the user site
    (``~/.local/lib/...`` — where the 2026-06-07 incident landed), and the
    ``sysconfig`` purelib, de-duplicated. Each source is best-effort: an
    interpreter that doesn't expose one simply contributes nothing.
    """
    import site as _site

    dirs: list[str] = []
    with contextlib.suppress(Exception):
        dirs.extend(_site.getsitepackages())
    with contextlib.suppress(Exception):
        user = _site.getusersitepackages()
        if user:
            dirs.append(user)
    purelib = current_site_packages()
    if purelib:
        dirs.append(purelib)
    return tuple(dict.fromkeys(dirs))


@dataclass(frozen=True)
class RealHealFilesystem:
    """Real :class:`HealFilesystem` over the operator's filesystem."""

    def list_site_entries(self, site_dir: str) -> tuple[str, ...]:
        try:
            return tuple(os.listdir(site_dir))
        except OSError:
            return ()

    def remove(self, path: str) -> None:
        target = Path(path)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
