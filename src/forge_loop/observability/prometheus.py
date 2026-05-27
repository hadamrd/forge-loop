"""Prometheus exporter — optional. Cheap no-op when disabled / missing.

Counters and gauges defined here mirror the spec in issue #23:

- ``forge_loop_workers_active{repo}``
- ``forge_loop_workers_dispatched_total{repo, role}``
- ``forge_loop_prs_merged_total{repo}``
- ``forge_loop_prs_failed_total{repo, reason}``
- ``forge_loop_budget_spent_usd_total{repo, role}``
- ``forge_loop_critic_findings_total{repo, severity}``

A ``Metrics`` instance always exposes the same method surface regardless of
whether ``prometheus_client`` is installed; the disabled path keeps an
in-memory tally so unit tests can verify the increment API without the
optional dep.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Any

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when extra is uninstalled
    _PROM_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


class Metrics:
    """Facade over a Prometheus registry; degrades to in-memory tally."""

    def __init__(self, enabled: bool = False) -> None:
        self._lock = Lock()
        self._tally: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(dict)
        self.enabled = enabled and _PROM_AVAILABLE

        if self.enabled:
            self._registry = CollectorRegistry()
            self._workers_active = Gauge(
                "forge_loop_workers_active",
                "Workers currently dispatched per repo",
                ["repo"],
                registry=self._registry,
            )
            self._workers_dispatched = Counter(
                "forge_loop_workers_dispatched_total",
                "Total workers dispatched",
                ["repo", "role"],
                registry=self._registry,
            )
            self._prs_merged = Counter(
                "forge_loop_prs_merged_total",
                "PRs successfully merged",
                ["repo"],
                registry=self._registry,
            )
            self._prs_failed = Counter(
                "forge_loop_prs_failed_total",
                "PRs that failed (closed / blocked / errored)",
                ["repo", "reason"],
                registry=self._registry,
            )
            self._budget_spent = Counter(
                "forge_loop_budget_spent_usd_total",
                "USD spent against the loop budget",
                ["repo", "role"],
                registry=self._registry,
            )
            self._critic_findings = Counter(
                "forge_loop_critic_findings_total",
                "Critic findings emitted",
                ["repo", "severity"],
                registry=self._registry,
            )

    # ------------------------------------------------------------------
    # Public increment API — these are called from runner / worker / critic.
    # Each method is a no-op-safe single dispatch site so calling code does
    # not need to branch on whether metrics are enabled.
    # ------------------------------------------------------------------

    def inc_workers_dispatched(self, repo: str, role: str, n: float = 1.0) -> None:
        self._inc("forge_loop_workers_dispatched_total", {"repo": repo, "role": role}, n)
        if self.enabled:
            self._workers_dispatched.labels(repo=repo, role=role).inc(n)

    def set_workers_active(self, repo: str, value: float) -> None:
        self._set("forge_loop_workers_active", {"repo": repo}, value)
        if self.enabled:
            self._workers_active.labels(repo=repo).set(value)

    def inc_prs_merged(self, repo: str, n: float = 1.0) -> None:
        self._inc("forge_loop_prs_merged_total", {"repo": repo}, n)
        if self.enabled:
            self._prs_merged.labels(repo=repo).inc(n)

    def inc_prs_failed(self, repo: str, reason: str, n: float = 1.0) -> None:
        self._inc("forge_loop_prs_failed_total", {"repo": repo, "reason": reason}, n)
        if self.enabled:
            self._prs_failed.labels(repo=repo, reason=reason).inc(n)

    def inc_budget_spent(self, repo: str, role: str, usd: float) -> None:
        self._inc("forge_loop_budget_spent_usd_total", {"repo": repo, "role": role}, usd)
        if self.enabled:
            self._budget_spent.labels(repo=repo, role=role).inc(usd)

    def inc_critic_findings(self, repo: str, severity: str, n: float = 1.0) -> None:
        self._inc("forge_loop_critic_findings_total", {"repo": repo, "severity": severity}, n)
        if self.enabled:
            self._critic_findings.labels(repo=repo, severity=severity).inc(n)

    # ------------------------------------------------------------------
    # Read-side
    # ------------------------------------------------------------------

    def value(self, name: str, **labels: str) -> float:
        """Return the current tally for a metric+label set. Test helper."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            return self._tally.get(name, {}).get(key, 0.0)

    def render(self) -> tuple[bytes, str]:
        """Render Prometheus exposition. Empty body when disabled."""
        if not self.enabled:
            return b"", CONTENT_TYPE_LATEST
        return generate_latest(self._registry), CONTENT_TYPE_LATEST

    # ------------------------------------------------------------------
    # Internal in-memory tally — drives unit tests + works when prom is off.
    # ------------------------------------------------------------------

    def _inc(self, name: str, labels: dict[str, str], n: float) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._tally[name][key] = self._tally[name].get(key, 0.0) + n

    def _set(self, name: str, labels: dict[str, str], value: float) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._tally[name][key] = value


def prometheus_available() -> bool:
    return _PROM_AVAILABLE


__all__: list[Any] = ["Metrics", "prometheus_available", "CONTENT_TYPE_LATEST"]
