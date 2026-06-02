from __future__ import annotations

from pathlib import Path

from forge_loop._testing.frontier_decisions import FakeFrontierDecisionLedger
from forge_loop.frontier.decisions import (
    FrontierDecision,
    FrontierDecisionLedger,
    FrontierDecisionOutcome,
    ProposalKind,
    format_prior_decisions,
    normalize_candidate_key,
)


def _decision(
    *,
    title: str,
    axis: str = "frontier-generation",
    outcome: FrontierDecisionOutcome = FrontierDecisionOutcome.ACCEPTED,
    source_key: str,
) -> FrontierDecision:
    return FrontierDecision(
        proposal_title=title,
        proposal_kind=ProposalKind.TICKET,
        axis=axis,
        outcome=outcome,
        rationale="reviewed by product maestro",
        source_key=source_key,
        issue_number=170 if outcome is FrontierDecisionOutcome.ACCEPTED else None,
    )


def test_frontier_decision_ledger_round_trips_all_outcomes(tmp_path: Path) -> None:
    path = tmp_path / ".forge" / "frontier-decisions.yaml"
    ledger = FrontierDecisionLedger(path)

    accepted = ledger.record(_decision(title="Record accepted proposal", source_key="report:a"))
    rejected = ledger.record(
        _decision(
            title="Drop duplicate proposal",
            outcome=FrontierDecisionOutcome.REJECTED,
            source_key="report:r",
        )
    )
    deferred = ledger.record(
        _decision(
            title="Wait for more signal",
            outcome=FrontierDecisionOutcome.DEFERRED,
            source_key="report:d",
        )
    )

    reopened = FrontierDecisionLedger(path)

    assert reopened.list() == (accepted, rejected, deferred)
    assert reopened.accepted() == (accepted,)
    assert reopened.rejected() == (rejected,)
    assert reopened.deferred() == (deferred,)


def test_frontier_decision_ledger_preserves_first_record_for_source_key(
    tmp_path: Path,
) -> None:
    ledger = FrontierDecisionLedger(tmp_path / "frontier-decisions.yaml")
    first = _decision(title="Keep original decision", source_key="reviewed:0")
    replacement = _decision(
        title="Keep original decision",
        outcome=FrontierDecisionOutcome.REJECTED,
        source_key="reviewed:0",
    )

    recorded = ledger.record(first)
    repeated = ledger.record(replacement)

    assert repeated == recorded
    assert ledger.list() == (first,)


def test_frontier_decision_ledger_matches_repeated_candidate_by_title_and_axis(
    tmp_path: Path,
) -> None:
    ledger = FrontierDecisionLedger(tmp_path / "frontier-decisions.yaml")
    decision = ledger.record(
        _decision(title="  File   Deterministic Report  ", axis="Billing", source_key="report:1")
    )

    match = ledger.find_by_candidate(title="file deterministic report", axis="billing")

    assert match == decision
    assert normalize_candidate_key("  File   Deterministic Report  ", "Billing") == (
        "file deterministic report",
        "billing",
    )


def test_format_prior_decisions_is_concise_and_separates_outcomes(tmp_path: Path) -> None:
    ledger = FrontierDecisionLedger(tmp_path / "frontier-decisions.yaml")
    ledger.record(_decision(title="Accepted proposal", source_key="a"))
    ledger.record(
        _decision(
            title="Rejected duplicate",
            outcome=FrontierDecisionOutcome.REJECTED,
            source_key="r",
        )
    )
    ledger.record(
        _decision(
            title="Deferred idea",
            outcome=FrontierDecisionOutcome.DEFERRED,
            source_key="d",
        )
    )

    summary = format_prior_decisions(ledger)

    assert "accepted: [frontier-generation] Accepted proposal (#170)" in summary
    assert "rejected: [frontier-generation] Rejected duplicate" in summary
    assert "deferred: [frontier-generation] Deferred idea" in summary


def test_fake_frontier_decision_ledger_matches_real_shape(tmp_path: Path) -> None:
    real = FrontierDecisionLedger(tmp_path / "frontier-decisions.yaml")
    fake = FakeFrontierDecisionLedger()
    decision = _decision(title="Contract shape", source_key="contract:1")

    assert real.record(decision) == fake.record(decision)
    assert real.record(decision) == fake.record(decision)
    assert real.list() == fake.list()
    assert real.accepted() == fake.accepted()
    assert real.find_by_candidate(title=" contract   shape ", axis="frontier-generation") == (
        fake.find_by_candidate(title=" contract   shape ", axis="frontier-generation")
    )
