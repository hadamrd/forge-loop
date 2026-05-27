"""Optional observability layer: Prometheus metrics + OpenTelemetry traces.

The hard dependencies are deliberately optional. Without ``prometheus_client``
or ``opentelemetry-*`` installed, every public API here becomes a silent
no-op. The loop continues to run; operators just don't get telemetry.

Public surface:

- ``metrics()`` — singleton ``Metrics`` facade with counter/gauge increments.
- ``tracer()`` — singleton ``Tracer`` facade with ``span(name, attrs=...)``.
- ``render_prometheus()`` — bytes of the Prometheus exposition for ``/metrics``.

Config knobs (read from env):

- ``LOOP_PROM_ENABLED`` — ``1`` / ``true`` enables the Prometheus registry.
- ``LOOP_OTEL_ENDPOINT`` — OTLP collector URL; absent ⇒ tracer is a no-op.

The runner / worker / critic call into these facades unconditionally; if
the optional deps are missing or config is unset, calls turn into cheap
no-ops so production code stays clean.
"""


from __future__ import annotations


# Experimental gate (issue #39): refuse to import unless the [experimental]
# extra is installed. Stable surface only in the default install.
from forge_loop._extras import require_experimental as _require_experimental
_require_experimental('observability')
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .otel import Tracer
    from .prometheus import Metrics


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


_metrics: Metrics | None = None
_tracer: Tracer | None = None


def metrics() -> Metrics:
    """Return the process-wide metrics facade (Prometheus-backed if enabled)."""
    global _metrics
    if _metrics is None:
        from .prometheus import Metrics

        enabled = _env_truthy(os.environ.get("LOOP_PROM_ENABLED"))
        _metrics = Metrics(enabled=enabled)
    return _metrics


def tracer() -> Tracer:
    """Return the process-wide tracer facade (OTel-backed if configured)."""
    global _tracer
    if _tracer is None:
        from .otel import Tracer

        endpoint = os.environ.get("LOOP_OTEL_ENDPOINT")
        _tracer = Tracer(endpoint=endpoint)
    return _tracer


def render_prometheus() -> tuple[bytes, str]:
    """Render the current metrics in Prometheus exposition format.

    Returns ``(body, content_type)``. If Prometheus is disabled / missing,
    returns an empty body and ``text/plain`` so the HTTP handler can still
    answer 200 OK.
    """
    return metrics().render()


def reset_for_testing() -> None:
    """Wipe singletons. Tests only — do not call from production code."""
    global _metrics, _tracer
    _metrics = None
    _tracer = None


__all__ = ["metrics", "tracer", "render_prometheus", "reset_for_testing"]
