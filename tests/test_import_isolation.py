from __future__ import annotations

import sys

from tests.import_isolation import isolated_import


def test_isolated_import_restores_parent_child_attribute() -> None:
    import forge_loop
    import forge_loop.worker as original_worker

    with isolated_import("forge_loop.worker") as temporary_worker:
        assert temporary_worker is not original_worker
        assert forge_loop.worker is temporary_worker

    assert sys.modules["forge_loop.worker"] is original_worker
    assert forge_loop.worker is original_worker
