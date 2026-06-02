"""Durable frontier proposal decision ledger."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import yaml


class ProposalKind(StrEnum):
    EPIC = "epic"
    TICKET = "ticket"


class FrontierDecisionOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class FrontierDecision:
    proposal_title: str
    proposal_kind: ProposalKind
    axis: str
    outcome: FrontierDecisionOutcome
    rationale: str
    source_key: str
    issue_number: int | None = None
    duplicate_of_title: str | None = None
    duplicate_of_issue: int | None = None
    source_report_path: str | None = None
    source_report_hash: str | None = None
    decided_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def candidate_key(self) -> tuple[str, str]:
        return normalize_candidate_key(self.proposal_title, self.axis)


class DecisionLedger(Protocol):
    def record(self, decision: FrontierDecision) -> FrontierDecision: ...

    def list(self) -> tuple[FrontierDecision, ...]: ...

    def find_by_candidate(self, *, title: str, axis: str) -> FrontierDecision | None: ...


class FrontierDecisionLedger:
    """YAML-backed frontier proposal decision ledger."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def record(self, decision: FrontierDecision) -> FrontierDecision:
        decisions = list(self.list())
        existing = next(
            (item for item in decisions if item.source_key == decision.source_key), None
        )
        if existing is not None:
            return existing
        decisions.append(decision)
        self._save(decisions)
        return decision

    def list(self) -> tuple[FrontierDecision, ...]:
        if not self.path.exists():
            return ()
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if raw is None:
            return ()
        if not isinstance(raw, dict):
            raise ValueError("frontier decision ledger must be a mapping")
        records = raw.get("decisions", [])
        if not isinstance(records, list):
            raise ValueError("frontier decision ledger field decisions must be a list")
        return tuple(_decision_from_yaml(record) for record in records)

    def accepted(self) -> tuple[FrontierDecision, ...]:
        return self.by_outcome(FrontierDecisionOutcome.ACCEPTED)

    def rejected(self) -> tuple[FrontierDecision, ...]:
        return self.by_outcome(FrontierDecisionOutcome.REJECTED)

    def deferred(self) -> tuple[FrontierDecision, ...]:
        return self.by_outcome(FrontierDecisionOutcome.DEFERRED)

    def by_outcome(self, outcome: FrontierDecisionOutcome) -> tuple[FrontierDecision, ...]:
        return tuple(decision for decision in self.list() if decision.outcome is outcome)

    def find_by_candidate(self, *, title: str, axis: str) -> FrontierDecision | None:
        key = normalize_candidate_key(title, axis)
        return next((decision for decision in self.list() if decision.candidate_key == key), None)

    def _save(self, decisions: Sequence[FrontierDecision]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "decisions": [_decision_to_yaml(decision) for decision in decisions],
        }
        self.path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def normalize_candidate_key(title: str, axis: str) -> tuple[str, str]:
    return (_normalize_text(title), _normalize_text(axis))


def format_prior_decisions(ledger: DecisionLedger, *, limit: int = 12) -> str:
    decisions = ledger.list()
    if not decisions:
        return "Prior frontier decisions: none"

    lines = ["Prior frontier decisions:"]
    for decision in decisions[-limit:]:
        issue = f" (#{decision.issue_number})" if decision.issue_number is not None else ""
        duplicate = (
            f" duplicate of #{decision.duplicate_of_issue}"
            if decision.duplicate_of_issue is not None
            else ""
        )
        lines.append(
            f"- {decision.outcome.value}: [{decision.axis}] "
            f"{decision.proposal_title}{issue}{duplicate} — {decision.rationale}"
        )
    return "\n".join(lines)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _decision_to_yaml(decision: FrontierDecision) -> dict[str, object]:
    data = asdict(decision)
    data["proposal_kind"] = decision.proposal_kind.value
    data["outcome"] = decision.outcome.value
    return {key: value for key, value in data.items() if value is not None}


def _decision_from_yaml(value: object) -> FrontierDecision:
    if not isinstance(value, dict):
        raise ValueError("frontier decision records must be mappings")
    try:
        return FrontierDecision(
            proposal_title=_required_str(value, "proposal_title"),
            proposal_kind=ProposalKind(_required_str(value, "proposal_kind")),
            axis=_required_str(value, "axis"),
            outcome=FrontierDecisionOutcome(_required_str(value, "outcome")),
            rationale=_required_str(value, "rationale"),
            source_key=_required_str(value, "source_key"),
            issue_number=_optional_int(value, "issue_number"),
            duplicate_of_title=_optional_str(value, "duplicate_of_title"),
            duplicate_of_issue=_optional_int(value, "duplicate_of_issue"),
            source_report_path=_optional_str(value, "source_report_path"),
            source_report_hash=_optional_str(value, "source_report_hash"),
            decided_at=_required_str(value, "decided_at"),
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid frontier decision record: {value!r}") from exc


def _required_str(value: dict[object, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"frontier decision missing required field: {field_name}")
    return raw


def _optional_str(value: dict[object, object], field_name: str) -> str | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"frontier decision field {field_name} must be a string")
    return raw


def _optional_int(value: dict[object, object], field_name: str) -> int | None:
    raw = value.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, int):
        raise ValueError(f"frontier decision field {field_name} must be an integer")
    return raw
