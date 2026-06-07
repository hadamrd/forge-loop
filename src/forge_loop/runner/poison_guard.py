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

Self-heal (#315). A guard whose only exit is human intervention is a
zero-HITL violation: the 2026-06-07 incident bricked *every* ``forge-loop
run`` for hours because a worker poisoned the operator site and boot could
only ``return 3``. The detection above stays pure; the **mutation** that
neutralizes a worker-caused poison lives behind the :class:`SiteHealer`
Protocol (manifesto Q2/Q6) so it is unit-testable with a Fake. The
:func:`plan_heal` *planner* is pure and SCOPED & SAFE (AC #3): it only
plans removal when the offending ``Location:`` is worktree-shaped, so a
uv-managed install or a canonical-checkout editable is never touched and
the old refuse-to-start path is preserved.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from enum import StrEnum
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
# Self-heal (#315): plan (pure) → SiteHealer Protocol (mutation) → orchestrate.
# ---------------------------------------------------------------------------


class HealOutcome(StrEnum):
    """Cross-module discriminator for the heal result (manifesto enum rule).

    ``HEALED`` — the stray editable was neutralized; boot may continue.
    ``NOT_HEALABLE`` — the poison is NOT worktree-shaped (a uv-managed or
    canonical-checkout editable); the caller preserves the refuse-to-start
    path. ``FAILED`` — cleanup itself raised; the caller falls back to refuse.
    """

    HEALED = "healed"
    NOT_HEALABLE = "not_healable"
    FAILED = "failed"


@dataclass(frozen=True)
class HealPlan:
    """Pure plan: scan ``site_packages`` and neutralize forge-loop editable
    artifacts (``.pth`` / editable finder / dist-info / shim symlink) that point
    into ``offending_path`` (a worker worktree) so the uv-managed install
    re-surfaces. ``module`` is the import name (``forge_loop``)."""

    offending_path: str
    site_packages: str
    module: str = "forge_loop"


@dataclass(frozen=True)
class HealResult:
    """Structured outcome of a heal attempt (see :class:`HealOutcome`)."""

    outcome: HealOutcome
    offending_path: str | None = None
    removed: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None


def plan_heal(
    result: PoisonResult,
    *,
    site_packages: str | None,
    package: str = DEFAULT_PACKAGE,
) -> HealPlan | None:
    """Pure heal planner (AC #1/#3/#6).

    Returns a :class:`HealPlan` ONLY when the poison is safely healable: the
    result is poisoned, the offending ``Location:`` is worktree-shaped, and we
    know which operator site to scan. Returns ``None`` — so the caller keeps the
    old refuse-to-start behavior — when the offending path is NOT worktree-shaped
    (a uv-managed / canonical-checkout editable) or the site is unknown. The
    planner never touches the filesystem; it only decides whether a heal is in
    scope and what to scan.
    """
    if not result.poisoned or result.offending_path is None:
        return None
    if not _location_in_worktree(result.offending_path, None):
        return None
    if not site_packages:
        return None
    return HealPlan(
        offending_path=result.offending_path,
        site_packages=site_packages,
        module=package.replace("-", "_"),
    )


_POINTER_SUFFIXES = (".pth", ".py", ".egg-link")


def _is_forge_editable_artifact(name: str, module: str) -> bool:
    """True when ``name`` is a forge-loop editable POINTER artifact in the site.

    By the single-editable invariant (a site holds at most one editable install
    of a package) and the fact that we only reach a heal when detection proved
    forge-loop's editable points into a worktree, these artifacts ARE the
    poison. Matches editable ``.pth`` / finder ``.py`` / ``.egg-link`` /
    ``.dist-info`` / ``.egg-info`` whose name carries the module, plus the
    legacy bare ``roles.pth`` shim. A non-forge-loop name never matches (AC #3).
    """
    low = name.lower().replace("-", "_")
    mod = module.lower()
    if name.endswith((".dist-info", ".egg-info")) and low.startswith(mod):
        return True
    if not name.endswith(_POINTER_SUFFIXES):
        return False
    if mod not in low:
        return low == "roles.pth"
    # forge-loop-named pointer: an editable finder/impl, a legacy .pth, or an
    # egg-link. A plain ``.py`` that is NOT editable-shaped is left alone.
    return "editable" in low or name.endswith((".pth", ".egg-link"))


class SiteHealer(Protocol):
    """Typed boundary (manifesto Q2) around the operator-site MUTATION.

    Detection stays pure; this is the ONLY seam that removes files, so the heal
    is unit-testable with a ``Fake`` while production wires the real
    :class:`FilesystemSiteHealer`.
    """

    def heal(self, plan: HealPlan) -> tuple[str, ...]:
        """Remove the planned editable artifacts pointing into the worktree and
        return the absolute paths actually removed. Raise on an irrecoverable IO
        error (the orchestrator turns that into a ``FAILED`` outcome)."""
        ...


@dataclass(frozen=True)
class FilesystemSiteHealer:
    """Real :class:`SiteHealer` — neutralizes forge-loop editable artifacts.

    SCOPED & SAFE (AC #3): it removes a path ONLY when that path is provably a
    forge-loop editable artifact (name carries the module) OR a
    ``forge_loop``/``roles`` SYMLINK whose target is worktree-shaped. A real
    (non-symlink) package directory, a uv-managed install (which lives in uv's
    own venv, not the operator site), and any unrelated package are never
    touched. Existence-independent: a dangling ``.pth`` whose worktree was
    already reaped is still removed.
    """

    def heal(self, plan: HealPlan) -> tuple[str, ...]:
        site = Path(plan.site_packages)
        if not site.is_dir():
            return ()
        removed: list[str] = []
        for entry in sorted(site.iterdir()):
            if self._should_remove(entry, plan):
                self._remove(entry)
                removed.append(str(entry))
        return tuple(removed)

    def _should_remove(self, entry: Path, plan: HealPlan) -> bool:
        name = entry.name
        if entry.is_symlink() and name in (plan.module, "roles"):
            try:
                target = os.readlink(entry)
            except OSError:
                return False
            return _location_in_worktree(target, None)
        return _is_forge_editable_artifact(name, plan.module)

    @staticmethod
    def _remove(entry: Path) -> None:
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)


def heal_poisoned_environment(
    result: PoisonResult,
    healer: SiteHealer,
    *,
    site_packages: str | None,
    package: str = DEFAULT_PACKAGE,
) -> HealResult:
    """Orchestrate a heal: plan (pure) → mutate (behind ``healer``).

    Best-effort like the existing guard (AC #4): a planner that declines returns
    ``NOT_HEALABLE`` (caller refuses), and a healer that raises is caught and
    returned as ``FAILED`` (caller emits ``heal_failed`` + refuses) — never an
    unhandled exception out of boot.
    """
    plan = plan_heal(result, site_packages=site_packages, package=package)
    if plan is None:
        return HealResult(outcome=HealOutcome.NOT_HEALABLE, offending_path=result.offending_path)
    try:
        removed = healer.heal(plan)
    except Exception as exc:  # noqa: BLE001 — cleanup failure falls back to refuse, never crashes boot
        return HealResult(
            outcome=HealOutcome.FAILED,
            offending_path=result.offending_path,
            error=f"{type(exc).__name__}: {exc}",
        )
    return HealResult(
        outcome=HealOutcome.HEALED,
        offending_path=result.offending_path,
        removed=tuple(removed),
    )
