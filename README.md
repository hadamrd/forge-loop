# forge-loop

> Autonomous multi-worker dispatcher for Claude Code and Codex.
> File an issue, label it `loop:ready`, walk away.

forge-loop turns a Claude Code subscription into an unattended swarm of
agents that picks up GitHub issues, opens worktrees, ships PRs, reviews
them with a typed-rubric critic, merges what passes, and redeploys —
indefinitely. It is the OSS extraction of the harness that built
[the Titan CI/CD engine](https://github.com/hadamrd/dashboard-plugin)
on its own backlog.

```
  GH issue labeled `loop:ready`
            │
            ▼
        ┌───────────────────────────────────┐
        │  forge-loop run  (tick every 60s) │
        └────────────────┬──────────────────┘
                         │
   1. sync trunk  ──────►│
   2. every Nth tick: maintenance pass (groom backlog)
   3. PO pass: expand thin issues into spec'd issues
   4. dispatch up to N workers in parallel (default 3)
            │
   ┌────────┴────────┬────────┐
   ▼                 ▼        ▼
  worker            worker    worker
  Opus 4.7 via SDK · git worktree off origin/trunk
  reads spec → tests → commit → push → gh pr create
            │
            ▼
   5. critic pass: typed CriticReport per PR
      ├─ sev1 → block auto-merge + label `critic:blocking`
      └─ sev2/sev3 → inline review comments
            │
            ▼
   6. merge gate: refuses if source issue closed mid-flight (#65)
      └─ otherwise: gh pr merge --squash --auto --delete-branch
            │
            ▼
   7. redeploy (optional): run configured `task <name>`
   8. emit events.jsonl + sleep + loop ↺
```

---

## Why it exists

A senior engineer writing well-scoped tickets can have forge-loop ship **8–15 PRs in an evening** of background work. The ones we measured land at $3–$5 per PR (Opus 4.7) with a typed critic gating merges. The tickets that flow cleanly are the ones with **falsifiable acceptance criteria** ("the test must assert X happens"); subjective tickets ("clean up Y") still need a human at the wheel.

This isn't a "code generator". It is a **harness** — a runner that lets the operator's intent (the issue body) become the contract, and walks an autonomous agent through that contract end-to-end with the same discipline a good engineer would: fingerprint-based retry, drift detection, structured critic, attempts ledger, auto-restart on its own self-upgrade.

The loop is also **resumable**. A hard-killed run (operator `^C` mid-tick, OOM, watchdog SIGKILL) no longer loses its place: `forge-loop run` reboots from a durable `.forge` control plane and self-heals the workers that died with the process — reaping their orphaned worktrees and closing their dead-worker task sagas before the first new dispatch.

---

## Stability matrix

| Surface | Status | Notes |
|---|---|---|
| **SDK worker (Opus 4.7)** | **stable** | Native Anthropic SDK, typed event stream |
| **Codex provider** | beta | `codex exec` backend for worker / PO / critic roles |
| **Typed critic (`CriticReport`)** | **stable** | sev1/sev2/sev3 findings, gates auto-merge on sev1 |
| **Retry + fingerprint cooldown** | **stable** | Skips in-flight and cooldown duplicates |
| **PO spec expander** | **stable** | Rewrites thin issue bodies into feature-grade specs |
| **Briefs as `.md.tmpl`** | **stable** | Per-role overrides via env or yaml |
| **SQLite queue** | **stable** | Durable embedded backend |
| **Watchdog** | **stable** | 15 min idle warn / 30 min idle kill |
| **`forge-loop doctor`** | **stable** | One-shot health check (Rich-rendered) |
| **MCP introspection tools** | **stable** | `loop_status`, `events_recent`, `worker_logs`, `loop_snapshot`, … |
| **Durable control plane (`.forge/{events,frontier,memory,tasks}`)** | **stable** | Seeded by `init`; survives a hard kill |
| **Task-saga lifecycle per dispatch** | **stable** | create → lease RUNNING → terminal; expired lease ⇒ stale (dead-worker) |
| **Resumable boot recovery** | **stable** | `run` self-heals stale sagas at startup; `forge-loop boot` / `recover` do it on demand |
| **Boot-context-driven planning (maestro)** | beta | Each tick reorders candidates by frontier alignment (dead-ends last) and hands workers an advisory frontier+memory context block; emits a `maestro_plan` event. Matching is keyword-based and advisory — it never drops work. |
| **Pipeline DAG (`.forge/pipeline.yaml`)** | experimental | Opt-in via `LOOP_PIPELINE_DRIVEN=1` |
| **Multi-repo (`.forge/repos/*.yaml`)** | experimental | Single host, single loop, N repos |
| **Async runner** | experimental | `--orchestrator async` |
| **Web dashboard** | experimental | `[experimental]` extras required |
| **Slack/Discord/webhook integrations** | experimental | Adapters in `forge_loop.integrations` |
| **Prometheus + OpenTelemetry** | experimental | `[experimental]` extras required |
| **Time-travel replay** | experimental | `forge-loop replay --tick N` |

Anything marked experimental lives behind an extras gate (`pip install 'forge-loop[experimental]'`). The stable surface is the part designed to run unattended overnight.

---

## Quickstart (60 seconds)

```bash
# 1. install
uv tool install --from git+https://github.com/hadamrd/forge-loop forge-loop

# 2. inside your repo
forge-loop init                                  # writes forge-loop.yaml + manual stub
gh label create loop:ready --color FFD700        # the pickup label

# 3. label an issue you want shipped
gh issue edit 42 --add-label loop:ready

# 4. start
forge-loop run                                   # foreground (Ctrl-C to stop)
#  ── or detach in tmux ──
tmux new -d -s loop "forge-loop run"
```

Prerequisites: `gh` authenticated, `git`, Python 3.11+, and at least one
configured agent provider: `claude` CLI signed in on a subscription plan or
`codex` CLI signed in locally.

---

## Configuration

The committed `forge-loop.yaml` is the project's loop contract. Operator-local
state (events, pid, halt markers) is gitignored.

```yaml
# forge-loop.yaml — everything below is optional; env vars override yaml.
repo:
  github: owner/repo                  # required (or LOOP_GH_REPO env)
  base_branch: trunk                  # default branch used for worker worktrees
  worktree_root: /tmp                 # /tmp/wt-loop-<N> per worker

deploy:
  task: ""                            # empty = no redeploy. Set to "deploy:k3s:trunk" etc.

scheduling:
  parallel: 3                         # max concurrent workers
  tick_interval_s: 60                 # poll cadence when idle
  worker_timeout_s: 7200              # hard wall ceiling per worker
  maintenance_every_n_ticks: 5        # AI-as-PM groom pass cadence

labels:
  ready: loop:ready
  blocked: loop:blocked
  risk_gate: risk:high                # auto-merge skipped; human review

agent:
  provider: claude                    # claude or codex; role blocks can override

critic:
  enabled: true                       # typed-rubric review before merge

attempts:
  enabled: true                       # persist per-issue history as gh comments

worker:
  brief_template: .forge-loop/briefs/worker.md.tmpl
  provider: claude
  model: claude-opus-4-7
  thinking: medium

po:
  brief_template: .forge-loop/briefs/po.md.tmpl
  provider: claude
  model: claude-opus-4-7
  thinking: high
```

### Agent providers

Claude remains the default and uses the Claude Agent SDK for workers. Codex is
available through the local `codex exec` CLI for worker, PO, and critic roles.
Set it globally:

```yaml
agent:
  provider: codex
```

or per role:

```yaml
worker:
  provider: codex
  model: gpt-5-codex   # optional; omit or set "" to use the Codex CLI default
po:
  provider: claude
critic:
  provider: codex
```

### Env-var overrides (highest priority)

| Var | Effect |
|---|---|
| `LOOP_GH_REPO` | `owner/repo` — required |
| `LOOP_AGENT_PROVIDER` | Global provider: `claude` or `codex` |
| `LOOP_WORKER_PROVIDER` / `LOOP_PO_PROVIDER` / `LOOP_CRITIC_PROVIDER` | Per-role provider override |
| `LOOP_WORKER_BRIEF` / `LOOP_PO_BRIEF` / `LOOP_CRITIC_BRIEF` | Path to a custom brief template |
| `LOOP_WORKER_MODEL` / `LOOP_PO_MODEL` / `LOOP_CRITIC_MODEL` | Per-role model override; Codex may be blank to use CLI default |
| `LOOP_PARALLEL` / `LOOP_TICK_INTERVAL_S` | Scheduling |
| `LOOP_DEPLOY_TASK` | Task target run after merges. Empty = skip |
| `LOOP_DEPLOY_DRIFT_HALT=1` | Opt in to the 3-fails-then-halt brake |
| `LOOP_RETRY_COOLDOWN_S` | Cooldown before re-trying a failed issue (default 3600) |
| `LOOP_COAUTHOR` | `Name <email>` appended as `Co-Authored-By:` on commits |
| `LOOP_PIPELINE_DRIVEN=1` | Route ticks through `.forge/pipeline.yaml` executor |
| `LOOP_QUEUE_URL` | `sqlite:///path/to/queue.db` for the durable backend |
| `LOOP_MCP_CAP_DEFAULT` | Per-tool MCP rate cap (default 20/process) |
| `FORGE_LOOP_EXPERIMENTAL=1` | Force-allow experimental modules even without the extra |

---

## Briefs — teach the loop about *your* project

The bundled briefs are intentionally generic. To produce production-grade
PRs in your codebase, drop project-specific overrides under `.forge-loop/briefs/`
and point env vars (or yaml) at them.

```
.forge-loop/briefs/
├── worker.md.tmpl       # required reads, test layout, build commands
├── po.md.tmpl           # effort bar, file-pointer rules
└── critic.md.tmpl       # rubric, severity policy
```

A good production-grade worker brief tells the agent:

- which docs to `cat` before touching code (CONSTITUTION, CLAUDE.md, design notes)
- the test layout (`*/src/test`, `*/src/integrationTest`, `e2e/specs/`)
- the build incantation (e.g. `./gradlew -p <module> test --tests <Class> --no-daemon -Xmx1500m` on WSL)
- forbidden patterns (Jenkins imports in a post-Jenkins product, plaintext secrets, `window.confirm` in styled UIs)
- the project's typed-config discipline (discriminated-union `type:`, no URL sniffing)

Placeholders the loader fills: `{n}`, `{issue_title}`, `{body}`, `{worktree}`,
`{history_section}` (worker); `{issue_number}`, `{issue_body}`, `{github_repo}` (PO).

---

## Manifestos & the brainstormer (axis-aligned tickets)

`forge-loop` does NOT just ship whatever you label. To produce *valuable* work the loop reads four customer-owned manifestos under `.forge/` and refuses cosmetic tickets.

### The four files you own

```
.forge/
├── product-vision.md     # free-form prose: who you serve, the wedge, what's NOT valuable
├── axes.yaml             # structured: the 4-6 value axes the loop must move
├── quality-manifesto.md  # how code MUST be written (critic enforces, sev1 blocks merge)
└── testing-manifesto.md  # how tests MUST be written (consulted by worker post-impl)
```

Every shipped ticket cites the axis it serves; every PR is gated by the quality + testing manifestos via the critic.

### Example `axes.yaml`

```yaml
axes:
  - name: golden-path-e2e
    customer: "SRE running their first pipeline on day zero"
    valuable_means: "Playwright tests driving the real rig — golden path survives every release"
    acceptable_work:
      - "Customer-shaped pipeline fixtures (Node, Java, polyglot)"
      - "Adversarial paths: failed step, OOM step, secret-needing step"
    rejected_as_cosmetic:
      - "304 responses to polls customers don't notice"
      - "Pretty timestamps, sparklines, theme polish"

  - name: scm-depth
    customer: "Team migrating from GitLab self-hosted / Bitbucket Cloud"
    ...
```

### Brainstormer workflow

```bash
# 1. Drop manifestos in .forge/ (see above)

# 2. Dry-run — propose axis-aligned epics + tickets, print and save them
GH_TOKEN=$(gh auth token) forge-loop brainstorm --output report.yaml

# 3. Review/curate report.yaml, then apply exactly that reviewed report
forge-loop brainstorm --apply --report report.yaml

# 4. The loop dispatches on the new loop:ready tickets normally
forge-loop run
```

Each filed ticket carries `axis:<name>` + `loop:ready` (or `epic` for parent rollups), plus a customer-story quote pulled from your vision.

### The feedback loop

Every bug → quality manifesto update → permanent gate.

```bash
# 1. A bug ships and gets fixed in PR #N
# 2. Generate a manifesto delta proposal based on the failure shape
forge-loop manifesto suggest --from-pr <N>

# 3. Review + commit. From the next worker run, the critic enforces it.
```

Real example: PR #147 hot-fixed a stringly-typed event-boundary bug. The quality manifesto gained a `No stringly-typed cross-module discriminators` rule. The critic now blocks any future PR that compares `event["kind"] == "literal"` across module boundaries.

---

## CLI reference

```
forge-loop run               # the main loop (foreground)
forge-loop doctor            # one-shot health check (Rich table)
forge-loop status            # current state file
forge-loop boot [--json]     # print reset-recovery context from durable .forge state
forge-loop recover [--json]  # reconcile dead-worker sagas (reap worktrees + close them)
forge-loop events [--tail N] # tail loop-runner-events.jsonl (colored)
forge-loop pause / resume    # touchfile control
forge-loop stop              # graceful stop at next tick boundary
forge-loop retry <issue>     # bypass cooldown for one issue
forge-loop record-session    # capture a real SDK session as a test fixture
forge-loop replay --tick N   # dry-run replay of a past tick
forge-loop brief --kind worker --issue 42   # render the brief the loop would send
forge-loop config [models]   # print resolved config (or per-role model table)
forge-loop brainstorm        # propose axis-aligned epics + tickets (dry-run)
forge-loop brainstorm --apply # file the proposed tickets on GitHub
forge-loop manifesto suggest --from-pr N  # propose manifesto deltas from a bug fix
forge-loop pipeline show     # render the .forge/pipeline.yaml DAG
forge-loop repos list/disable/enable  # multirepo state
forge-loop roles list        # pluggable role definitions
forge-loop mcp serve         # expose loop tools to MCP clients
forge-loop dashboard --tui   # Textual operator dashboard
forge-loop dashboard --web   # FastAPI dashboard (requires [experimental])
```

---

## MCP server — drive the loop from any MCP client

```bash
# wire forge-loop into your Claude Code config
claude mcp add forge-loop \
  --command "forge-loop mcp serve" \
  --env LOOP_GH_REPO=owner/repo
```

### Tools exposed

| Tool | Purpose |
|---|---|
| `loop_status` | Current tick, state, queue depth |
| `loop_events(n)` | Most-recent events from `loop-runner-events.jsonl` |
| `events_recent(kind, since_minutes, limit)` | Filtered tail |
| `events_query(sql)` | DuckDB SELECT against `events` + `summaries` views |
| `events_count_by_kind(since_minutes)` | Per-kind histogram |
| `worker_logs(issue, kind_filter, tail)` | Per-worker stream-json (truncated for context) |
| `loop_snapshot(since_minutes)` | One-call "what is the loop doing" |
| `attempts_history(issue)` | Per-issue attempt ledger |
| `gh_top_issues(label, limit)` | Read-only GH pickup |
| `gh_comment` / `gh_create_issue` / `gh_update_issue` / `gh_close_issue` / `gh_unlabel` | Issue mutators (rate-capped) |
| `dispatch_worker(issue_number)` | One-shot single-worker dispatch |
| `redeploy_project(task_name)` | Trigger the deploy hook |
| `groom_backlog(timeout_s)` | AI-as-PM maintenance pass |
| `critic_review_pr(pr_url, issue_number)` | On-demand critic |
| `run_sprint_workflow(parallel, max_ticks)` | The full loop, bounded |
| `manual_topics` / `manual_lookup` / `manual_search` | Project runbooks |
| `ask_operator(question, options, context)` | Mid-flight human checkpoint |

Mutating tools are rate-capped (default 20 calls per MCP-server lifetime,
configurable via `LOOP_MCP_CAP_<TOOL>=N`). A buggy worker can't open 100
issues from the loop's identity.

---

## For AI agents driving forge-loop

If you (the AI) are operating the loop on behalf of a human, your job is **not** to write the PRs — the workers do that. Your job is:

1. **Pick what to work on.** Read recent `events_recent` + `loop_snapshot`. Look for `loop:ready` issues with concrete behavioural ACs. Issues without falsifiable ACs are PO candidates, not worker candidates — let the PO pass run on those.
2. **Don't dispatch competing workers on the same surface.** Two parallel workers rewriting the same file = duplicate effort, $10+ of waste. Maintenance pass should dedupe; flag if it doesn't.
3. **Re-fetch issue state before drawing conclusions.** An issue may have been closed since the last loop tick.
4. **Use `worker_logs(issue, kind_filter='tool_use')`** to see what a worker is *doing*, not just what events it emitted.
5. **Believe the critic** on sev1, but verify on sev3 — sev3 findings are advisory and often noise.
6. **When in doubt, `ask_operator`.** Better one human checkpoint than three duplicate PRs.

### Common mistakes
- Hand-rolling bash one-liners against `events.jsonl` instead of calling `events_recent` / `loop_snapshot`
- Believing a worker is dead because no log was written in 5 min — Opus extended thinking can take 10+ min between writes. The watchdog handles real wedges
- Closing an issue mid-flight expecting the loop to stop — it does NOT yank running workers, but post-#65 the merge gate refuses to land their PRs

---

## Observed failure modes

These came out of real dogfooding sessions and inform the current safety design.

| Failure | Loop's mitigation |
|---|---|
| Worker on a closed-mid-flight issue ships a 1300-LOC refactor | Pre-merge gate refuses on `state: CLOSED` (#65) |
| Two parallel workers race-rewriting the same `cli.py` | Workers detect "file modified externally", re-read state; one ships, the other self-closes its PR as superseded |
| Worker SDK init timeout kills early dispatches | Cooldown skip + retry next tick |
| Deploy step misconfigured → 3 consecutive failures | Drift halt is **opt-in** (`LOOP_DEPLOY_DRIFT_HALT=1`); default is warn-only |
| Worktree from a crashed prior run | Orphan reaper at boot |
| Loop self-upgrades via a merged PR | Version-change detector exits cleanly; shim re-execs against fresh install |
| Events.jsonl grows unbounded | Boot-time rotation at 10 MB (3 archives kept) |
| Per-tool MCP spam from a misbehaving worker | Per-tool rate cap (default 20/proc) |

---

## Architecture (one tick, in detail)

```
runner.tick():
  if maintenance_due:
      run_maintenance()                  # AI-as-PM groom: triage / dedupe / retitle
      return

  issues = gh_top_issues(loop:ready)
  if not issues: idle()

  if po.enabled:
      expand_thin_specs(issues)           # rewrite skinny bodies → feature-grade

  for issue in issues:
      skip if attempts.fingerprint says in-flight or cooldown
      remove loop:ready after PR opens    # keep queue from recycling in-review work
      run_worker(issue) in ThreadPoolExecutor[parallel=N]
        └─ git worktree add /tmp/wt-loop-<N> -B branch origin/<base_branch>
        └─ plant .claude/settings.json (permissive, read-only)
        └─ claude_agent_sdk.query(prompt=worker_brief, options)
             stream typed events:
               • turn_start
               • tool_use / tool_result
               • assistant_text
               • final_result          ← extracts pr_url + status
               • error                 ← classified: timeout/429/oom
        └─ append to attempts.jsonl as a GH issue comment

  for pr in opened_prs:
      run_critic(pr) → CriticReport
        └─ overall: approve / request_changes / block
        └─ findings: [sev1, sev2, sev3] × [correctness/security/style/tests/docs]
      apply_critic_actions(report, pr)
        └─ sev1 → label "critic:blocking", disable auto-merge
        └─ sev2/sev3 → inline review comments

  for outcome in outcomes:                 # pre-merge gate (#65)
      if gh.get_issue_state(outcome.issue) != "OPEN":
          disable_pr_auto_merge(outcome.pr_url)
          gh.pr_comment("source issue closed mid-flight; refusing merge")
          outcome.status = "open"          # don't lie to attempts ledger

  for merged_issue in merged_nums:
      reap_worktree(merged_issue)

  if merged_nums and deploy_task:
      ok, log = redeploy(repo, deploy_task)
      if not ok and consecutive_fails >= 3:
          if LOOP_DEPLOY_DRIFT_HALT=1: halt
          else: emit `deploy_drift_warn`

  append `tick_done` event
  short_sleep(tick_interval_s)
```

State splits into two tiers. The **operator runtime** under `docs/ops/` is
ephemeral and gitignored:

- `docs/ops/loop-runner-events.jsonl` — the immutable audit log
- `docs/ops/loop-runner.json` — current state snapshot
- `docs/ops/loop-runner-logs/worker-<N>-<ts>.log` — per-worker stream-json
- `docs/ops/loop-runner.{pid,pause,stop,HALT}` — control touchfiles

The **durable control plane** under `.forge/` is the part that makes a crashed
run resumable — see the next section.

---

## Durable control plane & resumability

`forge-loop init` seeds a small durable control plane under `.forge/` that
survives a hard kill:

- `.forge/events.db` — append-only event log with projection cursors
- `.forge/frontier.yaml` — the frontier cursor (product goal, current problem, next expansion)
- `.forge/memory.db` — curated memory (active facts + rejected paths)
- `.forge/tasks.db` — leased, compensatable **task sagas**

Every worker dispatch now records a task-saga lifecycle in `.forge/tasks.db`:
the saga is **created**, then **leased RUNNING** (lease TTL = the worker wall
ceiling), then driven to a **terminal** state from the worker outcome
(`merged`/`open` complete it; anything else fails it, which runs the saga's
worktree-reap compensation). Saga bookkeeping is best-effort — it never blocks
a real dispatch or masks a worker outcome.

If the loop process is hard-killed, the workers it leased die with it. Their
sagas stay RUNNING with a lease that no longer beats; once the lease lapses
they read as **stale** (an expired-lease, dead-worker saga). On the next
`forge-loop run`, a boot-recovery pass reconciles every stale saga: it reaps
the orphaned worktree (the saga's compensation) and marks the saga
`COMPENSATED`, draining it from the in-flight view. The source issue, if still
labelled, is re-picked naturally on the next tick. Recovery never touches
GitHub, so it is safe to run offline.

Two operator commands expose the same machinery on demand:

- `forge-loop boot` assembles and prints the **reset-recovery context** —
  frontier cursor, active/rejected memory ids, in-flight + stale saga ids, and
  the event-log position (`--json` for scripts).
- `forge-loop recover` runs the dead-worker reconciliation sweep itself —
  the one-shot of what `run` does at boot (`--json` for scripts).

A crash → boot → recover → clean flow:

```bash
# a run is hard-killed mid-tick (^C / OOM / SIGKILL)
forge-loop boot              # frontier + ...; "stale (dead-worker leases): saga-42-worker"
forge-loop recover           # "recovery: reconciled 1 dead-worker saga(s)" — reaps the worktree
forge-loop boot              # stale list is now empty; clean to restart
forge-loop run               # would have self-healed at boot anyway; the issue is re-picked
```

---

## Operator guide

### Daily flow

```bash
# in the morning
forge-loop doctor                          # green/yellow/red one-screen sanity
forge-loop events -n 50                    # what shipped overnight
gh pr list --search "is:merged author:@me" --limit 10

# label new work
gh issue edit 142 --add-label loop:ready

# if anything looks wrong
forge-loop pause                           # touch pause file; current tick finishes
# … investigate …
forge-loop resume
```

### Recovery

| Symptom | Action |
|---|---|
| Loop stopped, halt marker present | `cat docs/ops/loop-runner.HALT` — the reason is the first line. Address it, `rm` the file, restart. |
| Worker stuck (no log writes >15 min) | Watchdog will SIGKILL at 30 min. To kill earlier: `tmux attach`, Ctrl-C. |
| Many orphan `/tmp/wt-loop-*` worktrees | They're reaped at next boot — or `for d in /tmp/wt-loop-*; do git worktree remove --force $d; done` |
| Multiple PRs racing on the same surface | Manual `gh pr close <N> --delete-branch` on the duplicate; comment on the issue to mark as superseded |
| Critic merging garbage | Inspect the CriticReport via `events_query "SELECT * FROM events WHERE kind='critic_done'"`. Tighten the critic brief. |

### Producing PRs at scale — the operator's discipline

The loop ships well when **every ticket has falsifiable acceptance criteria**.

- ✅ "User clicks Revoke; a themed modal opens with `role=alertdialog`; ESC closes; confirm POSTs to `/api/v1/me/tokens/<id>/revoke`."
- ❌ "Make the revoke flow nicer."

The PO pass rewrites thin tickets — but it can't invent intent. Spend two minutes writing a tighter spec and the loop will return 700+ LOC of real implementation + tests against it.

---

## Production considerations

- **Subscription billing only.** forge-loop assumes the operator is on a Claude Code subscription (flat fee). The budget tracking that existed in early versions was removed in #38; if you need per-token gating because you're paying per call, file an issue.
- **Secrets.** The loop never reads secrets. Workers should fetch them via your project's secret manager (Infisical, Vault, sealed-secrets). The bundled worker brief explicitly forbids plaintext secrets in commits.
- **Identity.** All `gh` calls go through the operator's `gh auth login`. Workers commit under the operator's git identity (configurable via `LOOP_COAUTHOR` for the `Co-Authored-By:` trailer).
- **Rate limits.** GitHub: the loop's pickup query is one `gh issue list` per tick (cheap). `gh pr merge --auto` doesn't poll. Agent-provider limits depend on the configured backend: Anthropic Opus via SDK or the local Codex CLI.
- **Cost.** Observed: $3-$5 per shipped PR on Opus 4.7, $9 wasted per duplicate-race (rare). Roughly $50-$100/week for a full unattended-overnight workflow.

---

## Development

```bash
git clone https://github.com/hadamrd/forge-loop
cd forge-loop
uv sync --extra dev --extra experimental
uv run pytest                              # 480+ tests, 25 s
forge-loop --help                          # local install via uv run
```

PRs welcome. CI runs the test matrix on push to `trunk`; the loop itself drove most of the codebase.

---

## License

MIT — see [LICENSE](LICENSE). Contributions under the same license.

---

## Acknowledgements

forge-loop was extracted from the harness that built [Titan](https://github.com/hadamrd/dashboard-plugin) — a post-Jenkins CI/CD product — on its own backlog. The recursive-bootstrap dogfooding pattern (the loop shipping its own features) is documented in the repository's commit history; PRs #2, #27, #41, #62 are particularly worth reading.

Built on [Claude Code](https://claude.com/claude-code) + the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python), with optional Codex CLI support.
