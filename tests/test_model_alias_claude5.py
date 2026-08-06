"""The model validator must accept the CURRENT generation of model names.

☠ It did not. `_MODEL_PATTERN` demanded ``claude-<family>-<major>-<minor>`` with BOTH numbers, so it
structurally could not express the Claude 5 family (claude-opus-5, claude-sonnet-5) and refused a
valid config at startup with "unknown model alias" — wording that reads as a typo and sends the
operator to edit the wrong file. A validator that rejects the current generation of the thing it
validates is worse than none: it blocks the correct value.
"""

from __future__ import annotations

import pytest

from forge_loop.settings import _MODEL_PATTERN


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-8",  # the older two-number shape still works
        "claude-sonnet-4-6",
    ],
)
def test_accepts_real_model_ids(model: str) -> None:
    assert _MODEL_PATTERN.match(model), f"{model} is a real model id and must validate"


@pytest.mark.parametrize(
    "model",
    ["claude-5", "opus-5", "gpt-4", "claude-turbo-5", "", "claude-opus-"],
)
def test_still_rejects_nonsense(model: str) -> None:
    """NEV-CTL-04: prove the pattern can still FAIL, or the accept test proves nothing."""
    assert not _MODEL_PATTERN.match(model), f"{model} must not validate"
