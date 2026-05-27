"""Tests for forge_loop.observability — Prom + OTel exporters.

Covers:

- Happy path: counter increment shows up in Prometheus exposition.
- Happy path: span start/end has correct duration; tool call → child span.
- Integration: scrape ``/metrics`` over HTTP during a simulated dispatch.
- Adversarial: OTLP collector unreachable → loop continues, warn-once.
- Adversarial: render when Prometheus is disabled returns empty body.
"""

from __future__ import annotations

import logging
import time
import urllib.request
from http.server import HTTPServer
from threading import Thread

import pytest

from forge_loop import observability
from forge_loop.dashboard.app import build_handler
from forge_loop.observability.otel import Tracer, otel_available
from forge_loop.observability.prometheus import Metrics, prometheus_available


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    observability.reset_for_testing()
    yield
    observability.reset_for_testing()


# ---------------------------------------------------------------------------
# Metrics — increment API + Prometheus exposition
# ---------------------------------------------------------------------------


def test_counter_increment_recorded_in_internal_tally() -> None:
    m = Metrics(enabled=False)
    m.inc_workers_dispatched(repo="acme/api", role="worker")
    m.inc_workers_dispatched(repo="acme/api", role="worker", n=2)
    m.inc_prs_merged(repo="acme/api")
    m.inc_prs_failed(repo="acme/api", reason="gate_failed")

    assert m.value("forge_loop_workers_dispatched_total", repo="acme/api", role="worker") == 3
    assert m.value("forge_loop_prs_merged_total", repo="acme/api") == 1
    assert m.value("forge_loop_prs_failed_total", repo="acme/api", reason="gate_failed") == 1


def test_budget_and_critic_counters_label_correctly() -> None:
    m = Metrics(enabled=False)
    m.inc_budget_spent(repo="acme/api", role="po", usd=0.12)
    m.inc_budget_spent(repo="acme/api", role="po", usd=0.05)
    m.inc_critic_findings(repo="acme/api", severity="major")

    assert m.value(
        "forge_loop_budget_spent_usd_total", repo="acme/api", role="po"
    ) == pytest.approx(0.17)
    assert m.value("forge_loop_critic_findings_total", repo="acme/api", severity="major") == 1


def test_workers_active_gauge_is_settable() -> None:
    m = Metrics(enabled=False)
    m.set_workers_active("acme/api", 3)
    assert m.value("forge_loop_workers_active", repo="acme/api") == 3
    m.set_workers_active("acme/api", 0)
    assert m.value("forge_loop_workers_active", repo="acme/api") == 0


@pytest.mark.skipif(not prometheus_available(), reason="prometheus_client not installed")
def test_prometheus_serialization_includes_all_spec_metrics() -> None:
    m = Metrics(enabled=True)
    m.inc_workers_dispatched(repo="acme/api", role="worker")
    m.inc_prs_merged(repo="acme/api")
    m.inc_prs_failed(repo="acme/api", reason="gate_failed")
    m.inc_budget_spent(repo="acme/api", role="po", usd=0.42)
    m.inc_critic_findings(repo="acme/api", severity="minor")
    m.set_workers_active("acme/api", 2)

    body, content_type = m.render()
    assert "text/plain" in content_type
    text = body.decode()
    assert "forge_loop_workers_dispatched_total" in text
    assert "forge_loop_prs_merged_total" in text
    assert "forge_loop_prs_failed_total" in text
    assert "forge_loop_budget_spent_usd_total" in text
    assert "forge_loop_critic_findings_total" in text
    assert "forge_loop_workers_active" in text
    # Labels survive
    assert 'repo="acme/api"' in text
    assert 'reason="gate_failed"' in text


def test_render_when_disabled_returns_empty_body() -> None:
    m = Metrics(enabled=False)
    m.inc_prs_merged(repo="acme/api")  # still tallied internally
    body, content_type = m.render()
    assert body == b""
    assert "text/plain" in content_type


def test_module_level_metrics_singleton_is_shared() -> None:
    a = observability.metrics()
    b = observability.metrics()
    assert a is b


# ---------------------------------------------------------------------------
# Tracer — span lifecycle + adversarial paths
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not otel_available(), reason="opentelemetry not installed")
def test_span_start_end_duration_and_child_span(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # Build a tracer manually wired to an in-memory exporter so we can assert
    # the recorded spans without needing a network collector.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    t = Tracer.__new__(Tracer)
    t._endpoint = "test"
    t._service_name = "test"
    t._warned = False
    t._provider = provider
    t._tracer = provider.get_tracer("test")
    t.enabled = True

    with t.span("worker.dispatch", attrs={"repo": "acme/api"}):
        time.sleep(0.01)
        with t.span("tool.bash"):
            time.sleep(0.005)

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    # Children finish before parents.
    names = [s.name for s in spans]
    assert names == ["tool.bash", "worker.dispatch"]
    tool_span = spans[0]
    dispatch_span = spans[1]
    assert tool_span.parent is not None
    assert tool_span.parent.span_id == dispatch_span.context.span_id
    # Both spans share a trace id.
    assert tool_span.context.trace_id == dispatch_span.context.trace_id
    duration_ns = dispatch_span.end_time - dispatch_span.start_time
    assert duration_ns > 10_000_000  # > 10ms


def test_tracer_noop_when_endpoint_unset() -> None:
    t = Tracer(endpoint=None)
    assert t.enabled is False
    with t.span("worker.dispatch") as span:
        # Noop span tolerates set_attribute calls without crashing.
        span.set_attribute("repo", "acme/api")
    assert t.current_trace_id_hex() is None


def test_tracer_warns_once_on_init_failure(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not otel_available():
        pytest.skip("opentelemetry not installed")

    import forge_loop.observability.otel as otel_mod

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(otel_mod, "OTLPSpanExporter", _boom)
    caplog.set_level(logging.WARNING, logger=otel_mod.__name__)

    t = Tracer(endpoint="http://unreachable.invalid:4318")
    assert t.enabled is False
    # Subsequent spans must still work (no exception, no second warning).
    with t.span("worker.dispatch"):
        pass
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1
    assert "collector unreachable" in warn_records[0].message


def test_tracer_singleton_via_module_facade() -> None:
    a = observability.tracer()
    b = observability.tracer()
    assert a is b


# ---------------------------------------------------------------------------
# Integration — /metrics endpoint over HTTP
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not prometheus_available(), reason="prometheus_client not installed")
def test_metrics_endpoint_serves_counters_after_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_PROM_ENABLED", "1")
    observability.reset_for_testing()

    # Simulate a dispatched tick.
    m = observability.metrics()
    m.inc_workers_dispatched(repo="acme/api", role="worker")
    m.inc_prs_merged(repo="acme/api")

    handler_cls = build_handler()
    httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
            assert resp.status == 200
            body = resp.read().decode()
            assert "forge_loop_workers_dispatched_total" in body
            assert "forge_loop_prs_merged_total" in body
            # Counter value is non-zero.
            assert 'forge_loop_workers_dispatched_total{repo="acme/api",role="worker"} 1.0' in body

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            assert resp.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_metrics_endpoint_404_for_unknown_path() -> None:
    handler_cls = build_handler()
    httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2)
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:
            pytest.fail("expected 404")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
