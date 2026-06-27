"""Typed events for the skill-tree lifecycle."""

from __future__ import annotations

import pytest

from forge_loop.events import (
    EVENT_REGISTRY,
    SkillExpiredEvent,
    SkillHarvestedEvent,
    SkillInjectedEvent,
    SkillPromotedEvent,
)


def test_skill_harvested_registered_and_roundtrips() -> None:
    ev = SkillHarvestedEvent(
        issue=430,
        area="pulsar-node/http-route",
        skill_key="k",
        memory_id="m",
        sha="s",
        confidence=0.9,
    )
    rec = ev.to_record()
    assert rec["kind"] == "skill_harvested"
    assert rec["issue"] == 430
    assert rec["area"] == "pulsar-node/http-route"
    assert EVENT_REGISTRY["skill_harvested"] is SkillHarvestedEvent


def test_skill_harvested_rejects_out_of_range_confidence() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SkillHarvestedEvent(issue=1, area="a", skill_key="k", memory_id="m", sha="s", confidence=5)


def test_skill_injected_carries_rank_and_kind() -> None:
    ev = SkillInjectedEvent(issue=1, memory_id="m", area="a", rank=0)
    rec = ev.to_record()
    assert rec["kind"] == "skill_injected"
    assert rec["rank"] == 0


def test_promoted_and_expired_are_registered() -> None:
    assert EVENT_REGISTRY["skill_promoted"] is SkillPromotedEvent
    assert EVENT_REGISTRY["skill_expired"] is SkillExpiredEvent
