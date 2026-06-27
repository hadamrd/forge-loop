"""Distil a merged diff into a structured, reusable procedural SkillCard.

The "librarian" is the harvest-time component that reads a *critic-clean* merged
PR (its diff + the issue title + acceptance criteria) and extracts ONE reusable
"how to do X in this repo" card. It is deliberately split into a pure core and a
thin impure edge:

- :func:`parse_skill_card` and :func:`distill_skill_from_merge` are pure and take
  the model call as an injected ``call_llm`` callable, so all of the structure,
  prompt assembly, and parse/validation logic is unit-tested without network.
- :func:`default_librarian` returns the real ``call_llm`` backed by
  :func:`forge_loop._critic_sdk.run_critic_sdk` (a cheap one-shot SDK session).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SkillCard",
    "parse_skill_card",
    "distill_skill_from_merge",
    "default_librarian",
]

#: Cap the diff we feed the model so a huge PR never blows the prompt budget.
_MAX_DIFF_CHARS = 6000

#: Default confidence when the model omits one. Mid-high: a card distilled from a
#: critic-clean merge is trustworthy, but absence of an explicit score is a mild
#: signal to not over-rank it.
_DEFAULT_CONFIDENCE = 0.8

_REQUIRED_FIELDS = ("area", "target", "trigger", "procedure")


@dataclass(frozen=True)
class SkillCard:
    """A reusable procedural recipe distilled from a merged change.

    ``area`` is the card's address in the skill tree (``/``-delimited).
    ``(failing_signal, target)`` is the dedup signature (the skill-key). The
    ``trigger``/``procedure``/``pitfalls`` are the human-usable recipe.
    """

    area: str
    target: str
    trigger: str
    procedure: str
    failing_signal: str = ""
    pitfalls: str = ""
    confidence: float = _DEFAULT_CONFIDENCE

    def to_body(self) -> str:
        """Render the card as the structured ``MemoryItem.body`` text."""
        lines = [
            f"trigger: {self.trigger}",
            f"procedure: {self.procedure}",
        ]
        if self.pitfalls:
            lines.append(f"pitfalls: {self.pitfalls}")
        return "\n".join(lines)


def _strip_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence the model may have added."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Drop the opening fence line (``` or ```json) and the closing fence.
    body = stripped[3:]
    newline = body.find("\n")
    if newline != -1:
        body = body[newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def parse_skill_card(raw: str) -> SkillCard | None:
    """Parse the model's JSON output into a :class:`SkillCard`, or ``None``.

    Returns ``None`` for malformed JSON, a non-object payload, or a payload
    missing any required field (``area``/``target``/``trigger``/``procedure``) —
    a card without an area, a target, or a recipe is worthless. Optional fields
    default; ``confidence`` is clamped to ``[0, 1]`` and falls back to the
    default when absent or non-numeric.
    """
    try:
        payload = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    fields: dict[str, str] = {}
    for key in _REQUIRED_FIELDS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        fields[key] = value.strip()

    failing_signal = payload.get("failing_signal", "")
    pitfalls = payload.get("pitfalls", "")
    confidence = _coerce_confidence(payload.get("confidence"))

    return SkillCard(
        area=fields["area"],
        target=fields["target"],
        trigger=fields["trigger"],
        procedure=fields["procedure"],
        failing_signal=str(failing_signal).strip(),
        pitfalls=str(pitfalls).strip(),
        confidence=confidence,
    )


def _coerce_confidence(value: object) -> float:
    try:
        conf = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, conf))


def _build_prompt(diff: str, issue_title: str, acceptance: str) -> str:
    clipped = diff[:_MAX_DIFF_CHARS]
    return f"""You are the librarian for an autonomous coding loop. A pull request just \
merged after passing review. Distil ONE reusable procedural skill card capturing \
"how to do this kind of change in this repo" so a future worker need not re-derive it.

ISSUE TITLE: {issue_title}
ACCEPTANCE CRITERIA: {acceptance}

MERGED DIFF (clipped):
{clipped}

Return ONLY a JSON object with these fields, no prose:
- "area": the skill's address in the tree, slash-delimited from coarse to fine \
(e.g. "pulsar-node/http-route", "ui/page"). Use existing top-level areas when one fits.
- "failing_signal": the symptom/trigger that this change addressed (may be "" for a \
pure feature).
- "target": the primary file or module this applies to.
- "trigger": one line — when a future worker should reach for this skill.
- "procedure": the reusable recipe — the files to touch, the pattern to copy, the \
exact test command to prove it.
- "pitfalls": known gotchas (may be "").
- "confidence": 0.0-1.0, how well this generalises beyond this one change."""


def distill_skill_from_merge(
    *,
    diff: str,
    issue_title: str,
    acceptance: str,
    call_llm: Callable[[str], str],
) -> SkillCard | None:
    """Distil a merged change into a :class:`SkillCard` via ``call_llm``.

    Pure with respect to I/O: the model call is injected, so the prompt assembly
    and parsing are fully unit-testable. Returns ``None`` when the model output
    cannot be parsed into a valid card.
    """
    prompt = _build_prompt(diff, issue_title, acceptance)
    raw = call_llm(prompt)
    return parse_skill_card(raw)


def default_librarian(
    *,
    cwd: Path,
    model: str = "claude-haiku-4-5-20251001",
    timeout_s: int = 180,
) -> Callable[[str], str]:
    """Return the real ``call_llm`` backed by a cheap one-shot SDK session.

    Defaults to a small/fast model — distillation is a bounded summarisation
    task, not a reasoning-heavy one, so it should be cheap per merge.
    """
    from forge_loop._critic_sdk import run_critic_sdk

    def _call(prompt: str) -> str:
        result = run_critic_sdk(prompt, cwd=cwd, timeout_s=timeout_s, model=model)
        if result.error:
            return ""
        return result.last_message

    return _call
