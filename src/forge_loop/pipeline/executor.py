"""Chain executor — walks a :class:`DAG` and invokes role handlers.

The executor is deliberately decoupled from the legacy runner so it can
be unit-tested without git / gh / Anthropic. The runner's integration
seam is :func:`PipelineExecutor.run`, which takes a callable per role
and emits ``step_started`` / ``step_finished`` / ``step_skipped`` /
``step_failed`` events through a user-supplied sink.

Status semantics:
  - ``ok``       — handler returned normally (outcome.ok == True)
  - ``failed``   — handler raised, timed out, or returned ok=False
  - ``skipped``  — condition evaluated False
  - ``partial``  — at least one upstream step failed/timed out, but
                   downstream steps that did not strictly depend on
                   the failed branch still ran. Final end-state of the
                   whole run is ``partial`` (not ``crashed``) so the
                   adversarial test in the issue passes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass, field
from typing import Any

from forge_loop.pipeline.dag import DAG
from forge_loop.pipeline.loader import ChainStep, Condition

# A role handler is invoked with a context dict and must return a
# StepOutcome. It MAY raise — the executor catches and converts to a
# failed outcome (so a buggy handler does not crash the run).
RoleHandler = Callable[["StepContext"], "StepOutcome"]


@dataclass
class StepContext:
    """Per-invocation payload passed to a RoleHandler."""

    step: ChainStep
    issue: dict[str, Any]
    upstream_outcomes: dict[str, StepOutcome]
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepOutcome:
    role: str
    status: str  # ok | failed | skipped | partial
    detail: str = ""
    payload: Any = None
    duration_s: float = 0.0


@dataclass
class ExecutionResult:
    outcomes: dict[str, StepOutcome]
    order: tuple[str, ...]
    end_state: str  # ok | partial | failed

    def by_status(self, status: str) -> list[str]:
        return [r for r, o in self.outcomes.items() if o.status == status]


EventSink = Callable[[dict[str, Any]], None]


def _evaluate_condition(
    cond: Condition,
    *,
    issue_labels: Iterable[str],
    upstream: dict[str, StepOutcome],
    all_prior_outcomes: dict[str, StepOutcome] | None = None,
) -> bool:
    if cond.is_empty:
        return True
    if cond.labels:
        labelset = set(issue_labels)
        if not labelset.intersection(cond.labels):
            return False
    if cond.all_approve:
        # all_approve is transitive: every step that ACTUALLY RAN before
        # this one must be ok. Skipped steps are non-blocking (e.g.
        # security-reviewer skipped due to label miss should not block
        # merge), but a failed/timed-out upstream anywhere in the
        # ancestor chain MUST block merge — that is the adversarial
        # contract from the issue body.
        pool = all_prior_outcomes if all_prior_outcomes is not None else upstream
        ran = [o for o in pool.values() if o.status != "skipped"]
        if not ran:
            return False
        if not all(o.status == "ok" for o in ran):
            return False
    return True


class PipelineExecutor:
    """Walks a DAG and dispatches handlers per role.

    Parallelism: each step's ``parallel`` value is interpreted as the
    fan-out for that role. The default :meth:`run` runs ONE invocation
    per step (matching the loop's per-issue model); callers wanting
    fan-out (e.g. multiple worker shards) override :meth:`run_fanout`.

    Timeouts: handlers can take a long time. The executor accepts a
    per-step timeout (seconds); on timeout the step becomes ``failed``
    and downstream steps still evaluate (their conditions may skip
    them).
    """

    def __init__(
        self,
        dag: DAG,
        handlers: dict[str, RoleHandler],
        *,
        events: EventSink | None = None,
        step_timeout_s: float | None = None,
    ) -> None:
        self.dag = dag
        self.handlers = handlers
        self.events = events or (lambda _e: None)
        self.step_timeout_s = step_timeout_s

    # ------------------------------------------------------------------ public

    def run(
        self,
        issue: dict[str, Any],
        *,
        extras: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Execute the chain for one issue. Returns the per-step outcomes."""
        outcomes: dict[str, StepOutcome] = {}
        for role in self.dag.order:
            node = self.dag.nodes[role]
            step = node.step
            upstream = {p: outcomes[p] for p in node.parents if p in outcomes}

            labels = issue.get("labels") or []
            # Normalize gh issue label shape: list[str] OR list[{"name": str}]
            label_names = [
                lbl["name"] if isinstance(lbl, dict) else lbl for lbl in labels
            ]

            if not _evaluate_condition(
                step.condition,
                issue_labels=label_names,
                upstream=upstream,
                all_prior_outcomes=outcomes,
            ):
                outcome = StepOutcome(role=role, status="skipped", detail="condition unmet")
                outcomes[role] = outcome
                self.events({
                    "kind": "step_skipped",
                    "role": role,
                    "issue": issue.get("number"),
                    "reason": "condition_unmet",
                })
                continue

            handler = self.handlers.get(role)
            if handler is None:
                outcome = StepOutcome(role=role, status="failed", detail="no handler registered")
                outcomes[role] = outcome
                self.events({
                    "kind": "step_failed",
                    "role": role,
                    "issue": issue.get("number"),
                    "reason": "no_handler",
                })
                continue

            self.events({
                "kind": "step_started",
                "role": role,
                "issue": issue.get("number"),
                "parallel": step.parallel,
            })

            ctx = StepContext(
                step=step, issue=issue, upstream_outcomes=upstream, extras=extras or {}
            )
            outcome = self._invoke(handler, ctx)
            outcomes[role] = outcome
            self.events({
                "kind": "step_finished" if outcome.status == "ok" else "step_failed",
                "role": role,
                "issue": issue.get("number"),
                "status": outcome.status,
                "detail": outcome.detail,
                "duration_s": outcome.duration_s,
            })

        end_state = self._end_state(outcomes)
        return ExecutionResult(
            outcomes=outcomes, order=self.dag.order, end_state=end_state
        )

    # --------------------------------------------------------------- internals

    def _invoke(self, handler: RoleHandler, ctx: StepContext) -> StepOutcome:
        start = time.monotonic()

        def _wrap() -> StepOutcome:
            try:
                got = handler(ctx)
            except Exception as e:  # noqa: BLE001 — boundary
                return StepOutcome(
                    role=ctx.step.role, status="failed", detail=f"{type(e).__name__}: {e}"
                )
            if not isinstance(got, StepOutcome):
                return StepOutcome(
                    role=ctx.step.role,
                    status="failed",
                    detail=f"handler returned {type(got).__name__}, expected StepOutcome",
                )
            return got

        if self.step_timeout_s is None:
            out = _wrap()
            out.duration_s = time.monotonic() - start
            return out

        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_wrap)
            try:
                out = fut.result(timeout=self.step_timeout_s)
            except FutTimeout:
                out = StepOutcome(
                    role=ctx.step.role,
                    status="failed",
                    detail=f"timeout after {self.step_timeout_s}s",
                )
        out.duration_s = time.monotonic() - start
        return out

    @staticmethod
    def _end_state(outcomes: dict[str, StepOutcome]) -> str:
        statuses = {o.status for o in outcomes.values()}
        if "failed" in statuses and "ok" in statuses:
            return "partial"
        if "failed" in statuses:
            return "failed"
        return "ok"
