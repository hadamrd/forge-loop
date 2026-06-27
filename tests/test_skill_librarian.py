"""The librarian distils a merged diff into a structured SkillCard."""

from __future__ import annotations

from forge_loop.skill_librarian import (
    SkillCard,
    distill_skill_from_merge,
    parse_skill_card,
)

_VALID = """
{
  "area": "pulsar-node/http-route",
  "failing_signal": "ledger-only change 500s on diff",
  "target": "bins/pulsar-node/src/ledger.rs",
  "trigger": "adding a GET endpoint to the node",
  "procedure": "1. add handler 2. route it 3. test",
  "pitfalls": "run cargo test -p pulsar-node --bin pulsar-node",
  "confidence": 0.9
}
"""


def test_parse_extracts_all_fields() -> None:
    card = parse_skill_card(_VALID)
    assert card == SkillCard(
        area="pulsar-node/http-route",
        failing_signal="ledger-only change 500s on diff",
        target="bins/pulsar-node/src/ledger.rs",
        trigger="adding a GET endpoint to the node",
        procedure="1. add handler 2. route it 3. test",
        pitfalls="run cargo test -p pulsar-node --bin pulsar-node",
        confidence=0.9,
    )


def test_parse_tolerates_markdown_fences() -> None:
    fenced = "```json\n" + _VALID.strip() + "\n```"
    card = parse_skill_card(fenced)
    assert card is not None
    assert card.area == "pulsar-node/http-route"


def test_parse_malformed_json_returns_none() -> None:
    assert parse_skill_card("not json at all") is None


def test_parse_missing_required_field_returns_none() -> None:
    # no "procedure" — a card with no recipe is worthless
    assert parse_skill_card('{"area":"a","target":"t","trigger":"x"}') is None


def test_parse_defaults_optional_fields() -> None:
    minimal = '{"area":"a/b","target":"t","trigger":"x","procedure":"do it"}'
    card = parse_skill_card(minimal)
    assert card is not None
    assert card.failing_signal == ""
    assert card.pitfalls == ""
    assert card.confidence == 0.8  # default when absent


def test_parse_clamps_out_of_range_confidence() -> None:
    raw = '{"area":"a","target":"t","trigger":"x","procedure":"p","confidence":5}'
    card = parse_skill_card(raw)
    assert card is not None
    assert card.confidence == 1.0


def test_distill_passes_diff_and_title_to_the_model_and_parses() -> None:
    seen: dict[str, str] = {}

    def fake_llm(prompt: str) -> str:
        seen["prompt"] = prompt
        return _VALID

    card = distill_skill_from_merge(
        diff="diff --git a/ledger.rs b/ledger.rs\n+fn foo() {}",
        issue_title="APP4: app-scoped token auth",
        acceptance="app writes through can()",
        call_llm=fake_llm,
    )
    assert card is not None
    assert card.target == "bins/pulsar-node/src/ledger.rs"
    # the model must actually receive the material to distil from
    assert "app-scoped token auth" in seen["prompt"]
    assert "diff --git" in seen["prompt"]


def test_distill_returns_none_when_model_output_is_garbage() -> None:
    card = distill_skill_from_merge(
        diff="d",
        issue_title="t",
        acceptance="a",
        call_llm=lambda _p: "sorry I cannot help",
    )
    assert card is None
