"""Minimal stdlib HTTP server exposing ``/metrics`` for Prometheus scrapes.

Kept dependency-free on purpose: the loop's hot path must not pull in
Flask / Starlette just to answer one scrape endpoint. The handler is
also exported for tests so they don't need to bind a port.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from ..observability import render_prometheus


class _MetricsHandler(BaseHTTPRequestHandler):
    """Routes ``GET /metrics`` and ``GET /healthz``."""

    server_version = "ForgeLoopDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path.rstrip("/") == "/metrics":
            body, content_type = render_prometheus()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.rstrip("/") in {"/healthz", ""}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence stderr access logs; the loop has its own event log.
        return


def build_handler() -> type[BaseHTTPRequestHandler]:
    """Return the request handler class. Public so tests can drive it directly."""
    return _MetricsHandler


def serve(host: str = "0.0.0.0", port: int = 9464) -> None:  # noqa: S104
    """Run the dashboard server in the foreground. Used by the CLI."""
    httpd = HTTPServer((host, port), _MetricsHandler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


__all__ = ["build_handler", "serve"]
