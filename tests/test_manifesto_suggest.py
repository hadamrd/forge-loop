"""Unit + module-integration tests for manifesto suggest (issue #134).

Covers the #134 test matrix:
  * ``ManifestoSuggestion`` schema round-trip + unknown-field ignore.
  * Context assembly from a fixture bug PR diff with the gh layer faked.
  * Suggestion parsing (canned JSON) + rationale-references-bug assertion.
  * Markdown-delta logic + PR-plan shape (targets ``.forge/*-manifesto.md``).
  * ``open_manifesto_pr`` git/gh dance: both returncode==0 and !=0 (T3).
  * Adversarial / sad paths: insufficient context, malformed/empty SDK output,
    linked-issue absent / get_issue None / gh raising (T2).
  * Property test on ``extract_linked_issue`` (T5: consumes user-supplied text).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from forge_loop.gh_client import Issue, MockGhClient
from forge_loop.manifesto_suggest import (
    BugContext,
    EditKind,
    InsufficientContextError,
    ManifestoPrPlan,
    ManifestoSuggester,
    ManifestoSuggestion,
    ManifestoTarget,
    ProposedManifestoEdit,
    SuggestionParseError,
    assemble_bug_context,
    build_markdown_delta,
    build_pr_plan,
    build_prompt,
    extract_linked_issue,
    open_manifesto_pr,
    parse_suggestion,
    render_suggestion_text,
)
from forge_loop.manifestos import QUALITY_REL, TESTING_REL

FIXTURE = Path(__file__).parent / "fixtures" / "manifesto_suggest" / "pr207.diff"

PR_BODY = (
    "Fixes #199. A worker assumed payload['commits'] was always present and "
    "raised KeyError on API failure. This adds a `.get(...)` guard + a "
    "regression test."
)
COMMIT_META = "fix(#199): guard missing commits key\n\nUse .get with fallback."
ISSUE_BODY = "KeyError when gh JSON omits 'commits'. Worker crashes on PR summary."


def _fake_gh_module(
    *,
    body: str = PR_BODY,
    commits: str = COMMIT_META,
    diff: str | None = None,
    changed: list[str] | None = None,
    raise_on: str | None = None,
) -> Any:
    """Build a stand-in for ``forge_loop.gh`` with the three PR read helpers."""
    diff_text = FIXTURE.read_text(encoding="utf-8") if diff is None else diff
    changed_files = (
        ["src/forge_loop/worker.py", "tests/test_worker_summary.py"] if changed is None else changed
    )

    def pr_precommit_context(pr_ref: str, cwd: Path) -> tuple[str, str]:
        if raise_on == "context":
            raise RuntimeError("boom-context")
        return body, commits

    def pr_diff(pr_ref: str, cwd: Path) -> str:
        if raise_on == "diff":
            raise RuntimeError("boom-diff")
        return diff_text

    def pr_changed_files(pr_ref: str, cwd: Path) -> list[str]:
        if raise_on == "files":
            raise RuntimeError("boom-files")
        return changed_files

    return SimpleNamespace(
        pr_precommit_context=pr_precommit_context,
        pr_diff=pr_diff,
        pr_changed_files=pr_changed_files,
    )


def _gh_client_with_issue(number: int = 199) -> MockGhClient:
    gh = MockGhClient()
    gh.issues[("acme", "widgets", number)] = Issue(number=number, title="bug", body=ISSUE_BODY)
    return gh


def _sample_suggestion() -> ManifestoSuggestion:
    return ManifestoSuggestion(
        summary="Never index gh JSON payloads directly.",
        edits=[
            ProposedManifestoEdit(
                target=ManifestoTarget.QUALITY,
                kind=EditKind.ADD,
                rule_text="Never index gh JSON payloads directly; use .get(...) with a fallback.",
                rationale="Derived from PR #207 / issue #199: a KeyError shipped.",
            ),
            ProposedManifestoEdit(
                target=ManifestoTarget.TESTING,
                kind=EditKind.ADD,
                rule_text="Add a regression test for the missing-key branch.",
                rationale="PR #207 added a test for the absent 'commits' key.",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestManifestoSuggestionSchema:
    def test_round_trips_edits(self) -> None:
        s = _sample_suggestion()
        again = ManifestoSuggestion.model_validate(s.model_dump(mode="json"))
        assert again == s
        assert again.edits[0].target is ManifestoTarget.QUALITY
        assert again.edits[1].target is ManifestoTarget.TESTING
        assert again.edits[0].kind is EditKind.ADD

    def test_ignores_unknown_fields(self) -> None:
        payload = {
            "summary": "x",
            "edits": [
                {
                    "target": "quality",
                    "kind": "add",
                    "rule_text": "r",
                    "rationale": "PR #1",
                    "bogus_field": "ignored",
                }
            ],
            "extra_top_level": 123,
        }
        s = ManifestoSuggestion.model_validate(payload)
        assert len(s.edits) == 1
        assert not hasattr(s.edits[0], "bogus_field")

    def test_empty_suggestion_is_empty(self) -> None:
        assert ManifestoSuggestion().is_empty is True
        assert _sample_suggestion().is_empty is False


# ---------------------------------------------------------------------------
# Linked-issue extraction (property + examples)
# ---------------------------------------------------------------------------


class TestExtractLinkedIssue:
    def test_prefers_fixes_keyword(self) -> None:
        assert extract_linked_issue("Fixes #199. See #4 too.") == 199

    def test_falls_back_to_bare_ref(self) -> None:
        assert extract_linked_issue("relates to #42 somehow") == 42

    def test_none_when_no_ref(self) -> None:
        assert extract_linked_issue("no issue here") is None
        assert extract_linked_issue("") is None

    @given(st.text())
    def test_never_raises_on_arbitrary_text(self, text: str) -> None:
        # T5: consumes user-supplied string — must not raise on any input.
        result = extract_linked_issue(text)
        assert result is None or isinstance(result, int)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


class TestContextAssembly:
    def test_builds_full_context_from_fixture(self, tmp_path: Path) -> None:
        gh_module = _fake_gh_module()
        gh_client = _gh_client_with_issue(199)
        ctx = assemble_bug_context(
            207,
            repo_path=tmp_path,
            owner="acme",
            repo="widgets",
            gh_client=gh_client,
            gh_module=gh_module,
        )
        assert ctx.pr_number == 207
        assert "commits" in ctx.diff
        assert ctx.linked_issue_number == 199
        assert ctx.linked_issue_body == ISSUE_BODY
        assert ctx.test_files == ("tests/test_worker_summary.py",)
        assert ctx.is_sufficient is True
        # get_issue was actually consulted.
        assert ("get_issue", {"owner": "acme", "repo": "widgets", "number": 199}) in gh_client.calls

    def test_insufficient_when_all_reads_empty(self, tmp_path: Path) -> None:
        gh_module = _fake_gh_module(body="", commits="", diff="", changed=[])
        ctx = assemble_bug_context(404, repo_path=tmp_path, gh_module=gh_module)
        assert ctx.is_sufficient is False
        assert ctx.linked_issue_number is None

    def test_degrades_when_diff_fetch_raises(self, tmp_path: Path) -> None:
        # T2: external read fails — must not crash, diff degrades to "".
        gh_module = _fake_gh_module(raise_on="diff")
        ctx = assemble_bug_context(207, repo_path=tmp_path, gh_module=gh_module)
        assert ctx.diff == ""
        # body still read → still sufficient.
        assert ctx.is_sufficient is True

    def test_no_linked_issue_skips_client(self, tmp_path: Path) -> None:
        # T2 (false branch): no #N in the body → get_issue never called.
        gh_module = _fake_gh_module(body="no refs here at all")
        gh_client = _gh_client_with_issue(199)
        ctx = assemble_bug_context(
            207,
            repo_path=tmp_path,
            owner="acme",
            repo="widgets",
            gh_client=gh_client,
            gh_module=gh_module,
        )
        assert ctx.linked_issue_number is None
        assert ctx.linked_issue_body == ""
        assert gh_client.calls == []

    def test_get_issue_returns_none(self, tmp_path: Path) -> None:
        # T2: linked issue referenced but get_issue yields None (closed/missing).
        gh_module = _fake_gh_module()
        gh_client = MockGhClient()  # no issues populated → get_issue -> None
        ctx = assemble_bug_context(
            207,
            repo_path=tmp_path,
            owner="acme",
            repo="widgets",
            gh_client=gh_client,
            gh_module=gh_module,
        )
        assert ctx.linked_issue_number == 199
        assert ctx.linked_issue_body == ""

    def test_get_issue_raising_degrades(self, tmp_path: Path) -> None:
        gh_module = _fake_gh_module()
        from forge_loop.gh_client import GhError

        gh_client = MockGhClient(raise_on={"get_issue": GhError("get_issue", 500, "boom")})
        ctx = assemble_bug_context(
            207,
            repo_path=tmp_path,
            owner="acme",
            repo="widgets",
            gh_client=gh_client,
            gh_module=gh_module,
        )
        assert ctx.linked_issue_body == ""


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_prompt_includes_manifestos_and_bug(self) -> None:
        manifestos = SimpleNamespace(
            quality=SimpleNamespace(markdown="Q1. quality rule body"),
            testing=SimpleNamespace(markdown="T1. testing rule body"),
        )
        bug = BugContext(
            pr_number=207,
            pr_body=PR_BODY,
            commit_metadata=COMMIT_META,
            diff="DIFFTEXT-marker",
            linked_issue_number=199,
            linked_issue_body=ISSUE_BODY,
            test_files=("tests/test_worker_summary.py",),
        )
        prompt = build_prompt(manifestos, bug)
        assert "Q1. quality rule body" in prompt
        assert "T1. testing rule body" in prompt
        assert "DIFFTEXT-marker" in prompt
        assert "#207" in prompt
        assert "#199" in prompt
        assert "tests/test_worker_summary.py" in prompt

    def test_prompt_handles_empty_manifestos(self) -> None:
        manifestos = SimpleNamespace(
            quality=SimpleNamespace(markdown=""),
            testing=SimpleNamespace(markdown=""),
        )
        bug = BugContext(pr_number=5, pr_body="body", diff="d")
        prompt = build_prompt(manifestos, bug)
        assert "(none)" in prompt
        assert "#5" in prompt


# ---------------------------------------------------------------------------
# Suggestion parsing
# ---------------------------------------------------------------------------


class TestParseSuggestion:
    def test_parses_canned_json_and_references_bug(self) -> None:
        canned = (
            "Here is my analysis.\n"
            '{"summary": "guard gh JSON access", "edits": ['
            '{"target": "quality", "kind": "add", '
            '"rule_text": "Use .get on gh payloads.", '
            '"rationale": "Derived from PR #207 (issue #199)."}]}'
        )
        s = parse_suggestion(canned)
        assert len(s.edits) == 1
        assert s.edits[0].target is ManifestoTarget.QUALITY
        # The rationale references the bug (PR # or linked issue #).
        assert any("#207" in e.rationale or "#199" in e.rationale for e in s.edits)

    def test_whole_message_json(self) -> None:
        s = parse_suggestion('{"summary": "s", "edits": []}')
        assert s.summary == "s"
        assert s.is_empty

    def test_empty_message_fails_closed(self) -> None:
        with pytest.raises(SuggestionParseError):
            parse_suggestion("")

    def test_non_json_fails_closed(self) -> None:
        with pytest.raises(SuggestionParseError):
            parse_suggestion("the model rambled and produced no JSON at all")

    def test_malformed_json_fails_closed(self) -> None:
        with pytest.raises(SuggestionParseError):
            parse_suggestion('{"summary": "x", "edits": [')

    def test_json_array_not_object_fails_closed(self) -> None:
        with pytest.raises(SuggestionParseError):
            parse_suggestion("[1, 2, 3]")


# ---------------------------------------------------------------------------
# Markdown delta + PR plan
# ---------------------------------------------------------------------------


class TestMarkdownDelta:
    def test_targets_both_manifesto_files(self) -> None:
        manifestos = SimpleNamespace(
            quality=SimpleNamespace(markdown="# Quality\n\nQ6. existing"),
            testing=SimpleNamespace(markdown="# Testing\n\nT1. existing"),
        )
        delta = build_markdown_delta(_sample_suggestion(), manifestos, pr_number=207)
        assert set(delta) == {QUALITY_REL, TESTING_REL}
        assert "Q6. existing" in delta[QUALITY_REL]  # preserves existing body
        assert "Never index gh JSON payloads directly" in delta[QUALITY_REL]
        assert "PR #207" in delta[QUALITY_REL]
        assert "regression test" in delta[TESTING_REL]

    def test_only_touched_side_appears(self) -> None:
        manifestos = SimpleNamespace(
            quality=SimpleNamespace(markdown="# Q"),
            testing=SimpleNamespace(markdown="# T"),
        )
        s = ManifestoSuggestion(
            edits=[
                ProposedManifestoEdit(
                    target=ManifestoTarget.QUALITY,
                    kind=EditKind.ADD,
                    rule_text="r",
                    rationale="PR #1",
                )
            ]
        )
        delta = build_markdown_delta(s, manifestos, pr_number=1)
        assert set(delta) == {QUALITY_REL}

    def test_backcompat_empty_manifesto_seeds_header(self) -> None:
        # Repo with no .forge manifestos: markdown == "" → still produces a file.
        manifestos = SimpleNamespace(
            quality=SimpleNamespace(markdown=""),
            testing=SimpleNamespace(markdown=""),
        )
        s = ManifestoSuggestion(
            edits=[
                ProposedManifestoEdit(
                    target=ManifestoTarget.QUALITY,
                    kind=EditKind.ADD,
                    rule_text="brand new rule",
                    rationale="PR #9",
                )
            ]
        )
        delta = build_markdown_delta(s, manifestos, pr_number=9)
        assert QUALITY_REL in delta
        assert "# Manifesto" in delta[QUALITY_REL]
        assert "brand new rule" in delta[QUALITY_REL]


class TestBuildPrPlan:
    def test_plan_shape(self) -> None:
        manifestos = SimpleNamespace(
            quality=SimpleNamespace(markdown="# Q"),
            testing=SimpleNamespace(markdown="# T"),
        )
        plan = build_pr_plan(_sample_suggestion(), manifestos, pr_number=207)
        assert isinstance(plan, ManifestoPrPlan)
        assert plan.branch == "manifesto/suggest-from-pr-207"
        assert "#207" in plan.title
        assert set(plan.file_contents) == {QUALITY_REL, TESTING_REL}
        assert "#207" in plan.body
        assert not plan.is_empty

    def test_empty_suggestion_yields_empty_plan(self) -> None:
        manifestos = SimpleNamespace(
            quality=SimpleNamespace(markdown="# Q"),
            testing=SimpleNamespace(markdown="# T"),
        )
        plan = build_pr_plan(ManifestoSuggestion(), manifestos, pr_number=1)
        assert plan.is_empty


# ---------------------------------------------------------------------------
# open_manifesto_pr — git/gh dance with injected runner (T3: rc==0 and !=0)
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Records the git commands ``open_manifesto_pr`` runs.

    Only ``git`` is driven through this runner now — the PR open goes through
    the GhClient (``create_pull``), not a ``gh pr create`` subprocess (#223).
    """

    def __init__(self, *, fail_cmd: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_cmd = fail_cmd

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        self.calls.append(cmd)
        if self.fail_cmd is not None and self.fail_cmd in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"{self.fail_cmd} failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _plan() -> ManifestoPrPlan:
    return ManifestoPrPlan(
        pr_number=207,
        branch="manifesto/suggest-from-pr-207",
        title="t",
        body="b",
        commit_message="m",
        file_contents={QUALITY_REL: "# Quality\n\nnew rule\n"},
    )


class TestOpenManifestoPr:
    def test_happy_path_writes_files_and_returns_url(self, tmp_path: Path) -> None:
        from forge_loop import gh_issues
        from forge_loop.gh_client import MockGhClient

        client = MockGhClient(create_pull_url="https://github.com/acme/widgets/pull/777")
        gh_issues.set_client(client)
        try:
            runner = _RecordingRunner()
            url = open_manifesto_pr(
                _plan(),
                repo_path=tmp_path,
                github_repo="acme/widgets",
                runner=runner,
            )
        finally:
            gh_issues.set_client(None)
        assert url == "https://github.com/acme/widgets/pull/777"
        # File actually written with the delta.
        assert (tmp_path / QUALITY_REL).read_text(encoding="utf-8") == "# Quality\n\nnew rule\n"
        # The PR was opened via the client (not a gh subprocess), exactly once,
        # for the right repo/branch.
        creates = [c for c in client.calls if c[0] == "create_pull"]
        assert len(creates) == 1
        assert creates[0][1]["repo"] == "widgets"
        assert creates[0][1]["head"] == "manifesto/suggest-from-pr-207"
        # git ran; no gh subprocess.
        assert any(c[:1] == ["git"] for c in runner.calls)
        assert not any(c and c[0] == "gh" for c in runner.calls)

    def test_guard_without_repo_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="github_repo"):
            open_manifesto_pr(
                _plan(), repo_path=tmp_path, github_repo="", runner=_RecordingRunner()
            )

    def test_empty_plan_raises(self, tmp_path: Path) -> None:
        empty = ManifestoPrPlan(
            pr_number=1, branch="b", title="t", body="b", commit_message="m", file_contents={}
        )
        with pytest.raises(ValueError, match="nothing to apply"):
            open_manifesto_pr(empty, repo_path=tmp_path, github_repo="acme/widgets")

    def test_git_step_failure_raises(self, tmp_path: Path) -> None:
        # T3: a subprocess returning rc != 0 must raise, not silently pass.
        runner = _RecordingRunner(fail_cmd="commit")
        with pytest.raises(RuntimeError, match="failed"):
            open_manifesto_pr(
                _plan(),
                repo_path=tmp_path,
                github_repo="acme/widgets",
                runner=runner,
            )
        # No gh pr create attempted after the failed commit.
        assert not any(c[:3] == ["gh", "pr", "create"] for c in runner.calls)


