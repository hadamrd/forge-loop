"""Tests for `forge-loop brainstorm` (issue #124).

Covers the full acceptance matrix from issue #124:
  * dry-run (no flags) prints YAML, never touches GitHub.
  * `--apply` files epics first, threads epic#s into ticket bodies.
  * Label contract: `axis:<name>` + `epic` for epics; `axis:<name>` +
    `loop:ready` for tickets.
  * Missing / invalid ProductVision → exit 2.
  * Partial failure during `--apply` → exit 1 with per-title reporting.
  * Empty BrainstormReport → exit 0, zero `create_issue` calls.

All tests use `typer.testing.CliRunner` against a real `.forge/` scaffold
in a tmp_path and a `MockGhClient` from `forge_loop.gh_client`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from forge_loop import cli
from forge_loop._testing.memory_store import FakeMemoryStore
from forge_loop.brainstormer import BrainstormReport, ProposedEpic, ProposedTicket
from forge_loop.frontier.decisions import FrontierDecisionLedger, FrontierDecisionOutcome
from forge_loop.gh_client import GhError, Issue, MockGhClient
from forge_loop.memory.models import REJECTED_PATH_TAG, axis_from_tags

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    except TypeError:
        return CliRunner()


def _write_vision(repo: Path, *, axes: list[dict[str, Any]] | None = None) -> None:
    forge = repo / ".forge"
    forge.mkdir(parents=True, exist_ok=True)
    (forge / "product-vision.md").write_text("# Vision\n\nBuild the loop.\n", encoding="utf-8")
    if axes is None:
        axes = [
            {
                "name": "billing",
                "customer": "operator",
                "valuable_means": "operator can bill",
                "acceptable_work": ["payment integration"],
                "rejected_as_cosmetic": [],
            }
        ]
    (forge / "axes.yaml").write_text(yaml.safe_dump({"axes": axes}), encoding="utf-8")


@pytest.fixture
def cwd_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Switch into a tmp dir with a valid `.forge/` scaffold."""
    _write_vision(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Stub `cli.load()` so brainstorm CLI gets a stable (owner, repo) +
    # repo_path without depending on the operator's environment / cached
    # pydantic-settings.
    from types import SimpleNamespace as _NS

    monkeypatch.setattr(cli, "load", lambda: _NS(repo=tmp_path, github_repo="acme/widgets"))
    return tmp_path


class _StubBrainstormer:
    def __init__(self, report: BrainstormReport) -> None:
        self._report = report
        self.calls = 0

    def run(self, _vision: Any) -> BrainstormReport:
        self.calls += 1
        return self._report


def _install_stub(
    monkeypatch: pytest.MonkeyPatch,
    report: BrainstormReport,
    mock_gh: MockGhClient | None = None,
) -> tuple[_StubBrainstormer, MockGhClient]:
    stub = _StubBrainstormer(report)
    gh = mock_gh or MockGhClient()
    monkeypatch.setattr(cli, "_brainstormer_factory", lambda *a, **k: stub)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)
    return stub, gh


