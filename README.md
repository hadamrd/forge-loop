# forge-loop

Autonomous multi-worker dispatcher for Claude Code — picks up GitHub issues
by label, dispatches parallel Claude workers in git worktrees, watches PRs,
merges, and redeploys.

> **Billing model: subscription only.** forge-loop assumes the operator runs
> on a Claude Code subscription (e.g. the Max plan) where billing is flat,
> not per-token. There is no token-cost accounting and no per-ticket /
> per-tick USD budget gate; if you need one because you're paying per-call,
> file a separate issue describing the operator persona and we'll design for
> it then. (Historical note: a token-budget gate shipped briefly under
> issue #6 and was removed in issue #38 — it was both off-thesis for the
> subscription persona and actively buggy.)

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

## Stability matrix

forge-loop's surface is split into a **stable** core and a quarantined
**experimental** ring (issue #39). The default `pip install forge-loop`
ships ONLY the stable surface; experiments require an extra and a
matching feature flag.

### STABLE (default install — supported)

| Module | What it does |
|---|---|
| `forge_loop.worker` | Claude Agent SDK worker (Opus 4.7) — the main dispatch path |
| `forge_loop.critic` | Typed CriticReport + per-finding gating |
| `forge_loop.attempts` | Per-issue attempts ledger w/ retry + cooldown |
| `forge_loop.briefs/*` | Externalised worker / PO / critic brief templates |
| `forge_loop.po` | PO spec-expander for thin tickets |
| `forge_loop.maintenance` | Periodic maintenance pass (triage / dedupe) |
| `forge_loop.watchdog` | Liveness watchdog + idle-kill |
| `forge_loop.runner` | Synchronous tick loop (PO → workers → critics) |
| `forge_loop.queue.in_memory` | Default queue (test / in-process) |
| `forge_loop.queue.sqlite` | Durable embedded queue (WAL) — production default |

### EXPERIMENTAL (gated, requires `pip install 'forge-loop[experimental]'`)

| Module | Why it's experimental |
|---|---|
| `forge_loop.multirepo` | One loop serves N repos — zero downstream operator yet |
| `forge_loop.runner_async` | Three-stage asyncio orchestrator — sync path is the supported one |
| `forge_loop.dashboard` | Stdlib HTTP `/metrics` + `/healthz` — no scrape consumer yet |
| `forge_loop.integrations.*` | Slack / Discord / generic-webhook adapters |
| `forge_loop.observability.*` | Prometheus + OpenTelemetry exporters |
| `forge_loop.replay` | Time-travel re-run of a past tick with a new brief |
| `forge_loop.pipeline` | Declarative role-chain pipeline (`.forge/pipeline.yaml`) |

Each experimental module's top-level import calls
`forge_loop._extras.require_experimental()`, which raises a clear
`ImportError` naming the extra to install if the gate is not satisfied.
Set `FORGE_LOOP_EXPERIMENTAL=1` to bypass the gate during local development.

### REMOVED in #39

| Module | Reason |
|---|---|
| `forge_loop.queue.redis_backend` | Premature distribution — no real 2+ host operator. Replaced by `SQLiteQueue`. |
| `forge_loop.cluster` (election + coordinator) | Same reason — single-host is the supported surface. |

## Running on a Claude subscription (flat-fee mode)

forge-loop is designed for an operator running on a Claude Pro / Max /
Team subscription, not a metered API key. **Per-token budget tracking is
not supported** in this mode — the loop's spend gauges will read $0 and
the `LOOP_DAILY_BUDGET_USD` / `LOOP_TICK_BUDGET_USD` knobs are no-ops.

What you get instead:

* The watchdog wall-clock budget (`LOOP_WORKER_TIMEOUT_S`) still applies
  per worker — it's the actual safety net under a flat fee.
* `forge-loop status` reports tick counts, PRs merged, failures.
* Cooldowns + attempts ledger still gate retries, so a stuck issue
  can't burn unbounded wall time.

If you DO have a metered key and want token accounting, set up a
separate billing scrape — the current default is "operator pays a flat
fee, we count work done not tokens spent".

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

### Choosing models per role

The loop runs three agent roles with very different cognitive shapes, so
each gets its own model + thinking-budget knob (issue #34). Defaults are
tuned for the trade-off observers in the loop's own dogfooding session
identified:

| Role     | Default model        | Default thinking | Why                                                |
| -------- | -------------------- | ---------------- | -------------------------------------------------- |
| `worker` | `claude-opus-4-7`    | `medium`         | medium-effort implementation across many files     |
| `po`     | `claude-opus-4-7`    | `high`           | hard thinking about spec quality before workers go |
| `critic` | `claude-sonnet-4-6`  | `off`            | rubric-checking is fast and cheap on Sonnet        |

Override via env (highest precedence) or YAML:

```sh
export LOOP_WORKER_MODEL=claude-sonnet-4-6
export LOOP_WORKER_THINKING=low
export LOOP_PO_MODEL=claude-opus-4-7
export LOOP_PO_THINKING=high
export LOOP_CRITIC_MODEL=claude-sonnet-4-6
export LOOP_CRITIC_THINKING=off
```

```yaml
worker:
  model: claude-opus-4-7
  thinking: medium
po:
  model: claude-opus-4-7
  thinking: high
critic:
  model: claude-sonnet-4-6
  thinking: "off"        # quote — bare ``off`` is YAML's false
```

Inspect what's actually resolved at runtime:

```sh
forge-loop config models           # human-readable table
forge-loop config models --json    # machine-readable
```

Unknown model aliases (e.g. `opus-99`) fail loudly at startup with the
offending value named, rather than silently at first dispatch.
Thinking-budget configurability for the PO and critic is currently
deferred — both still run via `claude -p` subprocess and the CLI does
not expose a thinking flag; they'll wire up once those roles migrate to
the Claude Agent SDK.

## CLI

```sh
forge-loop init [--create-labels]    # scaffold forge-loop.yaml + manual/ in a project
forge-loop run                       # run the loop in the foreground
forge-loop status                    # print current state file
forge-loop events -n 30              # tail event JSONL
forge-loop config                    # print resolved config
forge-loop config models             # print per-role model + thinking-budget
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

Exposed tools include `run_sprint_workflow` (top-level), `gh_top_issues`,
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
