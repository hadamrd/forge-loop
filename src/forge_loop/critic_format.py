"""Single source of truth for the critic's inline-finding tag format.

The critic stamps every inline review finding with a machine-readable tag
``**[<severity>/<category>]**`` (see
``critic_actions.apply_critic_report`` — the producer). Downstream, the repair
selector and the auto-merge gates in :mod:`forge_loop.gh_issues` classify a
leftover review thread as *the critic's own* (vs a human request-changes
thread) by matching that same tag on the thread's opening comment (#230 AC3).

Producer and classifier MUST agree on ONE spelling of the format. When they
were spelled independently — an ``f"**[{sev}/{cat}]**"`` on the producer side
and a hand-written ``^\\s*\\*\\*\\[sev[123]/`` regex on the classifier side —
a change to either silently desynchronised them: every critic thread would be
misclassified as *human*, the auto-merge gate would never fire, and a
critic-approved PR would stall forever in the repair loop (the #229 multi-hour
stall this ticket exists to kill). This module is that one spelling: the
producer calls :func:`finding_tag`, the classifier calls :func:`is_finding_body`,
and :data:`FINDING_TAG_RE` is *derived* from the same severity/category
vocabulary so the two cannot drift. ``tests/test_critic_format.py`` locks the
round-trip (every ``finding_tag`` output matches ``is_finding_body``).
"""

from __future__ import annotations

import re

#: The severity tokens the critic emits, most-severe first. Re-exported by
#: :mod:`forge_loop.critic` as ``VALID_SEVERITY`` so the vocabulary lives in
#: exactly one place.
SEVERITIES: tuple[str, ...] = ("sev1", "sev2", "sev3")

#: The finding categories the critic emits. Re-exported by
#: :mod:`forge_loop.critic` as ``VALID_CATEGORY``.
CATEGORIES: tuple[str, ...] = (
    "correctness",
    "security",
    "style",
    "tests",
    "docs",
    "product",
    "performance",
    "architecture",
)


def finding_tag(severity: str, category: str) -> str:
    """Return the machine-readable tag the critic prepends to every finding.

    This is the ONE place the ``**[severity/category]**`` literal is spelled.
    The producer (``critic_actions``) builds inline/summary comment bodies from
    it; :data:`FINDING_TAG_RE` matches it on the classifier side.
    """
    return f"**[{severity}/{category}]**"


def _build_tag_re() -> re.Pattern[str]:
    """Compile the classifier regex from the SAME vocabulary the producer uses.

    Anchored at the start (tolerating leading whitespace), it matches the full
    ``**[<known-sev>/<known-cat>]**`` tag — not just a ``**[sev`` prefix — so a
    human comment that merely *opens* with two asterisks, or pastes an unknown
    ``[sevN/...]`` token, does not get mistaken for a critic finding (#230
    sev3/correctness: never auto-merge over a human request-changes thread).
    """
    sev_alt = "|".join(re.escape(s) for s in SEVERITIES)
    cat_alt = "|".join(re.escape(c) for c in CATEGORIES)
    return re.compile(rf"^\s*\*\*\[(?:{sev_alt})/(?:{cat_alt})\]\*\*")


#: Regex matching the critic's inline-finding tag at the start of a comment
#: body. Derived from :data:`SEVERITIES` / :data:`CATEGORIES` so it can never
#: drift from :func:`finding_tag`.
FINDING_TAG_RE: re.Pattern[str] = _build_tag_re()


def is_finding_body(body: str | None) -> bool:
    """Return ``True`` iff ``body`` opens with the critic's inline-finding tag.

    Used to classify a review thread as the critic's own leftover finding (vs a
    human review thread) by its opening comment body — authorship login is
    unreliable when the loop dogfoods itself (critic and reviewers can share one
    GitHub identity), but the critic's body format is stable code.
    """
    return bool(FINDING_TAG_RE.match(body or ""))
