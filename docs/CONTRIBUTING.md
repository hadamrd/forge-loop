# Contributing to forge-loop

## Setup

```bash
uv sync --all-extras
uv run pre-commit install
```

## The four CI gates

CI strategy: **no GitHub Actions**. forge-loop is the dispatcher operators use for their own repos — so its CI is a forge-loop instance running against its own trunk. The four gates live in `Taskfile.yml` and operators wire them into whatever they use (Jenkins, Drone, forge-loop self-pipeline).

Run the full gate locally:
```bash
task ci
```

| Gate | Tool | What it catches |
|---|---|---|
| Format | `ruff format --check` | Style drift |
| Lint | `ruff check` | Common bugs (E/F/I/B/UP/SIM rules) |
| Type | `mypy` + `pyright` | Two checkers catch overlap holes (pydantic/SDK boundaries) |
| Tests | `pytest --cov-fail-under=70` | Behavioural regressions + coverage floor |

The same gates run locally on every commit via pre-commit. Run them ad-hoc:

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
uv run mypy src/forge_loop/
uv run pyright
uv run pytest tests/ --cov=src/forge_loop --cov-fail-under=70
```

## Type checking — current state

- **mypy** runs at the project's existing strictness in `pyproject.toml`. There are ~60 existing errors at full strict mode that will be fixed in a dedicated cascade follow-up — until then, mypy gates regressions in code that's already clean.
- **pyright** runs in `basic` mode. New code should pass cleanly; existing modules may surface warnings.

## Writing a PR

1. Branch off `origin/trunk` (never the local stale copy).
2. One concern per PR. If a refactor touches > 5 modules, split it.
3. Tests live alongside the change — unit in `tests/test_*.py`, property in `tests/property/`.
4. Don't lower the coverage floor. If a refactor genuinely needs to, justify it in the PR body.
5. Adversarial tests preferred — hunt the sad path, not the happy path.

## Reviewers

The manifesto-aware critic is the primary reviewer for forge-loop's style,
architecture, testing discipline, and project-specific quality rules.
CodeRabbit runs alongside it as a second-opinion reviewer for correctness,
security, runtime behavior, and Python API-contract issues that the manifesto
does not yet encode. When the two disagree, the manifesto wins on style and
architecture; CodeRabbit wins on correctness unless the critic cites a
documented manifesto rule.

## Architecture conventions

- Single source of config: `forge_loop.settings.Settings`. No new `os.environ.get("LOOP_*")` outside `settings.py` — the `test_no_loop_env_reads_outside_settings` regression test enforces this.
- Typed events: declare via `@register_event` in `forge_loop.events`. The loose `append_event(kind, **fields)` path still works but emits a `DeprecationWarning` for any registered kind.
- Adapters: `Container` bundles `git` / `fs` / `clock`. Production code accepts a Container arg; tests inject `FakeGitClient` / `FakeFileSystem` / `FakeClock`.
- Logging: `from forge_loop.log import get_logger`. Never `print()` in production code.
- GitHub: prefer `forge_loop.gh_client.GhClient` for new code. The legacy `forge_loop.gh` module is being migrated per-function in follow-ups.
