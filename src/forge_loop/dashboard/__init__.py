"""Dashboard server — exposes ``/metrics`` (Prometheus) and ``/healthz``.

Stdlib-only HTTP server so the loop core has no new hard deps. The
``forge-loop dashboard`` CLI surface starts ``app.serve``.
"""

# Experimental gate (issue #39): refuse to import unless the [experimental]
# extra is installed. Stable surface only in the default install.
from forge_loop._extras import require_experimental as _require_experimental

_require_experimental("dashboard")
from .app import build_handler, serve

__all__ = ["build_handler", "serve"]