# ---------------------------------------------------------------------------
# ManifestoSuggester.run — orchestration with injected sdk_fn
# ---------------------------------------------------------------------------


class TestManifestoSuggesterRun:
    def _suggester(
        self, tmp_path: Path, *, sdk_fn: Any, gh_module: Any = None
    ) -> ManifestoSuggester:
        return ManifestoSuggester(
            repo_path=tmp_path,
            owner="acme",
            repo="widgets",
            gh_client=_gh_client_with_issue(199),
            gh_module=gh_module or _fake_gh_module(),
            sdk_fn=sdk_fn,
        )

    def test_run_returns_suggestion(self, tmp_path: Path) -> None:
        captured: dict[str, Any] = {}

        def sdk_fn(prompt: str, **kwargs: Any) -> Any:
            captured["prompt"] = prompt
            return SimpleNamespace(
                last_message='{"summary": "s", "edits": [{"target": "quality", '
                '"kind": "add", "rule_text": "r", "rationale": "PR #207"}]}',
                error=None,
                timed_out=False,
            )

        s = self._suggester(tmp_path, sdk_fn=sdk_fn).run(207)
        assert len(s.edits) == 1
        # Prompt was seeded with the diff from the fixture.
        assert "commits" in captured["prompt"]

    def test_run_insufficient_context_raises(self, tmp_path: Path) -> None:
        gh_module = _fake_gh_module(body="", commits="", diff="", changed=[])

        def sdk_fn(prompt: str, **kwargs: Any) -> Any:  # pragma: no cover - must not run
            raise AssertionError("SDK must not be called on insufficient context")

        with pytest.raises(InsufficientContextError):
            self._suggester(tmp_path, sdk_fn=sdk_fn, gh_module=gh_module).run(404)

    def test_run_malformed_output_fails_closed(self, tmp_path: Path) -> None:
        def sdk_fn(prompt: str, **kwargs: Any) -> Any:
            return SimpleNamespace(last_message="not json", error=None, timed_out=False)

        with pytest.raises(SuggestionParseError):
            self._suggester(tmp_path, sdk_fn=sdk_fn).run(207)

    def test_run_sdk_timeout_fails_closed(self, tmp_path: Path) -> None:
        def sdk_fn(prompt: str, **kwargs: Any) -> Any:
            return SimpleNamespace(last_message="", error="timeout", timed_out=True)

        with pytest.raises(SuggestionParseError, match="timed out"):
            self._suggester(tmp_path, sdk_fn=sdk_fn).run(207)

    def test_run_sdk_error_fails_closed(self, tmp_path: Path) -> None:
        def sdk_fn(prompt: str, **kwargs: Any) -> Any:
            return SimpleNamespace(last_message="", error="boom", timed_out=False)

        with pytest.raises(SuggestionParseError, match="failed"):
            self._suggester(tmp_path, sdk_fn=sdk_fn).run(207)


class TestRenderSuggestionText:
    def test_renders_edits(self) -> None:
        text = render_suggestion_text(_sample_suggestion(), pr_number=207)
        assert "PR #207" in text
        assert "target=quality" in text
        assert "target=testing" in text

    def test_renders_empty(self) -> None:
        text = render_suggestion_text(ManifestoSuggestion(), pr_number=207)
        assert "no rule proposed" in text
