"""Extras gate — central place to declare 'this module is experimental'.

The stable surface of forge-loop is documented in the README's stability
matrix. Anything outside that surface lives behind an extras gate: the
default install of forge-loop deliberately does NOT pull the third-party
dependencies the experimental modules need (fastapi, prometheus_client,
opentelemetry, jinja2, …), and the modules themselves refuse to import
unless those deps are present.

This file owns the detection logic so each experimental module can be
one-line-gated:

    from forge_loop._extras import require_experimental
    require_experimental("dashboard")

If a sentinel dep from the ``[experimental]`` extra is missing, we raise
``ImportError`` with a single clear remediation step: install the extra.
The override env var ``FORGE_LOOP_EXPERIMENTAL=1`` lets us run tests of
experimental modules without forcing the extras on every dev box; the
production gate stays loud.
"""

from __future__ import annotations

import importlib

# Sentinel dependencies that ship inside the [experimental] extra. If
# ANY one of them is importable, we treat the extra as installed (we do
# not require all of them — operators sometimes carve out a subset, and
# the message remains actionable either way).
_EXPERIMENTAL_SENTINELS = (
    "prometheus_client",
    "fastapi",
    "uvicorn",
    "opentelemetry",
    "jinja2",
    "redis",
)


def experimental_installed() -> bool:
    """Return True if any sentinel dep from [experimental] is importable."""

    # Settings-driven (issue #84): was env FORGE_LOOP_EXPERIMENTAL,
    # now misc.experimental_enabled. Kept the legacy env-name as the
    # only knob — operators on subscription plans want the explicit
    # opt-in toggle.
    try:
        from forge_loop.settings import Settings as _Settings
        if _Settings.load().misc.experimental_enabled:
            return True
    except Exception:  # noqa: BLE001 — bootstrap helpers must not crash
        pass
    for name in _EXPERIMENTAL_SENTINELS:
        try:
            importlib.import_module(name)
            return True
        except ImportError:
            continue
    return False


def require_experimental(feature: str) -> None:
    """Refuse to import an experimental module unless the extra is present.

    Raises ``ImportError`` (the canonical signal for "this module is
    unavailable in your environment") with a single remediation line.
    """

    if experimental_installed():
        return
    raise ImportError(
        f"forge-loop feature {feature!r} is experimental and is not "
        f"available in the default install. Install with: "
        f"pip install 'forge-loop[experimental]'  "
        f"(or export FORGE_LOOP_EXPERIMENTAL=1 to bypass for local dev)."
    )
