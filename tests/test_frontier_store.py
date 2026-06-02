from pathlib import Path

import pytest
import yaml

from forge_loop.frontier import FrontierCursor, FrontierStore, HotArtifact, RejectedPath


def test_frontier_store_round_trips_cursor_yaml(tmp_path: Path) -> None:
    path = tmp_path / ".forge" / "frontier.yaml"
    cursor = FrontierCursor(
        product_goal="make long-running agents recoverable",
        current_problem="frontier state is not durable",
        next_expansion="load a cursor during boot",
        why_now="maestro needs continuity after restart",
        active_decisions=("use yaml for operator-readable state",),
        rejected_paths=(
            RejectedPath(
                idea="store cursor in process memory",
                reason="lost on restart",
                revisit_if="runner becomes stateless",
            ),
        ),
        hot_files=(HotArtifact(ref="src/forge_loop/frontier/cursor.py", why_hot="cursor schema"),),
        hot_tests=(HotArtifact(ref="tests/test_frontier_store.py", why_hot="store coverage"),),
        open_questions=("how should event projections advance this later?",),
        external_sources=("docs/design/long-running-agent-architecture.md",),
        version=2,
    )

    store = FrontierStore(path)
    store.save(cursor)

    assert path.exists()
    assert store.load() == cursor


def test_frontier_store_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FrontierStore(tmp_path / "missing.yaml").load()


def test_frontier_store_load_missing_required_field_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "frontier.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "product_goal": "make agents durable",
                "next_expansion": "persist frontier cursor",
                "why_now": "boot needs context",
            }
        )
    )

    with pytest.raises(ValueError, match="current_problem"):
        FrontierStore(path).load()


def test_frontier_store_preserves_rejected_paths_and_hot_artifacts(tmp_path: Path) -> None:
    path = tmp_path / "frontier.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "product_goal": "make long-running agents recoverable",
                "current_problem": "frontier state is not durable",
                "next_expansion": "load a cursor during boot",
                "why_now": "maestro needs continuity after restart",
                "rejected_paths": [
                    {
                        "idea": "derive frontier from latest issue",
                        "reason": "issues are too narrow",
                        "revisit_if": "issues gain durable strategy metadata",
                    }
                ],
                "hot_files": [
                    {
                        "ref": "src/forge_loop/frontier/cursor.py",
                        "why_hot": "cursor schema",
                    }
                ],
                "hot_tests": [
                    {
                        "ref": "tests/test_frontier_store.py",
                        "why_hot": "store coverage",
                    }
                ],
            }
        )
    )

    cursor = FrontierStore(path).load()

    assert cursor.rejected_paths == (
        RejectedPath(
            idea="derive frontier from latest issue",
            reason="issues are too narrow",
            revisit_if="issues gain durable strategy metadata",
        ),
    )
    assert cursor.hot_files == (
        HotArtifact(ref="src/forge_loop/frontier/cursor.py", why_hot="cursor schema"),
    )
    assert cursor.hot_tests == (
        HotArtifact(ref="tests/test_frontier_store.py", why_hot="store coverage"),
    )
