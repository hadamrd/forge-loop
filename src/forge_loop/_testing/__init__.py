"""Test-only utilities for record/replay of Claude Agent SDK sessions.

This package is intentionally NOT imported by production code paths. It
exists so that integration tests can replay real subprocess interactions
(captured into fixtures by SessionRecorder) without re-running the
`claude` CLI in CI.
"""
