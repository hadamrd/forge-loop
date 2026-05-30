"""Assert the default-install surface of forge-loop (issue #39).

The stable surface MUST be importable with only the default deps. The
experimental surface MUST refuse to import when the [experimental]
extra is absent.

In a dev environment the experimental extras ARE installed, so we
simulate "default install" by monkeypatching the sentinel detector
and clearing the override env var.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from tests.import_isolation import isolated_import

# Modules that must always import with only the default dependencies.
STABLE_MODULES = [
    "forge_loop",
    "forge_loop.cli",
    "forge_loop.runner",
    "forge_loop.worker",
    "forge_loop.critic",
    "forge_loop.attempts",
    "forge_loop.po",
    "forge_loop.maintenance",
    "forge_loop.watchdog",
    "forge_loop.queue",
    "forge_loop.queue.in_memory",
    "forge_loop.queue.sqlite",
]

# Modules that must refuse to import without the [experimental] extra.
EXPERIMENTAL_MODULES = [
    "forge_loop.multirepo",
    "forge_loop.runner_async",
    "forge_loop.dashboard",
    "forge_loop.integrations",
    "forge_loop.observability",
    "forge_loop.replay",
    "forge_loop.pipeline",
]


@pytest.mark.parametrize("mod", STABLE_MODULES)
def test_stable_module_imports_clean(mod: str) -> None:
    """Stable surface imports without any experimental dep present."""
    # isolated_import restores both sys.modules and parent package attrs.
    with isolated_import(mod):
        pass


def test_no_redis_or_cluster_module_exists() -> None:
    """Regression guard: the removed modules must not be re-introduced."""
    with pytest.raises(ImportError):
        importlib.import_module("forge_loop.queue.redis_backend")
    with pytest.raises(ImportError):
        importlib.import_module("forge_loop.cluster")


def test_redis_url_rejected_at_factory() -> None:
    """``build_queue('redis://...')`` raises a clear ValueError — no silent fallback."""
    from forge_loop.queue import build_queue

    with pytest.raises(ValueError, match="Redis backend was removed"):
        build_queue("redis://example:6379/0")


def test_runner_module_has_no_cluster_import() -> None:
    """Adversarial: runner.py must not reference the removed cluster package."""
    import inspect

    from forge_loop import runner

    src = inspect.getsource(runner)
    assert "from forge_loop.cluster" not in src
    assert "ClusterCoordinator" not in src
    assert "redis_backend" not in src


@pytest.mark.parametrize("mod", EXPERIMENTAL_MODULES)
def test_experimental_module_refuses_without_extras(
    monkeypatch: pytest.MonkeyPatch, mod: str
) -> None:
    """Adversarial: simulate a default install — experimental imports must raise."""
    # Pretend none of the [experimental] sentinel deps are installed and
    # clear the dev override. The gate must then refuse the import.
    monkeypatch.delenv("FORGE_LOOP_EXPERIMENTAL", raising=False)
    import forge_loop._extras as _extras

    monkeypatch.setattr(_extras, "experimental_installed", lambda: False)

    # Drop the cached experimental package + any submodules so the gate
    # re-runs on this import attempt. Snapshot/restore so we don't
    # pollute later tests that already imported these modules at their
    # own top-level.
    saved = {
        m: sys.modules[m]
        for m in list(sys.modules)
        if m == mod or m.startswith(mod + ".")
    }
    for m in saved:
        sys.modules.pop(m, None)
    try:
        with pytest.raises(ImportError, match="experimental"):
            importlib.import_module(mod)
    finally:
        # Drop any partially-imported new copy, then restore the
        # originals so other tests keep their object identities.
        for m in list(sys.modules):
            if m == mod or m.startswith(mod + "."):
                sys.modules.pop(m, None)
        sys.modules.update(saved)


def test_experimental_gate_passes_with_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """FORGE_LOOP_EXPERIMENTAL=1 bypasses the gate — covers the dev escape hatch."""
    monkeypatch.setenv("FORGE_LOOP_EXPERIMENTAL", "1")
    from forge_loop._extras import experimental_installed, require_experimental

    assert experimental_installed() is True
    require_experimental("anything")  # must not raise


def test_critic_template_mentions_no_scaffold_theatre() -> None:
    """Spec rule: critic.md.tmpl must teach the no-scaffold-theatre rule."""
    from importlib import resources

    body = (
        resources.files("forge_loop.briefs")
        .joinpath("critic.md.tmpl")
        .read_text(encoding="utf-8")
    )
    assert "product" in body
    assert "scaffold theatre" in body.lower()


def test_critic_accepts_product_category() -> None:
    """CriticReport must accept the new ``product`` category."""
    from forge_loop.critic import VALID_CATEGORY

    assert "product" in VALID_CATEGORY


def test_pyproject_default_deps_are_minimal() -> None:
    """The default dep list must not pull experimental deps."""
    import tomllib
    from pathlib import Path

    # Walk up from this test file to locate pyproject.toml so the test
    # works regardless of cwd.
    here = Path(__file__).resolve()
    for parent in here.parents:
        pp = parent / "pyproject.toml"
        if pp.exists():
            data = tomllib.loads(pp.read_text())
            break
    else:  # pragma: no cover
        pytest.fail("pyproject.toml not found")

    deps = data["project"]["dependencies"]
    forbidden = {"fastapi", "uvicorn", "prometheus_client", "redis", "jinja2"}
    for d in deps:
        head = d.split()[0].lower().replace("-", "_")
        assert head not in forbidden, f"forbidden default dep: {d}"

    extras = data["project"]["optional-dependencies"]
    assert "experimental" in extras
    # Redis must NOT be in the experimental extra either — it's removed.
    assert not any(e.split()[0].lower().startswith("redis") for e in extras["experimental"])
