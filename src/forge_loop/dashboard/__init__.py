"""Dashboard server — exposes ``/metrics`` (Prometheus) and ``/healthz``.

Stdlib-only HTTP server so the loop core has no new hard deps. The
``forge-loop dashboard`` CLI surface starts ``app.serve``.
"""

from .app import build_handler, serve

__all__ = ["build_handler", "serve"]
