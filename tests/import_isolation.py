"""Helpers for tests that temporarily evict and re-import modules.

Python sets child modules as attributes on their parent package during import.
If a test restores only ``sys.modules`` after a temporary re-import, later
``monkeypatch.setattr("pkg.child.name", ...)`` can patch a different module
object than already-imported functions use through their globals.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType


def _parent_attr_state(module_name: str) -> tuple[ModuleType | None, str | None, object | None]:
    parent_name, _, attr = module_name.rpartition(".")
    if not parent_name:
        return None, None, None
    parent = sys.modules.get(parent_name)
    if not isinstance(parent, ModuleType):
        return None, None, None
    return parent, attr, getattr(parent, attr, None)


@contextmanager
def isolated_import(module_name: str) -> Iterator[ModuleType]:
    """Temporarily re-import ``module_name`` and restore import identity after.

    Restores both the ``sys.modules`` entry and the parent package's child
    attribute. Tests should use this instead of open-coded ``sys.modules.pop``.
    """
    saved_module = sys.modules.pop(module_name, None)
    parent, attr, saved_attr = _parent_attr_state(module_name)
    try:
        yield importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)
        if saved_module is not None:
            sys.modules[module_name] = saved_module
        if parent is not None and attr is not None:
            if saved_attr is None:
                if hasattr(parent, attr):
                    delattr(parent, attr)
            else:
                setattr(parent, attr, saved_attr)
