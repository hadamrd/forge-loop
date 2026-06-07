"""Tests for deterministic branch GC (forge_loop.branch_sweep).

The sweep is provably conservative: only loop/<n> branches whose issue is CLOSED
are deleted; everything else (non-loop, open issue, unknown state, protected) is
preserved. These tests pin each classification edge + the failure handling.
"""

from __future__ import annotations

from forge_loop.branch_sweep import loop_issue_number, sweep


class _FakeGh:
    def __init__(
        self,
        states: dict[int, str | None],
        *,
        fail_delete: set[str] | None = None,
        raise_state: set[int] | None = None,
    ) -> None:
        self.states = states
        self.fail_delete = fail_delete or set()
        self.raise_state = raise_state or set()
        self.deleted: list[str] = []

    def get_issue_state(self, owner: str, repo: str, number: int) -> str | None:
        if number in self.raise_state:
            raise RuntimeError("boom")
        return self.states.get(number)

    def delete_branch(self, owner: str, repo: str, branch: str) -> bool:
        if branch in self.fail_delete:
            return False
        self.deleted.append(branch)
        return True


def test_loop_issue_number_parsing() -> None:
    assert loop_issue_number("loop/156-feat-x") == 156
    assert loop_issue_number("loop/42") == 42
    assert loop_issue_number(" loop/7-y ") == 7
    assert loop_issue_number("feat/foo") is None
    assert loop_issue_number("trunk") is None
    assert loop_issue_number("loop/abc") is None
    assert loop_issue_number("notloop/12") is None


def test_sweep_deletes_only_closed_loop_branches() -> None:
    gh = _FakeGh({156: "closed", 241: "open", 999: None})
    rep = sweep(
        gh,
        owner="o",
        repo="r",
        branch_names=["loop/156-x", "loop/241-y", "loop/999-z", "feat/keep", "trunk"],
    )
    assert rep.deleted == ["loop/156-x"]
    assert rep.skipped_open == ["loop/241-y"]
    assert set(rep.skipped_unknown) == {"loop/999-z", "feat/keep", "trunk"}
    assert gh.deleted == ["loop/156-x"]


def test_sweep_state_is_case_insensitive() -> None:
    gh = _FakeGh({1: "CLOSED", 2: " Closed "})
    rep = sweep(gh, owner="o", repo="r", branch_names=["loop/1", "loop/2"])
    assert set(rep.deleted) == {"loop/1", "loop/2"}


def test_sweep_never_touches_protected_or_non_loop() -> None:
    gh = _FakeGh({5: "closed"})
    rep = sweep(
        gh,
        owner="o",
        repo="r",
        branch_names=["loop/5", "main", "feat/x"],
        protected=frozenset({"loop/5", "main"}),
    )
    assert rep.deleted == []
    assert gh.deleted == []
    assert rep.skipped_unknown == ["feat/x"]


def test_sweep_records_errors_without_crashing() -> None:
    gh = _FakeGh({1: "closed", 2: "closed"}, fail_delete={"loop/1"}, raise_state={2})
    rep = sweep(gh, owner="o", repo="r", branch_names=["loop/1", "loop/2"])
    assert rep.deleted == []
    assert rep.errors["loop/1"] == "delete returned False"
    assert "boom" in rep.errors["loop/2"]
