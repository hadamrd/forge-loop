"""The single shared GitHub PR-reference parser (#242 review fix).

`canonical_pr_key`, `critic._repo_slug_from_pr_url` and
`runner.recovery._parse_pr_url` must all build on ONE parser so the critic
write path and the repair read path of the findings store can never drift
(the Q10 failure mode #242 kills).
"""

from __future__ import annotations

import pytest

from forge_loop.critic import _repo_slug_from_pr_url, canonical_pr_key
from forge_loop.pr_ref import parse_pr_url, repo_slug
from forge_loop.runner.recovery import _parse_pr_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widgets/pull/7",
        "https://github.com/acme/widgets/pull/7/",
        "https://github.com/acme/widgets/pull/7?diff=split",
        "https://github.com/acme/widgets/pull/7#discussion",
        "https://api.github.com/repos/acme/widgets/pulls/7",
        "git@github.com:acme/widgets.git/pull/7",
    ],
)
def test_parse_pr_url_collapses_every_shape(url: str) -> None:
    assert parse_pr_url(url) == ("acme", "widgets", 7)


def test_parse_pr_url_rejects_non_pr() -> None:
    assert parse_pr_url(None) is None
    assert parse_pr_url("") is None
    assert parse_pr_url("not a url") is None


def test_repo_slug() -> None:
    assert repo_slug("https://github.com/acme/widgets/pull/7") == "acme/widgets"
    assert repo_slug("https://github.com/acme/widgets/issues/3") == "acme/widgets"
    assert repo_slug("garbage") == ""


def test_consumers_delegate_to_shared_parser() -> None:
    """The three flagged duplicate parsers now share one implementation."""
    url = "https://github.com/acme/widgets/pull/7"
    # recovery delegates → same tuple as the shared parser.
    assert _parse_pr_url(url) == parse_pr_url(url) == ("acme", "widgets", 7)
    # critic's slug extractor delegates.
    assert _repo_slug_from_pr_url(url) == "acme/widgets"
    # canonical key is built on the shared parser → HTML / REST / trailing
    # slash all collapse to one stable key.
    canon = canonical_pr_key(url)
    assert canonical_pr_key(url + "/") == canon
    assert canonical_pr_key("https://api.github.com/repos/acme/widgets/pulls/7") == canon
