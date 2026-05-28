"""Tests for worker manifesto injection (issue #132).

Covers the acceptance criteria's test matrix:

- Both manifestos present → both headers + bodies in order.
- Both missing → byte-identical pre-feature baseline (no MANIFESTO block).
- Only one side present → that subsection only, no empty header.
- Whitespace-only file → treated as missing.
- ``manifesto_sha`` recorded on the WorkerOutcome / telemetry.
- Block precedes any task-specific brief markers.
- Integration: SDK system prompt + outcome both round-trip the bundle.
- Adversarial: unreadable/binary manifesto does NOT crash the worker.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from forge_loop.manifestos import (
    BLOCK_CLOSE,
    BLOCK_OPEN,
    QUALITY_HEADER,
    QUALITY_REL,
    TESTING_HEADER,
    TESTING_REL,
    ManifestoBundle,
    ManifestoSide,
    inject_into_brief,
    load_manifestos,
    render_manifesto_block,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_manifesto(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_blob_sha(text: str) -> str:
    blob = text.encode("utf-8")
    h = hashlib.sha1()
    h.update(f"blob {len(blob)}\0".encode("ascii"))
    h.update(blob)
    return h.hexdigest()


@pytest.fixture
def issue() -> dict[str, Any]:
    return {"number": 999, "title": "test ticket", "body": "do the thing"}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_load_manifestos_empty_repo_returns_empty_bundle(tmp_path: Path) -> None:
    bundle = load_manifestos(tmp_path)
    assert not bundle.any_present
    assert bundle.quality == ManifestoSide(None, None)
    assert bundle.testing == ManifestoSide(None, None)
    assert bundle.sha_payload() == {"quality": None, "testing": None}


def test_load_manifestos_both_present(tmp_path: Path) -> None:
    _write_manifesto(tmp_path, QUALITY_REL, "be excellent")
    _write_manifesto(tmp_path, TESTING_REL, "test everything")
    bundle = load_manifestos(tmp_path)
    assert bundle.quality.content == "be excellent"
    assert bundle.testing.content == "test everything"
    assert bundle.quality.sha == _git_blob_sha("be excellent")
    assert bundle.testing.sha == _git_blob_sha("test everything")


def test_whitespace_only_manifesto_treated_as_missing(tmp_path: Path) -> None:
    _write_manifesto(tmp_path, QUALITY_REL, "\n\n   \n")
    bundle = load_manifestos(tmp_path)
    assert bundle.quality.content is None
    assert bundle.quality.sha is None


def test_corrupt_manifesto_does_not_crash_loader(tmp_path: Path) -> None:
    # Binary garbage that's invalid utf-8.
    bad_path = tmp_path / QUALITY_REL
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(b"\xff\xfe\x00\x01garbage\xc3\x28")
    bundle = load_manifestos(tmp_path)
    assert bundle.quality.content is None
    assert bundle.quality.sha is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_renders_both_manifestos_when_present(tmp_path: Path) -> None:
    _write_manifesto(tmp_path, QUALITY_REL, "Q1\nQ2")
    _write_manifesto(tmp_path, TESTING_REL, "T1\nT2")
    bundle = load_manifestos(tmp_path)
    block = render_manifesto_block(bundle)
    # Both headers present.
    assert QUALITY_HEADER in block
    assert TESTING_HEADER in block
    # Quality precedes testing.
    assert block.index(QUALITY_HEADER) < block.index(TESTING_HEADER)
    # Bodies verbatim.
    assert "Q1\nQ2" in block
    assert "T1\nT2" in block
    # Sentinel markers wrap.
    assert block.startswith(BLOCK_OPEN)
    assert BLOCK_CLOSE in block


def test_silent_skip_when_both_missing(tmp_path: Path, issue: dict[str, Any]) -> None:
    """Pre-feature baseline: empty bundle → brief is byte-identical."""
    from forge_loop.worker import make_brief

    bundle_empty = load_manifestos(tmp_path)
    baseline = make_brief(issue, tmp_path)
    with_bundle = make_brief(issue, tmp_path, manifesto_bundle=bundle_empty)
    assert baseline == with_bundle
    assert BLOCK_OPEN not in with_bundle


def test_partial_render_only_quality(tmp_path: Path) -> None:
    _write_manifesto(tmp_path, QUALITY_REL, "only quality here")
    bundle = load_manifestos(tmp_path)
    block = render_manifesto_block(bundle)
    assert QUALITY_HEADER in block
    assert TESTING_HEADER not in block
    assert "only quality here" in block


def test_partial_render_only_testing(tmp_path: Path) -> None:
    _write_manifesto(tmp_path, TESTING_REL, "only testing here")
    bundle = load_manifestos(tmp_path)
    block = render_manifesto_block(bundle)
    assert TESTING_HEADER in block
    assert QUALITY_HEADER not in block


def test_whitespace_only_manifesto_not_rendered_as_empty_header(tmp_path: Path) -> None:
    _write_manifesto(tmp_path, QUALITY_REL, "\n   \n")
    _write_manifesto(tmp_path, TESTING_REL, "real testing rules")
    bundle = load_manifestos(tmp_path)
    block = render_manifesto_block(bundle)
    assert QUALITY_HEADER not in block
    assert TESTING_HEADER in block


# ---------------------------------------------------------------------------
# Injection into worker brief
# ---------------------------------------------------------------------------


def test_manifesto_block_precedes_task_brief(tmp_path: Path, issue: dict[str, Any]) -> None:
    from forge_loop.worker import make_brief

    _write_manifesto(tmp_path, QUALITY_REL, "QQ rules")
    _write_manifesto(tmp_path, TESTING_REL, "TT rules")
    bundle = load_manifestos(tmp_path)
    brief = make_brief(issue, tmp_path, manifesto_bundle=bundle)
    # The MANIFESTO block must appear at offset 0 (or near zero) — before
    # the "You are an autonomous worker" task header.
    task_marker = "autonomous worker"
    assert task_marker in brief
    assert brief.index(BLOCK_OPEN) < brief.index(task_marker)
    assert brief.index(QUALITY_HEADER) < brief.index(task_marker)
    assert brief.index(TESTING_HEADER) < brief.index(task_marker)


def test_inject_no_op_when_bundle_empty() -> None:
    empty = ManifestoBundle(ManifestoSide(None, None), ManifestoSide(None, None))
    assert inject_into_brief("hello world", empty) == "hello world"


# ---------------------------------------------------------------------------
# Telemetry (manifesto_sha on WorkerOutcome)
# ---------------------------------------------------------------------------


def test_manifesto_sha_in_telemetry_both_present(
    tmp_path: Path, issue: dict[str, Any]
) -> None:
    """End-to-end: run_worker records manifesto_sha on the outcome.

    Uses a fake _prep_worktree + _run_worker_sdk so we exercise the wiring
    without launching the real SDK or git.
    """
    from forge_loop import worker as worker_mod
    from forge_loop.worker import WorkerOutcome

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_manifesto(repo, QUALITY_REL, "QUAL")
    _write_manifesto(repo, TESTING_REL, "TEST")
    logs = tmp_path / "logs"

    fake_outcome = WorkerOutcome(
        issue=999, title="t", pr_url=None, status="open",
        duration_s=0.1, stdout_tail="", error=None,
    )
    captured: dict[str, Any] = {}

    def _fake_prep(*a: Any, **k: Any) -> tuple[Path, str | None]:
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        return wt, None

    def _fake_sdk(**kwargs: Any) -> WorkerOutcome:
        captured["brief"] = kwargs["brief"]
        return fake_outcome

    with patch.object(worker_mod, "_prep_worktree", _fake_prep), patch.object(
        worker_mod, "_run_worker_sdk", _fake_sdk
    ):
        outcome = worker_mod.run_worker(issue, repo, logs, timeout_s=10)

    assert outcome.manifesto_sha == {
        "quality": _git_blob_sha("QUAL"),
        "testing": _git_blob_sha("TEST"),
    }
    # Brief that was sent to the SDK must contain the rendered block.
    assert BLOCK_OPEN in captured["brief"]
    assert QUALITY_HEADER in captured["brief"]
    assert TESTING_HEADER in captured["brief"]


def test_manifesto_sha_null_when_repo_has_no_manifestos(
    tmp_path: Path, issue: dict[str, Any]
) -> None:
    from forge_loop import worker as worker_mod
    from forge_loop.worker import WorkerOutcome

    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    fake_outcome = WorkerOutcome(
        issue=999, title="t", pr_url=None, status="open",
        duration_s=0.1, stdout_tail="", error=None,
    )
    captured: dict[str, Any] = {}

    def _fake_prep(*a: Any, **k: Any) -> tuple[Path, str | None]:
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        return wt, None

    def _fake_sdk(**kwargs: Any) -> WorkerOutcome:
        captured["brief"] = kwargs["brief"]
        return fake_outcome

    with patch.object(worker_mod, "_prep_worktree", _fake_prep), patch.object(
        worker_mod, "_run_worker_sdk", _fake_sdk
    ):
        outcome = worker_mod.run_worker(issue, repo, logs, timeout_s=10)

    assert outcome.manifesto_sha is None
    assert BLOCK_OPEN not in captured["brief"]


def test_manifesto_sha_partial_when_only_one_side_present(
    tmp_path: Path, issue: dict[str, Any]
) -> None:
    from forge_loop import worker as worker_mod
    from forge_loop.worker import WorkerOutcome

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_manifesto(repo, QUALITY_REL, "only quality")
    logs = tmp_path / "logs"
    fake_outcome = WorkerOutcome(
        issue=999, title="t", pr_url=None, status="open",
        duration_s=0.1, stdout_tail="", error=None,
    )

    def _fake_prep(*a: Any, **k: Any) -> tuple[Path, str | None]:
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        return wt, None

    def _fake_sdk(**kwargs: Any) -> WorkerOutcome:
        return fake_outcome

    with patch.object(worker_mod, "_prep_worktree", _fake_prep), patch.object(
        worker_mod, "_run_worker_sdk", _fake_sdk
    ):
        outcome = worker_mod.run_worker(issue, repo, logs, timeout_s=10)

    assert outcome.manifesto_sha == {
        "quality": _git_blob_sha("only quality"),
        "testing": None,
    }


def test_corrupt_manifesto_does_not_crash_worker(
    tmp_path: Path, issue: dict[str, Any]
) -> None:
    """Adversarial: binary garbage in manifesto file → worker still runs."""
    from forge_loop import worker as worker_mod
    from forge_loop.worker import WorkerOutcome

    repo = tmp_path / "repo"
    repo.mkdir()
    bad = repo / QUALITY_REL
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\xff\xfe\x00garbage\xc3\x28")
    # Testing side is fine.
    _write_manifesto(repo, TESTING_REL, "valid testing")
    logs = tmp_path / "logs"
    fake_outcome = WorkerOutcome(
        issue=999, title="t", pr_url=None, status="open",
        duration_s=0.1, stdout_tail="", error=None,
    )

    def _fake_prep(*a: Any, **k: Any) -> tuple[Path, str | None]:
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        return wt, None

    def _fake_sdk(**kwargs: Any) -> WorkerOutcome:
        return fake_outcome

    with patch.object(worker_mod, "_prep_worktree", _fake_prep), patch.object(
        worker_mod, "_run_worker_sdk", _fake_sdk
    ):
        outcome = worker_mod.run_worker(issue, repo, logs, timeout_s=10)

    # Corrupt side → null sha; clean side → real sha.
    assert outcome.manifesto_sha == {
        "quality": None,
        "testing": _git_blob_sha("valid testing"),
    }


# ---------------------------------------------------------------------------
# Git blob sha parity
# ---------------------------------------------------------------------------


def test_sha_matches_git_hash_object(tmp_path: Path) -> None:
    """Our blob-sha implementation must match ``git hash-object`` byte-for-byte.

    Operators audit ``manifesto_sha`` by running ``git hash-object`` on the
    live file; a divergence would break that workflow.
    """
    if not _which("git"):  # pragma: no cover
        pytest.skip("git not available")
    content = "rule 1\nrule 2\n"
    path = tmp_path / "f.md"
    path.write_text(content, encoding="utf-8")
    expected = subprocess.check_output(
        ["git", "hash-object", str(path)], text=True
    ).strip()
    from forge_loop.manifestos import _git_blob_sha as impl

    assert impl(content) == expected


def _which(cmd: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / cmd
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None