def _fixed_report() -> BrainstormReport:
    return BrainstormReport(
        proposed_epics=[
            ProposedEpic(
                title="Billing epic",
                body="Enable payments",
                axis="billing",
                customer_story="Operator wants invoicing",
            ),
        ],
        proposed_tickets=[
            ProposedTicket(
                title="Wire Stripe SDK",
                body="Add stripe-python",
                axis="billing",
                customer_story="Operator wants invoicing",
            ),
            ProposedTicket(
                title="Add receipt endpoint",
                body="GET /receipts",
                axis="billing",
                customer_story="Operator wants receipts",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Dry-run path (no --apply)
# ---------------------------------------------------------------------------


def test_brainstorm_dry_run_prints_yaml(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _fixed_report()
    _, gh = _install_stub(monkeypatch, report)

    result = runner.invoke(cli.app, ["brainstorm"])

    assert result.exit_code == 0, result.stdout + result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict)
    assert {e["title"] for e in parsed["proposed_epics"]} == {"Billing epic"}
    assert {t["title"] for t in parsed["proposed_tickets"]} == {
        "Wire Stripe SDK",
        "Add receipt endpoint",
    }
    # No GitHub calls in dry-run mode.
    assert not any(c[0] == "create_issue" for c in gh.calls)


def test_brainstorm_dry_run_writes_output_report(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _fixed_report()
    output = cwd_repo / "brainstorm-report.yaml"
    stub, gh = _install_stub(monkeypatch, report)

    result = runner.invoke(cli.app, ["brainstorm", "--output", str(output)])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert stub.calls == 1
    parsed = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert [e["title"] for e in parsed["proposed_epics"]] == ["Billing epic"]
    assert [t["title"] for t in parsed["proposed_tickets"]] == [
        "Wire Stripe SDK",
        "Add receipt endpoint",
    ]
    assert not any(c[0] == "create_issue" for c in gh.calls)


def test_brainstorm_factory_receives_po_provider_config(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    report = BrainstormReport(proposed_epics=[], proposed_tickets=[])

    def factory(*args: Any, **kwargs: Any) -> _StubBrainstormer:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _StubBrainstormer(report)

    from types import SimpleNamespace as _NS

    monkeypatch.setattr(
        cli,
        "load",
        lambda: _NS(
            repo=cwd_repo,
            github_repo="acme/widgets",
            po=_NS(provider="codex", model="gpt-5-codex", timeout_s=123),
        ),
    )
    monkeypatch.setattr(cli, "_brainstormer_factory", factory)

    result = runner.invoke(cli.app, ["brainstorm"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["kwargs"]["provider"] == "codex"
    assert captured["kwargs"]["model"] == "gpt-5-codex"
    assert captured["kwargs"]["timeout_s"] == 123


# ---------------------------------------------------------------------------
# --apply path
# ---------------------------------------------------------------------------


def test_brainstorm_apply_files_epics_first(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Epic must be filed before tickets, and ticket bodies must cite the
    just-returned epic number with ``Parent: #<n>``."""
    gh = MockGhClient(create_issue_responses=[501, 502, 503])
    report = _fixed_report()
    _install_stub(monkeypatch, report, mock_gh=gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply"])

    assert result.exit_code == 0, result.stdout + result.stderr
    create_calls = [c for c in gh.calls if c[0] == "create_issue"]
    # Order: epic (501), ticket1 (502), ticket2 (503).
    assert [c[1]["title"] for c in create_calls] == [
        "Billing epic",
        "Wire Stripe SDK",
        "Add receipt endpoint",
    ]
    # Both ticket bodies cross-link to the epic.
    assert "Parent: #501" in create_calls[1][1]["body"]
    assert "Parent: #501" in create_calls[2][1]["body"]


def test_brainstorm_apply_validates_github_auth_before_generating_proposals(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = MockGhClient(raise_on={"check_auth": GhError("check_auth", 401, "bad credentials")})

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("brainstormer SDK must not run when GitHub auth is invalid")

    monkeypatch.setattr(cli, "_brainstormer_factory", _explode)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply"])

    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert "github auth check failed" in combined.lower()
    assert "check_auth" in combined
    assert "bad credentials" in combined
    assert "via mock" in combined.lower()
    assert not any(c[0] == "create_issue" for c in gh.calls)


def test_brainstorm_apply_report_files_exact_reviewed_items_without_sdk(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewed report application must be deterministic: no resampling."""
    report_path = cwd_repo / "reviewed.yaml"
    report_path.write_text(
        yaml.safe_dump(_fixed_report().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    gh = MockGhClient(create_issue_responses=[901, 902, 903])

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("brainstormer SDK must not run when applying a report")

    monkeypatch.setattr(cli, "_brainstormer_factory", _explode)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply", "--report", str(report_path)])

    assert result.exit_code == 0, result.stdout + result.stderr
    create_calls = [c for c in gh.calls if c[0] == "create_issue"]
    assert [(c[1]["title"], c[1]["body"]) for c in create_calls] == [
        (
            "Billing epic",
            "Enable payments\n\n\n## Customer story\n\nOperator wants invoicing",
        ),
        (
            "Wire Stripe SDK",
            "Parent: #901\n\nAdd stripe-python\n\n\n## Customer story\n\nOperator wants invoicing",
        ),
        (
            "Add receipt endpoint",
            "Parent: #901\n\nGET /receipts\n\n\n## Customer story\n\nOperator wants receipts",
        ),
    ]
    decisions = FrontierDecisionLedger(cwd_repo / ".forge" / "frontier-decisions.yaml").list()
    assert [(d.proposal_title, d.outcome, d.issue_number) for d in decisions] == [
        ("Billing epic", FrontierDecisionOutcome.ACCEPTED, 901),
        ("Wire Stripe SDK", FrontierDecisionOutcome.ACCEPTED, 902),
        ("Add receipt endpoint", FrontierDecisionOutcome.ACCEPTED, 903),
    ]


def test_brainstorm_apply_report_revalidates_axes_and_duplicates(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = BrainstormReport(
        proposed_epics=[],
        proposed_tickets=[
            ProposedTicket(
                title="Wire Stripe SDK",
                body="Already exists",
                axis="billing",
                customer_story="Operator wants invoicing",
            ),
            ProposedTicket(
                title="Bogus axis",
                body="Must be filtered",
                axis="missing",
                customer_story="Operator wants invoicing",
            ),
            ProposedTicket(
                title="File deterministic report",
                body="Fresh item",
                axis="billing",
                customer_story="Operator wants deterministic backlog filing",
            ),
        ],
    )
    report_path = cwd_repo / "reviewed.yaml"
    report_path.write_text(
        yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    gh = MockGhClient(
        create_issue_responses=[777],
        issues_by_label_response=[Issue(number=42, title="Wire Stripe SDK")],
    )

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("brainstormer SDK must not run when applying a report")

    monkeypatch.setattr(cli, "_brainstormer_factory", _explode)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply", "--report", str(report_path)])

    assert result.exit_code == 0, result.stdout + result.stderr
    create_calls = [c for c in gh.calls if c[0] == "create_issue"]
    assert [c[1]["title"] for c in create_calls] == ["File deterministic report"]
    assert "dropped" in result.stdout.lower()
    decisions = FrontierDecisionLedger(cwd_repo / ".forge" / "frontier-decisions.yaml").list()
    assert [
        (d.proposal_title, d.outcome, d.issue_number, d.duplicate_of_issue) for d in decisions
    ] == [
        ("Wire Stripe SDK", FrontierDecisionOutcome.REJECTED, None, 42),
        ("File deterministic report", FrontierDecisionOutcome.ACCEPTED, 777, None),
    ]


def test_brainstorm_apply_report_backlog_scan_failure_exits_1(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_path = cwd_repo / "reviewed.yaml"
    report_path.write_text(
        yaml.safe_dump(_fixed_report().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    gh = MockGhClient(
        create_issue_responses=[901, 902, 903],
        raise_on={"issues_by_label": GhError("issues_by_label", 401, "unauthorized")},
    )

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("brainstormer SDK must not run when applying a report")

    monkeypatch.setattr(cli, "_brainstormer_factory", _explode)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply", "--report", str(report_path)])

    assert result.exit_code == 1
    assert "failed to scan open backlog" in (result.stderr + result.stdout).lower()
    assert not any(c[0] == "create_issue" for c in gh.calls)


def test_brainstorm_report_without_apply_exits_2(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(monkeypatch, _fixed_report())
    result = runner.invoke(cli.app, ["brainstorm", "--report", "reviewed.yaml"])
    assert result.exit_code == 2
    assert "--report requires --apply" in (result.stderr + result.stdout)


def test_brainstorm_apply_labels_epic(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = MockGhClient(create_issue_responses=[10, 11, 12])
    _install_stub(monkeypatch, _fixed_report(), mock_gh=gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply"])
    assert result.exit_code == 0
    create_calls = [c for c in gh.calls if c[0] == "create_issue"]
    epic_labels = create_calls[0][1]["labels"]
    assert "axis:billing" in epic_labels
    assert "epic" in epic_labels


def test_brainstorm_apply_labels_ticket(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = MockGhClient(create_issue_responses=[20, 21, 22])
    _install_stub(monkeypatch, _fixed_report(), mock_gh=gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply"])
    assert result.exit_code == 0
    create_calls = [c for c in gh.calls if c[0] == "create_issue"]
    ticket_labels = create_calls[1][1]["labels"]
    assert "axis:billing" in ticket_labels
    assert "loop:ready" in ticket_labels
    assert "epic" not in ticket_labels


# ---------------------------------------------------------------------------
# Sad paths — vision discovery
# ---------------------------------------------------------------------------


def test_brainstorm_missing_vision_exits_2(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `.forge/` at all → exit 2; brainstormer/gh are never invoked."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOOP_GH_REPO", "acme/widgets")
    gh = MockGhClient()

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("brainstormer must not run on missing vision")

    monkeypatch.setattr(cli, "_brainstormer_factory", _explode)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "vision" in (result.stderr + result.stdout).lower()
    assert not any(c[0] == "create_issue" for c in gh.calls)


def test_brainstorm_invalid_vision_exits_2(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty axes list → schema-invalid → exit 2."""
    forge = tmp_path / ".forge"
    forge.mkdir()
    (forge / "product-vision.md").write_text("# Vision\n\nx\n")
    (forge / "axes.yaml").write_text(yaml.safe_dump({"axes": []}))
    monkeypatch.chdir(tmp_path)
    gh = MockGhClient()
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm"])
    assert result.exit_code == 2
    assert not any(c[0] == "create_issue" for c in gh.calls)


# ---------------------------------------------------------------------------
# Adversarial — partial failure on --apply
# ---------------------------------------------------------------------------


def test_brainstorm_partial_failure_exits_1(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the 2nd ticket's create_issue raises, the epic + first ticket
    are still filed (no rollback), exit code is 1, stdout reports the
    successes and stderr reports the failing title + error."""
    gh = MockGhClient(create_issue_responses=[700, 701])
    gh.raise_on_create_titles = {
        "Add receipt endpoint": GhError("create_issue", 422, "rate-limited"),
    }
    _install_stub(monkeypatch, _fixed_report(), mock_gh=gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply"])
    assert result.exit_code == 1, result.stdout + result.stderr
    # Epic + first ticket DID get filed.
    titles_created = [c[1]["title"] for c in gh.calls if c[0] == "create_issue"]
    assert "Billing epic" in titles_created
    assert "Wire Stripe SDK" in titles_created
    # Reporting: successes on stdout, failure on stderr.
    assert "#700" in result.stdout and "Billing epic" in result.stdout
    assert "#701" in result.stdout and "Wire Stripe SDK" in result.stdout
    assert "Add receipt endpoint" in result.stderr
    assert "rate-limited" in result.stderr or "422" in result.stderr


def test_brainstorm_apply_report_failure_does_not_record_accepted_decision(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = BrainstormReport(
        proposed_epics=[],
        proposed_tickets=[
            ProposedTicket(
                title="Create failing proposal",
                body="Failure should not be accepted",
                axis="billing",
                customer_story="Operator wants trustworthy decision state",
            ),
        ],
    )
    report_path = cwd_repo / "reviewed.yaml"
    report_path.write_text(
        yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    gh = MockGhClient()
    gh.raise_on_create_titles = {
        "Create failing proposal": GhError("create_issue", 500, "server error"),
    }

    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("brainstormer SDK must not run when applying a report")

    monkeypatch.setattr(cli, "_brainstormer_factory", _explode)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply", "--report", str(report_path)])

    assert result.exit_code == 1
    decisions = FrontierDecisionLedger(cwd_repo / ".forge" / "frontier-decisions.yaml").list()
    assert decisions == ()


def test_brainstorm_apply_no_proposals(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = MockGhClient()
    empty = BrainstormReport(proposed_epics=[], proposed_tickets=[])
    _install_stub(monkeypatch, empty, mock_gh=gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply"])
    assert result.exit_code == 0
    assert not any(c[0] == "create_issue" for c in gh.calls)
    assert "no proposals" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Default safety — --apply is opt-in
# ---------------------------------------------------------------------------


def test_brainstorm_default_never_writes(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 manifesto: explicit adversarial test that the opt-in flag is
    NOT default. A regression that flips the default would file issues
    against a real GitHub repo without operator consent."""
    gh = MockGhClient()
    _install_stub(monkeypatch, _fixed_report(), mock_gh=gh)

    result = runner.invoke(cli.app, ["brainstorm"])
    assert result.exit_code == 0
    # Confirm zero write-shaped calls of ANY kind.
    write_methods = {"create_issue", "add_comment", "add_labels", "remove_label"}
    assert not any(c[0] in write_methods for c in gh.calls)


# ---------------------------------------------------------------------------
# Rejected-path / decision memory provenance (issue #203)
# ---------------------------------------------------------------------------


def _install_fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeMemoryStore:
    store = FakeMemoryStore()
    monkeypatch.setattr(cli, "_memory_store_factory", lambda _repo_path: store)
    return store


def _write_report(repo: Path, report: BrainstormReport) -> Path:
    path = repo / "reviewed.yaml"
    path.write_text(
        yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return path


def _explode_brainstormer(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("brainstormer SDK must not run when applying a report")

    monkeypatch.setattr(cli, "_brainstormer_factory", _explode)


def _dup_report() -> BrainstormReport:
    return BrainstormReport(
        proposed_epics=[],
        proposed_tickets=[
            ProposedTicket(
                title="Wire Stripe SDK",
                body="Already exists",
                axis="billing",
                customer_story="Operator wants invoicing",
            ),
            ProposedTicket(
                title="File deterministic report",
                body="Fresh item",
                axis="billing",
                customer_story="Operator wants deterministic backlog filing",
            ),
        ],
    )


def test_apply_report_writes_reject_and_accept_memory(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate drop writes a REJECTED_PATH_TAG item with axis+rationale
    provenance; an accepted item writes a durable decision record carrying the
    filed issue number and axis."""
    store = _install_fake_store(monkeypatch)
    report_path = _write_report(cwd_repo, _dup_report())
    gh = MockGhClient(
        create_issue_responses=[777],
        issues_by_label_response=[Issue(number=42, title="Wire Stripe SDK")],
    )
    _explode_brainstormer(monkeypatch)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    result = runner.invoke(cli.app, ["brainstorm", "--apply", "--report", str(report_path)])
    assert result.exit_code == 0, result.stdout + result.stderr

    rejected = store.list_rejected_paths()
    assert [item.title for item in rejected] == ["Wire Stripe SDK"]
    rej = rejected[0]
    assert REJECTED_PATH_TAG in rej.tags
    assert axis_from_tags(rej.tags) == "billing"
    assert "billing" in rej.body and "rejected" in rej.body.lower()
    assert rej.provenance.authored_by == "brainstorm-apply"
    assert rej.provenance.source_task_ref  # non-empty when no source event

    active = store.list_active()
    accepted = [i for i in active if REJECTED_PATH_TAG not in i.tags]
    assert [i.title for i in accepted] == ["File deterministic report"]
    assert "#777" in accepted[0].body
    assert axis_from_tags(accepted[0].tags) == "billing"

    # Ledger still records both verdicts — memory is an additive sink.
    decisions = FrontierDecisionLedger(cwd_repo / ".forge" / "frontier-decisions.yaml").list()
    assert {(d.proposal_title, d.outcome) for d in decisions} == {
        ("Wire Stripe SDK", FrontierDecisionOutcome.REJECTED),
        ("File deterministic report", FrontierDecisionOutcome.ACCEPTED),
    }


def test_apply_report_twice_is_idempotent_for_memory(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running apply over the same report (same source_report_hash) must not
    duplicate memory items — memory_id is derived from the stable source key."""
    store = _install_fake_store(monkeypatch)
    report_path = _write_report(cwd_repo, _dup_report())
    _explode_brainstormer(monkeypatch)

    def _run() -> None:
        gh = MockGhClient(
            create_issue_responses=[777],
            issues_by_label_response=[Issue(number=42, title="Wire Stripe SDK")],
        )
        monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)
        res = runner.invoke(cli.app, ["brainstorm", "--apply", "--report", str(report_path)])
        assert res.exit_code == 0, res.stdout + res.stderr

    _run()
    rejected_after_first = len(store.list_rejected_paths())
    active_after_first = len(store.list_active())
    _run()

    assert len(store.list_rejected_paths()) == rejected_after_first == 1
    assert len(store.list_active()) == active_after_first  # 1 rejected + 1 accepted = 2


def test_two_run_loop_rejected_path_feeds_back_and_filters(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run A rejects X (duplicate) → persisted to the shared store. Run B is a
    fresh generation wired to the SAME store: X is rendered into the prompt's
    rejected-path block AND filtered before output if the SDK re-proposes it."""
    store = _install_fake_store(monkeypatch)

    # --- Run A: report-apply rejects "Wire Stripe SDK" as a duplicate. ---
    report_path = _write_report(cwd_repo, _dup_report())
    gh_a = MockGhClient(
        create_issue_responses=[777],
        issues_by_label_response=[Issue(number=42, title="Wire Stripe SDK")],
    )
    _explode_brainstormer(monkeypatch)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh_a)
    res_a = runner.invoke(cli.app, ["brainstorm", "--apply", "--report", str(report_path)])
    assert res_a.exit_code == 0, res_a.stdout + res_a.stderr
    assert [i.title for i in store.list_rejected_paths()] == ["Wire Stripe SDK"]

    # --- Run B: fresh generation wired to the SAME store, SDK re-proposes X. ---
    import json as _json
    from dataclasses import dataclass

    from forge_loop.brainstormer import Brainstormer

    captured: dict[str, Any] = {}

    @dataclass
    class _SdkResult:
        last_message: str
        timed_out: bool = False
        error: Any = None

    # Re-propose the already-rejected idea (case/space mangled) + a new one.
    _payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "  wire   stripe sdk ",
                "body": "x",
                "axis": "billing",
                "customer_story": "Operator wants invoicing",
            },
            {
                "title": "A brand new idea",
                "body": "x",
                "axis": "billing",
                "customer_story": "Operator wants something new",
            },
        ],
    }

    def _sdk(prompt: str, *, cwd: Any, timeout_s: int, model: Any = None, **_kw: Any) -> Any:
        captured["prompt"] = prompt
        return _SdkResult(last_message=_json.dumps(_payload))

    def _factory(*_a: Any, **_k: Any) -> Brainstormer:
        return Brainstormer(sdk_fn=_sdk, memory_store=store)

    monkeypatch.setattr(cli, "_brainstormer_factory", _factory)

    res_b = runner.invoke(cli.app, ["brainstorm"])
    assert res_b.exit_code == 0, res_b.stdout + res_b.stderr
    parsed = yaml.safe_load(res_b.stdout)
    titles = [t["title"] for t in parsed["proposed_tickets"]]
    # X filtered (whitespace/case-insensitive match); only the new idea survives.
    assert titles == ["A brand new idea"]
    # The prompt fed to run B's session cited the rejected path.
    assert "Wire Stripe SDK" in captured["prompt"]
    assert "Previously rejected paths" in captured["prompt"]
