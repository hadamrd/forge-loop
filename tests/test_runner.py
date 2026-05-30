

def test_installed_version_returns_string_or_empty() -> None:
    """Smoke check the version reader doesn't crash."""
    from forge_loop.runner import _installed_version
    v = _installed_version()
    assert isinstance(v, str)
    # Either we get a real version or empty string — never None/raises.


def test_reap_orphan_worktrees_handles_missing_dir(tmp_path) -> None:
    """No worktrees → no crash, no event."""
    from forge_loop.runner import _reap_orphan_worktrees
    events = tmp_path / "events.jsonl"
    events.touch()
    # No /tmp/wt-loop-* exists for issue 99999 → reaper returns 0.
    reaped = _reap_orphan_worktrees(tmp_path, events)
    assert reaped == 0
    # No event should have been written for zero reaps.
    assert events.read_text() == ""


def test_reap_orphan_worktrees_skips_non_numeric_paths(tmp_path) -> None:
    """A /tmp/wt-loop-* path whose suffix isn't an int must be skipped silently."""
    from forge_loop.runner import _reap_orphan_worktrees
    events = tmp_path / "events.jsonl"
    events.touch()
    decoy = tmp_path / "wt-loop-not-an-int"
    decoy.mkdir()
    # Reaper only looks at /tmp/wt-loop-* so this dir is invisible to it,
    # but if someone moves the prefix in the future this asserts that bad
    # names don't blow up the boot path.
    reaped = _reap_orphan_worktrees(tmp_path, events)
    assert reaped == 0


def test_issue_number_from_pr_prefers_loop_branch() -> None:
    from forge_loop.runner.tick import _issue_number_from_pr

    assert _issue_number_from_pr({"headRefName": "loop/123-fix-the-thing"}) == 123


def test_issue_number_from_pr_falls_back_to_body_reference() -> None:
    from forge_loop.runner.tick import _issue_number_from_pr

    assert _issue_number_from_pr({"body": "Fixes #456"}) == 456
