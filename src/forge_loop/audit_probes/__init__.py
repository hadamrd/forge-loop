"""Codebase-audit probes (issue #156).

Each probe is a small module exposing one class that satisfies the
:class:`forge_loop.codebase_audit.Probe` Protocol. Keep probes:

* Pure (filesystem in, ``Violation`` objects out).
* Independently importable — the framework can disable a broken probe
  without bringing the rest down.
* Named with a stable identifier (the probe label uses it).
"""

from forge_loop.audit_probes.file_size import FileSizeProbe
from forge_loop.audit_probes.function_size import FunctionSizeProbe

__all__ = ["FileSizeProbe", "FunctionSizeProbe"]
