"""Property-based tests for :func:`forge_loop.attempts.compute_fingerprint`.

Three invariants (issue #91):
* **Determinism**: same inputs → same fingerprint, always.
* **Sensitivity**: changing any one of (issue_id, issue_body,
  brief_template_hash) must change the fingerprint (avoids the
  cooldown-bypass bug where two materially different attempts produce
  the same hash).
* **Unicode-safety**: arbitrary unicode in issue_body must not crash
  the hasher.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from forge_loop.attempts import compute_fingerprint

_issue_id_st = st.one_of(
    st.integers(min_value=1, max_value=999_999),
    st.text(min_size=1, max_size=40),
)

_body_st = st.text(
    alphabet=st.characters(blacklist_characters="\x00"),
    max_size=2000,
)

_brief_hash_st = st.text(
    alphabet=st.characters(blacklist_characters="\x00"),
    max_size=80,
)


@given(issue_id=_issue_id_st, body=_body_st, brief_hash=_brief_hash_st)
@settings(max_examples=200, deadline=None)
def test_fingerprint_is_deterministic(
    issue_id: int | str, body: str, brief_hash: str
) -> None:
    a = compute_fingerprint(issue_id, body, brief_hash)
    b = compute_fingerprint(issue_id, body, brief_hash)
    assert a == b


@given(issue_id=_issue_id_st, body=_body_st, brief_hash=_brief_hash_st)
@settings(max_examples=200, deadline=None)
def test_fingerprint_changes_when_body_changes(
    issue_id: int | str, body: str, brief_hash: str
) -> None:
    """Mutating the body (append a non-empty marker) must alter the hash —
    otherwise the cooldown gate would mistake a re-spec for a retry."""
    original = compute_fingerprint(issue_id, body, brief_hash)
    mutated = compute_fingerprint(issue_id, body + "\nsentinel-mutation", brief_hash)
    assert original != mutated


@given(issue_id=_issue_id_st, body=_body_st, brief_hash=_brief_hash_st)
@settings(max_examples=200, deadline=None)
def test_fingerprint_changes_when_brief_hash_changes(
    issue_id: int | str, body: str, brief_hash: str
) -> None:
    """Brief-template changes must invalidate the cache — the worker is
    being asked to do meaningfully different work."""
    original = compute_fingerprint(issue_id, body, brief_hash)
    mutated = compute_fingerprint(issue_id, body, brief_hash + "x")
    assert original != mutated


@given(
    body=st.text(
        alphabet=st.characters(blacklist_characters="\x00"),
        max_size=2000,
    )
)
@settings(max_examples=200, deadline=None)
def test_fingerprint_unicode_safe(body: str) -> None:
    """Arbitrary unicode (emoji, RTL, combining marks) must not crash."""
    digest = compute_fingerprint(42, body, "deadbeef")
    # 64-char hex sha256 hexdigest
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


@given(
    issue_id=st.integers(min_value=1, max_value=999_999),
    body=_body_st,
    brief_hash=_brief_hash_st,
)
@settings(max_examples=100, deadline=None)
def test_fingerprint_distinguishes_int_issue_ids(
    issue_id: int, body: str, brief_hash: str
) -> None:
    """A boundary case — two adjacent integer issue IDs must produce
    different fingerprints when body + brief are equal."""
    a = compute_fingerprint(issue_id, body, brief_hash)
    b = compute_fingerprint(issue_id + 1, body, brief_hash)
    assert a != b
