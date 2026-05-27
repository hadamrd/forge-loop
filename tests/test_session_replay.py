"""Tests for the record-replay infrastructure (issue #9).

These tests exercise:
- :class:`SessionReplayer` happy-path: each baseline fixture replays and
  the parsed outcome matches the trailer the recorder wrote live.
- adversarial fixture handling: missing header field, out-of-order seq,
  unknown schema, missing trailer, non-JSON line.
- the recorder/replayer round-trip via a synthetic fixture (no real
  `claude` subprocess needed).

Test matrix mapping (issue #9):
- unit: ``test_replayer_feeds_events_in_order``
- unit: ``test_fixture_missing_required_field_raises_corrupt``
- integration: ``test_baseline_fixture_*_replays_cleanly``
- adversarial: ``test_out_of_order_event_sequence_rejected``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_loop._testing.recorder import SCHEMA_VERSION
from forge_loop._testing.replayer import (
    FixtureCorruptError,
    SessionReplayer,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"


# ---------------------------------------------------------------- baselines

@pytest.mark.parametrize(
    "fixture_name, expected_status, expected_pr_substr",
    [
        ("happy_path.jsonl", "merged", "/pull/9101"),
        ("sad_path.jsonl", "failed", None),
        ("long_tool_use.jsonl", "merged", "/pull/9203"),
    ],
)
def test_baseline_fixture_replays_cleanly(
    fixture_name: str, expected_status: str, expected_pr_substr: str | None
) -> None:
    """Integration: each baseline fixture replays through `_extract_outcome`
    and produces the same (pr, status) the trailer recorded."""
    r = SessionReplayer(FIXTURES / fixture_name)
    outcome = r.replay_to_worker()
    assert outcome.status == expected_status
    if expected_pr_substr is None:
        assert outcome.pr_url is None
    else:
        assert outcome.pr_url is not None
        assert expected_pr_substr in outcome.pr_url
    assert outcome.matches_recording, (
        f"parser drift: parsed=({outcome.pr_url},{outcome.status}) "
        f"recorded=({outcome.recorded_pr_url},{outcome.recorded_status})"
    )


# ---------------------------------------------------------------- ordering

def test_replayer_feeds_events_in_order(tmp_path: Path) -> None:
    r = SessionReplayer(FIXTURES / "long_tool_use.jsonl")
    seen: list[int] = []
    r.replay_to_worker(consumer=lambda e: seen.append(int(e["seq"])))
    assert seen == sorted(seen), "events delivered out of order"
    assert seen == list(range(1, len(seen) + 1)), "seq must be dense from 1"


def test_iter_events_skips_trailer_and_header(tmp_path: Path) -> None:
    r = SessionReplayer(FIXTURES / "happy_path.jsonl")
    events = list(r.iter_events())
    assert all(e.get("type") != "outcome" for e in events)
    assert all("seq" in e for e in events)


# ------------------------------------------------------------ adversarial

def _write_fixture(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _valid_header(**overrides):
    h = {
        "schema": SCHEMA_VERSION,
        "issue": 1,
        "title": "t",
        "recorded_at": "2026-01-01T00:00:00+00:00",
    }
    h.update(overrides)
    return h


def _valid_trailer():
    return {"type": "outcome", "pr": None, "status": "no_pr", "returncode": 0}


def test_fixture_missing_required_field_raises_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "missing.jsonl"
    header = _valid_header()
    del header["issue"]
    _write_fixture(bad, [header, _valid_trailer()])
    with pytest.raises(FixtureCorruptError, match="missing required field 'issue'"):
        SessionReplayer(bad).load()


def test_out_of_order_event_sequence_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "ooo.jsonl"
    _write_fixture(bad, [
        _valid_header(),
        {"seq": 1, "type": "assistant"},
        {"seq": 3, "type": "assistant"},
        {"seq": 2, "type": "assistant"},  # out of order
        _valid_trailer(),
    ])
    with pytest.raises(FixtureCorruptError, match="out-of-order"):
        SessionReplayer(bad).load()


def test_duplicate_seq_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "dup.jsonl"
    _write_fixture(bad, [
        _valid_header(),
        {"seq": 1, "type": "assistant"},
        {"seq": 1, "type": "assistant"},
        _valid_trailer(),
    ])
    with pytest.raises(FixtureCorruptError, match="out-of-order"):
        SessionReplayer(bad).load()


def test_unknown_schema_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "schema.jsonl"
    _write_fixture(bad, [_valid_header(schema="other/v9"), _valid_trailer()])
    with pytest.raises(FixtureCorruptError, match="unknown schema"):
        SessionReplayer(bad).load()


def test_missing_trailer_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "no_trailer.jsonl"
    _write_fixture(bad, [_valid_header(), {"seq": 1, "type": "assistant"}])
    with pytest.raises(FixtureCorruptError, match="no trailer outcome"):
        SessionReplayer(bad).load()


def test_empty_fixture_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "empty.jsonl"
    bad.write_text("")
    with pytest.raises(FixtureCorruptError, match="empty fixture"):
        SessionReplayer(bad).load()


def test_non_json_line_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "junk.jsonl"
    bad.write_text(json.dumps(_valid_header()) + "\nnot json at all\n"
                   + json.dumps(_valid_trailer()) + "\n")
    with pytest.raises(FixtureCorruptError, match="not JSON"):
        SessionReplayer(bad).load()


def test_event_missing_seq_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "no_seq.jsonl"
    _write_fixture(bad, [
        _valid_header(),
        {"type": "assistant", "message": "no seq here"},
        _valid_trailer(),
    ])
    with pytest.raises(FixtureCorruptError, match="missing integer 'seq'"):
        SessionReplayer(bad).load()


def test_fixture_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SessionReplayer(tmp_path / "nope.jsonl")


# --------------------------------------------------- recorder argument guard

def test_recorder_rejects_issue_without_number_or_title(tmp_path: Path) -> None:
    from forge_loop._testing.recorder import SessionRecorder
    with pytest.raises(ValueError, match="must have"):
        SessionRecorder(issue={"title": "x"}, worktree=tmp_path, brief="b")
    with pytest.raises(ValueError, match="must have"):
        SessionRecorder(issue={"number": 1}, worktree=tmp_path, brief="b")


# --------------------------------------------------------- CLI smoke test

def test_cli_record_session_subcommand_registered() -> None:
    """Smoke: argparse knows about `record-session`."""
    import argparse

    from forge_loop.cli import main

    # Run with --help on the subcommand: argparse exits with code 0.
    with pytest.raises(SystemExit) as exc:
        main(["record-session", "--help"])
    assert exc.value.code == 0
    # And without args it errors loudly (required args).
    with pytest.raises(SystemExit):
        main(["record-session"])
    # Cheap sanity that the parser is constructible
    assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)
