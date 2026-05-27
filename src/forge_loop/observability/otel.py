"""OpenTelemetry tracer facade — optional, warn-once on collector failure.

Each worker dispatch starts a span; tool calls become child spans of the
current span via ``tracer().span(name, attrs=...)``. The span's
``trace_id`` is exposed as ``current_trace_id_hex()`` so it can be threaded
into ``events.jsonl`` rows for cross-correlation between metrics, traces,
and the loop's own JSONL log.

Failure modes are explicitly graceful:

- ``opentelemetry-*`` not installed → ``Tracer`` is a no-op.
- ``LOOP_OTEL_ENDPOINT`` unset → ``Tracer`` is a no-op.
- OTLP exporter raises (collector unreachable) → log a single warning and
  flip into no-op mode. Subsequent dispatches do NOT retry / re-warn.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when extra is uninstalled
    _OTEL_AVAILABLE = False


class _NoopSpan:
    """Stand-in span when OTel is disabled / failed. Records nothing."""

    def set_attribute(self, _key: str, _value: Any) -> None:
        return

    def get_span_context(self) -> Any:  # pragma: no cover - parity helper
        return None


class Tracer:
    """Optional OpenTelemetry tracer; degrades to no-op on failure."""

    def __init__(self, endpoint: str | None = None, service_name: str = "forge-loop") -> None:
        self._endpoint = endpoint
        self._service_name = service_name
        self._warned = False
        self._tracer: Any = None
        self.enabled = bool(endpoint) and _OTEL_AVAILABLE

        if self.enabled:
            try:
                resource = Resource.create({"service.name": service_name})
                provider = TracerProvider(resource=resource)
                exporter = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                # Use a private provider so we don't stomp on globals if the
                # host app already set one up.
                self._provider = provider
                self._tracer = provider.get_tracer("forge_loop")
            except Exception as exc:
                self._warn_once(f"OTel init failed: {exc}")
                self.enabled = False

    @contextmanager
    def span(self, name: str, attrs: dict[str, Any] | None = None) -> Any:
        """Start a span. Child of the current active span automatically."""
        if not self.enabled or self._tracer is None:
            yield _NoopSpan()
            return
        try:
            with self._tracer.start_as_current_span(name) as span:
                if attrs:
                    for k, v in attrs.items():
                        span.set_attribute(k, v)
                try:
                    yield span
                except Exception as exc:  # propagate but mark span
                    if hasattr(span, "record_exception"):
                        span.record_exception(exc)
                    raise
        except Exception as exc:
            # Exporter or batch processor blew up — degrade to no-op.
            self._warn_once(f"OTel span emission failed: {exc}")
            self.enabled = False
            yield _NoopSpan()

    def current_trace_id_hex(self) -> str | None:
        """Hex trace id of the current span, or None if no active span / OTel off."""
        if not self.enabled or not _OTEL_AVAILABLE:
            return None
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx is None or not getattr(ctx, "is_valid", False):
            return None
        return format(ctx.trace_id, "032x")

    def shutdown(self) -> None:
        if self.enabled and hasattr(self, "_provider"):
            try:
                self._provider.shutdown()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("OTel shutdown failed: %s", exc)

    def _warn_once(self, msg: str) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning("%s — disabling OTel for the rest of this process", msg)


def otel_available() -> bool:
    return _OTEL_AVAILABLE


__all__ = ["Tracer", "otel_available"]
