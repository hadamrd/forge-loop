from __future__ import annotations

from pathlib import Path

from forge_loop.agent_backend import (
    build_codex_exec_argv,
    extract_github_pr,
    extract_last_json_object,
)


def test_build_codex_exec_argv_uses_stdin_prompt_and_last_message_file(tmp_path: Path) -> None:
    last = tmp_path / "last.txt"
    argv = build_codex_exec_argv(
        cwd=tmp_path,
        last_message_path=last,
        model="gpt-5-codex",
        add_dirs=[tmp_path / "extra"],
    )
    assert argv[:3] == ["codex", "exec", "-"]
    assert "--json" in argv
    assert ["-C", str(tmp_path)] == argv[argv.index("-C") : argv.index("-C") + 2]
    assert argv[argv.index("-m") : argv.index("-m") + 2] == ["-m", "gpt-5-codex"]
    assert str(last) in argv
    assert str(tmp_path / "extra") in argv


def test_extract_last_json_object_tolerates_prose() -> None:
    obj = extract_last_json_object(
        'first {"ignored": true}\nDone.\n{"issue": 1, "status": "merged"}'
    )
    assert obj == {"issue": 1, "status": "merged"}


def test_extract_github_pr_fallback() -> None:
    assert extract_github_pr("opened https://github.com/o/r/pull/42") == (
        "https://github.com/o/r/pull/42"
    )
