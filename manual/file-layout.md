# forge-loop file layout

forge-loop is filesystem-light. Three things on disk: package source, a
state directory written at runtime, and (optionally) a per-project
`manual/` knowledge base.

## State directory

By default the loop writes to `docs/ops/` in the target repo (overridable
via `cfg.state_dir`). Contents:

```
docs/ops/
├── loop-runner.json              # current state (state, tick, dispatched, last outcomes)
├── loop-runner-events.jsonl      # structured event bus (append-only)
├── loop-runner-summaries.jsonl   # one consolidated row per tick
├── loop-runner.pid               # PID of the active runner (if foreground)
├── loop-runner.pause             # touchfile: pause after current tick
├── loop-runner.stop              # touchfile: graceful stop
├── loop-runner.HALT              # halt marker (`all-nighter.sh` respects this)
└── loop-runner-logs/
    ├── worker-<n>-<ts>.log       # stream-json log per worker subagent
    ├── po-<n>-<ts>.log           # PO spec-expander logs
    └── maintenance-<ts>.log      # AI-as-PM maintenance subagent logs
```

## Worker worktrees

Each ticked issue gets its own git worktree:

```
/tmp/wt-loop-<issue_number>/
├── .claude/settings.json     # planted permissive settings (the loop manages it)
├── sprint-events.jsonl       # subagent-written events (parent reads)
└── <full working tree>       # branch loop/<n>-<slug>, based off origin/trunk
```

Worktrees are force-removed at the start of every tick — anything not
committed and pushed is lost.

## Manual / knowledge base

Operator-authored markdown files agents can read at runtime via the MCP
tools `manual_topics` / `manual_lookup` / `manual_search`. Per-repo
overrides take precedence over package defaults.

Search order (first match wins):

1. `<repo>/dev/sprint-loop/manual/*.md`
2. The package's bundled `manual/*.md` (this directory)

One file per topic; filename stem = topic key. Title = first non-empty
line (markdown `#` stripped).

Suggested entries to author per project:

- `secrets.md` — how your secret manager works (Vault / Infisical /
  sealed-secrets / KMS / whatever) so agents fetch values without
  asking humans.
- `deploy.md` — pre-flight + verify steps around your deploy task.
- `architecture.md` — quick orientation for agents new to the codebase.
