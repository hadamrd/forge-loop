"""Pytest configuration for the forge-loop test suite.

Issue #39 introduced an extras gate on experimental modules (dashboard,
multirepo, runner_async, integrations, observability, replay, pipeline).
The dev install has every experimental dep available, but the gate
also honours an explicit override env var. We set that override here so
existing experimental-module tests can keep importing without each test
file having to opt in.

`test_install_surface.py` explicitly clears this env var (via
monkeypatch) when it needs to simulate a default-only install.
"""

from __future__ import annotations

import os


def pytest_configure(config: object) -> None:  # noqa: ARG001 - pytest hook signature
    os.environ.setdefault("FORGE_LOOP_EXPERIMENTAL", "1")
