"""Unit tests for the pure write-root-violation helper (issue #443).

``write_root_violations`` underpins the write-root-escape merge gate: given the
paths a worker's diff touched and the filesystem write-roots it was leased, it
returns the paths that escaped the sandbox. The matrix below mirrors the issue
body: in-bounds → empty, out-of-bounds → that path, normalization (no false
positive on trailing-slash / relative-vs-abs), and the closed-by-default stance
on an empty lease.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from forge_loop.sandbox.policy import write_root_violations


def test_in_bounds_path_is_no_violation(tmp_path: Path) -> None:
    root = str(tmp_path)
    inside = str(tmp_path / "src" / "forge_loop" / "x.py")
    assert write_root_violations((inside,), (root,)) == ()


def test_out_of_bounds_path_is_a_violation(tmp_path: Path) -> None:
    root = str(tmp_path / "wt")
    escape = "/home/u/forge-loop/src/forge_loop/runner/tick.py"
    assert write_root_violations((escape,), (root,)) == (escape,)


def test_mixes_in_and_out_of_bounds(tmp_path: Path) -> None:
    root = str(tmp_path / "wt")
    inside = str(tmp_path / "wt" / "a.py")
    escape = "/etc/passwd"
    assert write_root_violations((inside, escape), (root,)) == (escape,)


def test_trailing_slash_insensitive_no_false_positive(tmp_path: Path) -> None:
    # write root has a trailing slash; changed path does not — still in-bounds.
    root_slash = str(tmp_path) + "/"
    inside = str(tmp_path / "a.py")
    assert write_root_violations((inside,), (root_slash,)) == ()
    # And the root itself (with trailing slash) equals the root → in-bounds.
    assert write_root_violations((str(tmp_path) + "/",), (str(tmp_path),)) == ()


def test_relative_vs_abs_no_false_positive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A relative changed path resolving under an absolute write root is in-bounds.
    monkeypatch.chdir(tmp_path)
    assert write_root_violations(("sub/a.py",), (str(tmp_path),)) == ()


def test_empty_write_roots_is_closed_by_default(tmp_path: Path) -> None:
    # No lease at all → conservative: any changed path is a violation.
    changed = str(tmp_path / "a.py")
    assert write_root_violations((changed,), ()) == (changed,)
    # A whitespace-only / empty root entry is treated as "no root".
    assert write_root_violations((changed,), ("", "  ")) == (changed,)


def test_clean_empty_diff_is_no_violation(tmp_path: Path) -> None:
    assert write_root_violations((), (str(tmp_path),)) == ()
    # Blank changed entries are ignored, not reported as escapes.
    assert write_root_violations(("", "   "), ()) == ()


def test_violations_are_deduplicated_order_preserving() -> None:
    escape = "/etc/passwd"
    other = "/etc/shadow"
    assert write_root_violations((escape, other, escape), ("/safe",)) == (escape, other)


def test_multiple_write_roots_any_match_is_in_bounds(tmp_path: Path) -> None:
    root_a = str(tmp_path / "a")
    root_b = str(tmp_path / "b")
    inside_b = str(tmp_path / "b" / "deep" / "x.py")
    assert write_root_violations((inside_b,), (root_a, root_b)) == ()


@given(
    st.lists(st.text()),
    st.lists(st.text()),
)
def test_never_raises_on_arbitrary_text(changed: list[str], roots: list[str]) -> None:
    # Property (T5): the pure helper consumes user-shaped strings (paths from a
    # diff) and must never raise — only ever return a tuple of strings.
    result = write_root_violations(changed, roots)
    assert isinstance(result, tuple)
    assert all(isinstance(p, str) for p in result)
