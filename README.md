# forge-loop

Autonomous multi-worker dispatcher for Claude Code — picks up GitHub issues
by label, dispatches parallel Claude workers in git worktrees, watches PRs,
merges, and redeploys.

## How it works

```
  label issues `loop:ready`
         │
         ▼
  ┌─────────────────────┐    every tick:
  │  forge-loop runner  │    - fetch ready issues
  └──────────┬──────────┘    - prep worktrees
             │               - dispatch N workers in parallel
   ┌─────────┴─────────┐
   ▼         ▼         ▼
 claude    claude    claude     each worker: read issue, write tests,
 worker    worker    worker     commit, push, open PR, auto-merge
   │         │         │
   └────┬────┴────┬────┘
        ▼         ▼
   record       trigger
   attempt      redeploy
```

Every Nth tick is a maintenance pass: a PM agent triages/retitles/dedupes
the backlog. Risk-gated issues skip auto-merge.

## Quickstart

```sh
# Install
git clone https://github.com/hadamrd/forge-loop.git
cd forge-loop
uv sync --extra dev

# Auth
gh auth login

# Configure (per project)
export LOOP_GH_REPO=owner/your-repo
cd /path/to/your-project
uv run --directory /path/to/forge-loop forge-loop init --create-labels

# Run
uv run --directory /path/to/forge-loop forge-loop run
```

Label any GitHub issue `loop:ready` and the loop will attack it on its
next tick.

## Configuration

Two layers, in priority order: env vars > YAML.

### Environment variables

| Var | Purpose |
|---|---|
| `LOOP_GH_REPO` | **Required**. `owner/repo` target for `gh` calls. |
| `LOOP_COAUTHOR` | Optional. If set, workers add `Co-Authored-By: <value>` to commits. |
| `LOOP_DEPLOY_TASK` | Optional. `task <name>` target invoked after merges. No-op if unset. |
| `LOOP_PARALLEL` | Workers per tick (default 3). |
| `LOOP_TICK_INTERVAL_S` | Seconds between ticks (default 60). |
| `LOOP_MAX_TICKS` | Stop after N ticks; 0 = forever. |
| `LOOP_WORKER_TIMEOUT_S` | Per-worker wall ceiling (default 7200). |
| `LOOP_CONFIG_PATH` | Override YAML file location. |

### YAML

See [`forge-loop.example.yaml`](forge-loop.example.yaml) for the full schema.
`forge-loop init` scaffolds a starter config in any project.

## CLI

```sh
forge-loop init [--create-labels]    # scaffold forge-loop.yaml + manual/ in a project
forge-loop run                       # run the loop in the foreground
forge-loop status                    # print current state file
forge-loop events -n 30              # tail event JSONL
forge-loop config                    # print resolved config
forge-loop pause                     # pause after current tick
forge-loop resume
forge-loop stop                      # graceful stop
forge-loop mcp serve                 # run as an MCP server on stdio
```

## MCP server

forge-loop is also an MCP server. Wire it into Claude Code (or any MCP
client) by adding to your `mcp.json`:

```json
{
  "mcpServers": {
    "forge-loop": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/forge-loop",
               "forge-loop", "mcp", "serve"],
      "env": {
        "LOOP_GH_REPO": "owner/your-repo"
      }
    }
  }
}
```

Exposed tools include `dev_sprint_workflow_start` (top-level), `gh_top_issues`,
`gh_create_issue`, `groom_backlog`, `dispatch_worker`, `critic_review_pr`,
`redeploy_project`, `loop_status`, `loop_events`, `events_query`,
`manual_lookup`, and more.

## Safety knobs

- **`risk_gate` label** — issues tagged `risk:high` skip auto-merge. Workers
  open the PR + comment "ready for human review" + exit.
- **`critic.enabled`** — every PR a worker opens goes through a review
  agent before auto-merge.
- **`attempts.enabled`** — workers see prior attempts on an issue and learn
  from past failures (persisted as GH issue comments).
- **`SIGTERM` / `SIGINT`** — graceful stop (current tick finishes).
- **`SIGUSR1`** — toggle pause/resume.
- **Touchfiles** — `docs/ops/loop-runner.{pause,stop}` for ops-friendly
  signaling without finding the PID.

## Manual / knowledge base

Drop markdown files into `manual/` (or `dev/sprint-loop/manual/` inside
the target project). Agents query them via `manual_lookup(topic)`. Each
file is one topic; filename stem = topic key. See
[`manual/file-layout.md`](manual/file-layout.md) for layout details.

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check src tests
uv run mypy src
```

## License

MIT — see [LICENSE](LICENSE).
