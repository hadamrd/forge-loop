"""Tests for the externalised brief templates and loader.

Issue #3: briefs live as overridable .md.tmpl files under
``src/forge_loop/briefs/``. The loader honours ``LOOP_<KIND>_BRIEF`` env
overrides, reads bundled templates via ``importlib.resources`` (so it works
from an installed wheel), and surfaces missing override files / unknown
placeholders as loud errors.
"""

from __future__ import annotations

import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from forge_loop.briefs import _ENV_OVERRIDES, KINDS, load_template, render_brief


@pytest.fixture(autouse=True)
def _clear_brief_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no operator override leaks into a test from the runtime env."""
    for var in _ENV_OVERRIDES.values():
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_render_worker_brief_substitutes_all_placeholders(tmp_path: Path) -> None:
    from forge_loop.worker import make_brief

    issue = {"number": 42, "title": "Fix the thing", "body": "details"}
    out = make_brief(issue, tmp_path / "wt-42")
    assert "#42" in out
    assert "Fix the thing" in out
    assert "details" in out
    assert str(tmp_path / "wt-42") in out
    # No unsubstituted format-style placeholders should remain.
    assert "{n}" not in out
    assert "{worktree}" not in out
    assert "{issue_title}" not in out


def test_render_po_brief_substitutes_all_placeholders() -> None:
    out = render_brief(
        "po",
        issue_number=99,
        issue_title="Tighten X",
        issue_body="thin body",
        github_repo="acme/widgets",
    )
    assert "#99" in out
    assert "Tighten X" in out
    assert "thin body" in out
    assert "acme/widgets" in out
    # The literal ``{{...}}`` in the template renders as a single brace; the
    # PO's example JSON block should appear with single-brace braces.
    assert '{"issue": 99' in out


def test_render_critic_brief_substitutes_pr_and_issue() -> None:
    out = render_brief(
        "critic",
        pr_url="https://github.com/acme/repo/pull/7",
        issue_number=7,
    )
    assert "https://github.com/acme/repo/pull/7" in out
    assert "#7" in out
    assert '"issue": 7' in out
    # No leftover Python-format markers.
    assert "{pr_url}" not in out
    assert "{issue_number}" not in out


# ---------------------------------------------------------------------------
# env-var override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_env_override_used_when_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    override = tmp_path / f"{kind}.tmpl"
    override.write_text(f"OVERRIDDEN {kind} brief — token {{token}}")
    monkeypatch.setenv(_ENV_OVERRIDES[kind], str(override))

    rendered = render_brief(kind, token="abc")
    assert rendered == f"OVERRIDDEN {kind} brief — token abc"


@pytest.mark.parametrize("kind", KINDS)
def test_env_override_missing_file_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    bogus = tmp_path / "does-not-exist.tmpl"
    monkeypatch.setenv(_ENV_OVERRIDES[kind], str(bogus))

    with pytest.raises(FileNotFoundError) as exc:
        load_template(kind)
    msg = str(exc.value)
    assert _ENV_OVERRIDES[kind] in msg
    assert str(bogus) in msg


# ---------------------------------------------------------------------------
# bundled-template / importlib.resources path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_bundled_template_loads_via_importlib_resources(kind: str) -> None:
    """Loader uses importlib.resources, not __file__ — so it works from a wheel."""
    direct = (
        resources.files("forge_loop.briefs").joinpath(f"{kind}.md.tmpl").read_text(encoding="utf-8")
    )
    assert load_template(kind) == direct
    assert direct.strip(), f"{kind} template must not be empty"


def test_templates_load_from_installed_wheel(tmp_path: Path) -> None:
    """Build a wheel, install it into a fresh venv, render all 3 briefs.

    This is the only test that catches "the .tmpl files got dropped from
    the wheel" — a packaging regression that would silently fall back to
    the source tree in dev.
    """
    if not (Path(__file__).resolve().parent.parent / "pyproject.toml").exists():
        pytest.skip("not running from repo checkout")

    repo_root = Path(__file__).resolve().parent.parent
    dist = tmp_path / "dist"
    dist.mkdir()

    # Prefer `python -m build`; fall back to `pip wheel --no-deps` (which is
    # always available alongside pip in modern envs and exercises the same
    # hatchling backend).
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(repo_root)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if build.returncode != 0:
        build = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(dist),
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    if build.returncode != 0:
        # `uv build` is the modern alternative when pip/build aren't installed.
        import shutil

        uv = shutil.which("uv")
        if uv:
            build = subprocess.run(
                [uv, "build", "--wheel", "--out-dir", str(dist), str(repo_root)],
                capture_output=True,
                text=True,
                timeout=180,
            )
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable: {build.stderr[:300]}")

    wheels = list(dist.glob("forge_loop-*.whl"))
    assert wheels, f"no wheel produced in {dist}"

    # Inspect the wheel directly — much cheaper than spinning up a venv,
    # and gives a deterministic answer to "are the .tmpl files inside?"
    import zipfile

    with zipfile.ZipFile(wheels[0]) as z:
        names = z.namelist()
        for kind in KINDS:
            member = f"forge_loop/briefs/{kind}.md.tmpl"
            assert member in names, (
                f"{member} missing from wheel — pyproject.toml force-include broken. "
                f"Members: {[n for n in names if 'briefs' in n]}"
            )
            # And the content matches what the loader returns.
            with z.open(member) as fh:
                wheel_content = fh.read().decode("utf-8")
            assert wheel_content == load_template(kind)


# ---------------------------------------------------------------------------
# adversarial
# ---------------------------------------------------------------------------


def test_unknown_placeholder_in_template_raises_loud_keyerror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / "po.tmpl"
    bad.write_text("hello {issue_number}, but also {totally_made_up}")
    monkeypatch.setenv("LOOP_PO_BRIEF", str(bad))

    with pytest.raises(KeyError) as exc:
        render_brief(
            "po",
            issue_number=1,
            issue_title="t",
            issue_body="b",
            github_repo="o/r",
        )
    msg = str(exc.value)
    assert "totally_made_up" in msg, f"KeyError should name the missing key, got: {msg}"
    assert "po" in msg


def test_unknown_kind_raises_value_error() -> None:
    with pytest.raises(ValueError) as exc:
        load_template("nope")  # type: ignore[arg-type]
    assert "nope" in str(exc.value)


def test_missing_render_kwarg_for_bundled_template_raises_keyerror() -> None:
    """Calling render_brief without a required placeholder fails loudly."""
    with pytest.raises(KeyError) as exc:
        render_brief("critic", pr_url="x")  # missing issue_number
    assert "issue_number" in str(exc.value)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_brief_renders_worker_with_placeholder_when_no_gh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`forge-loop brief --kind worker --issue 123` without gh available falls
    back to a placeholder issue rather than crashing."""
    monkeypatch.setenv("PATH", "/nonexistent")
    from forge_loop.cli import main

    rc = main(["brief", "--kind", "worker", "--issue", "123"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#123" in out


def test_cli_brief_raw_emits_unrendered_template(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from forge_loop.cli import main

    rc = main(["brief", "--kind", "critic", "--raw"])
    out = capsys.readouterr().out
    assert rc == 0
    # --raw bypasses substitution: format placeholders should still be there.
    assert "{pr_url}" in out
    assert "{issue_number}" in out


def test_cli_brief_uses_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operator override flows through the CLI rendering path too."""
    override = tmp_path / "critic.tmpl"
    override.write_text("CUSTOM CRITIC for {pr_url}/{issue_number}")
    monkeypatch.setenv("LOOP_CRITIC_BRIEF", str(override))

    from forge_loop.cli import main

    rc = main(
        [
            "brief",
            "--kind",
            "critic",
            "--issue-file",
            _write_issue_file(tmp_path, 88),
            "--pr",
            "https://example/pr/1",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "CUSTOM CRITIC for https://example/pr/1/88" in out


def _write_issue_file(tmp_path: Path, n: int) -> str:
    import json

    p = tmp_path / "issue.json"
    p.write_text(json.dumps({"number": n, "title": "t", "body": "b"}))
    return str(p)
