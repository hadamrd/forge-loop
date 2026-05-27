"""Tests for issue #34: PO dispatch threads ``--model`` into the CLI argv.

We assert directly on the assembled argv via the public ``_build_po_argv``
helper rather than poking subprocess internals — same surface the
production code uses to call ``claude -p``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from forge_loop import po


def test_build_po_argv_includes_model_when_set(tmp_path: Path) -> None:
    argv = po._build_po_argv("brief text", tmp_path, model="claude-opus-4-7")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-7"


def test_build_po_argv_omits_model_when_none(tmp_path: Path) -> None:
    argv = po._build_po_argv("brief text", tmp_path, model=None)
    assert "--model" not in argv


def test_expand_thin_specs_threads_model_through_to_subprocess(
    tmp_path: Path,
) -> None:
    """End-to-end of the argv contract: a thin candidate triggers _run_one,
    which calls subprocess.run with the assembled argv carrying --model."""
    captured: dict[str, object] = {}

    def fake_run(argv, **_kw):
        captured["argv"] = list(argv)
        return MagicMock(returncode=0)

    with (
        patch.object(po, "subprocess", MagicMock(run=fake_run, TimeoutExpired=Exception)),
        patch.object(po, "ensure_subagent_trusted", lambda *_a, **_k: None),
    ):
        po.expand_thin_specs(
            [{"number": 1, "title": "t", "body": "thin body"}],
            tmp_path,
            tmp_path / "logs",
            github_repo="o/r",
            timeout_s=10,
            brief_template="dummy",
            max_to_expand=1,
            model="claude-opus-4-7",
        )

    argv = captured["argv"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-7"


def test_expand_thin_specs_no_model_means_no_model_flag(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **_kw):
        captured["argv"] = list(argv)
        return MagicMock(returncode=0)

    with (
        patch.object(po, "subprocess", MagicMock(run=fake_run, TimeoutExpired=Exception)),
        patch.object(po, "ensure_subagent_trusted", lambda *_a, **_k: None),
    ):
        po.expand_thin_specs(
            [{"number": 1, "title": "t", "body": "thin"}],
            tmp_path,
            tmp_path / "logs",
            github_repo="o/r",
            timeout_s=10,
            brief_template="dummy",
            max_to_expand=1,
        )

    assert "--model" not in captured["argv"]


def test_expand_thin_specs_codex_provider_uses_codex_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from forge_loop import agent_backend

    captured: dict[str, object] = {}

    def fake_codex(**kwargs):
        captured.update(kwargs)
        return agent_backend.AgentRunResult(
            provider="codex",
            log_path=kwargs["log_path"],
            last_message='{"skipped": false, "reason": "expanded", "sections_added": ["Acceptance"]}',
            duration_s=0.5,
        )

    monkeypatch.setattr(agent_backend, "run_codex_exec", fake_codex)
    with patch.object(po, "ensure_subagent_trusted", lambda *_a, **_k: None):
        outcomes = po.expand_thin_specs(
            [{"number": 1, "title": "t", "body": "thin"}],
            tmp_path,
            tmp_path / "logs",
            github_repo="o/r",
            timeout_s=10,
            brief_template="dummy",
            max_to_expand=1,
            provider="codex",
            model="gpt-5-codex",
        )

    assert captured["model"] == "gpt-5-codex"
    assert outcomes[0].sections_added == ["Acceptance"]
