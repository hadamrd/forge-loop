"""Test fakes for frontier decision storage."""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_loop.frontier.decisions import (
    FrontierDecision,
    FrontierDecisionOutcome,
    normalize_candidate_key,
)


@dataclass
class FakeFrontierDecisionLedger:
    decisions: list[FrontierDecision] = field(default_factory=list)

    def record(self, decision: FrontierDecision) -> FrontierDecision:
        existing = next(
            (item for item in self.decisions if item.source_key == decision.source_key),
            None,
        )
        if existing is not None:
            return existing
        self.decisions.append(decision)
        return decision

    def list(self) -> tuple[FrontierDecision, ...]:
        return tuple(self.decisions)

    def accepted(self) -> tuple[FrontierDecision, ...]:
        return self.by_outcome(FrontierDecisionOutcome.ACCEPTED)

    def rejected(self) -> tuple[FrontierDecision, ...]:
        return self.by_outcome(FrontierDecisionOutcome.REJECTED)

    def deferred(self) -> tuple[FrontierDecision, ...]:
        return self.by_outcome(FrontierDecisionOutcome.DEFERRED)

    def by_outcome(self, outcome: FrontierDecisionOutcome) -> tuple[FrontierDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.outcome is outcome)

    def find_by_candidate(self, *, title: str, axis: str) -> FrontierDecision | None:
        key = normalize_candidate_key(title, axis)
        return next(
            (decision for decision in self.decisions if decision.candidate_key == key), None
        )
