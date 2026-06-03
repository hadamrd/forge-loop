# forge-loop

**An autonomous control loop that converts well-specified GitHub issues into merged pull requests, unattended, until the backlog is empty.**

---

## Abstract

forge-loop is a harness, not a code generator. It places an autonomous coding
agent (Claude Code via the Anthropic SDK, or Codex) under a fixed contract —
the issue body — and walks it through that contract end to end with the
discipline of a careful engineer: branch in an isolated worktree, write tests,
commit, open a pull request, submit to a typed critic, merge what passes,
redeploy, repeat.

The central hypothesis is falsifiable and stated plainly: *an issue with
falsifiable acceptance criteria can be discharged by an unsupervised agent at
a cost and quality competitive with a junior engineer.* Issues that specify a
checkable post-condition ("the test asserts X occurs") are discharged cleanly.
Issues that specify taste ("clean up Y") are not, and the system is designed to
decline them rather than fabricate progress.

The codebase was largely constructed by running forge-loop against its own
backlog. The commit history is the primary experimental record.

---

## Mechanism

The runner advances in discrete ticks (default period: 60 s when idle). One
tick is a fixed control cycle:

```
sync trunk
  → [every Nth tick] groom backlog          (triage, dedupe, retitle)
  → expand thin issues into specifications   (product-owner pass)
  → select up to N ready issues              (label: loop:ready; default N=3)
  → dispatch N workers in parallel           (one git worktree each, off trunk)
        worker: read spec → tests → commit → push → open PR
  → critic pass: one typed CriticReport per PR
        sev1 → block auto-merge
        sev2/sev3 → inline review comments
  → merge gate: refuse if the source issue closed mid-flight
        otherwise squash-merge with auto-delete
  → [optional] redeploy
  → append to the event log; sleep; repeat
```

Each worker is disposable and sandboxed: it runs against a capability policy
(filesystem read/write roots, allowed network domains, MCP grants) and a
read-only planted configuration. The operator's intent is the only durable
input; everything else is reconstructable.

---

## Durable state and resumability

Working memory (an agent's context window) is treated as volatile. Project
cognition is externalized to a durable control plane under `.forge/`:

| Store | Contents |
|---|---|
| `events.db` | Append-only event log; the system's audit record. |
| `tasks.db` | One leased, compensatable saga per worker dispatch. |
| `frontier.yaml` | A compact statement of product direction: goal, current problem, next expansion, rejected approaches, hot files. |
| `memory.db` | Curated long-term facts (semantic / episodic / procedural), each with provenance. |

Two consequences follow.

**The loop is resumable.** Each dispatch leases a task saga and renews the
lease on a timer. A hard-killed process (operator interrupt, OOM, watchdog
SIGKILL) leaves its workers' leases unrenewed; the leases lapse within minutes
and the sagas read as *stale*. On the next start, the runner reconciles them —
running their compensations (reaping the orphaned worktree) and closing them —
before dispatching new work. The same reconciliation is available on demand:

```
worker hard-killed            →  saga left RUNNING, lease unrenewed
forge-loop boot               →  stale (dead-worker leases): saga-9-worker
forge-loop recover            →  reconciled 1 saga; reaped /tmp/wt-loop-9
forge-loop boot               →  clean
```

**Dispatch is informed by durable direction.** At each tick a maestro step
reads the frontier and curated memory, reorders candidate issues toward the
stated next expansion and away from recorded dead ends, and hands each worker
an advisory context block. (This pass is advisory and keyword-based; it
reorders but never drops work.)

---

## Operating envelope

Measured under subscription-mode billing (flat fee), Opus 4.7 workers, a typed
critic gating merges:

- **Throughput.** A senior engineer writing well-scoped tickets observes
  8–15 merged pull requests across an evening of background operation.
- **Cost.** $3–$5 per merged PR; ≈ $9 wasted per duplicate-race (rare).
  ≈ $50–$100 per week for continuous overnight operation.
- **Determinant of success.** Outcome quality correlates with the
  falsifiability of the acceptance criteria, not with ticket length.

```
discharged cleanly:  "Clicking Revoke opens a role=alertdialog modal;
                       ESC closes it; confirm POSTs /tokens/<id>/revoke."
not discharged:       "Make the revoke flow nicer."
```

The stable surface — SDK worker, typed critic, retry/cooldown, durable queue,
watchdog, resumable recovery — is the part intended to run unsupervised
overnight. Experimental surfaces (pipeline DAG, multi-repo, async runner, web
dashboard, telemetry, time-travel replay) are gated behind an extras install.

---

## Reproduction

```bash
uv tool install --from git+https://github.com/hadamrd/forge-loop forge-loop

cd your-repo
forge-loop init                            # scaffold config + .forge control plane
gh label create loop:ready --color FFD700  # the pickup label
gh issue edit 42 --add-label loop:ready    # mark one issue for dispatch
forge-loop run                             # foreground; Ctrl-C is safe (it resumes)
```

Prerequisites: `gh` authenticated, `git`, Python ≥ 3.11, and one agent provider
signed in (`claude` on a subscription plan, or `codex` locally).

Inspection: `forge-loop status` (health), `forge-loop boot` (reset-recovery
context), `forge-loop events` (event tail), `forge-loop doctor` (one-shot
check). An MCP server (`forge-loop mcp serve`) exposes the same surface to any
MCP client.

---

## Observed failure modes

These are drawn from real operation and determine the current safety design.
They are reported because honest limitations are the more useful half of any
empirical claim.

| Observation | Mitigation |
|---|---|
| Worker ships a large refactor for an issue closed mid-flight. | Pre-merge gate refuses on `state: CLOSED`. |
| Two workers race on the same file. | External-modification detection; one ships, the other self-closes as superseded. |
| Process hard-killed mid-tick; worktrees + leases orphaned. | Leases lapse → stale; reconciled (compensated, reaped) at next boot. |
| Worker silent for 10+ min during extended thinking. | Lease renewed on a wall-clock timer, independent of agent output. |
| Loop merges a PR that upgrades its own packaging. | Version-change detector exits cleanly; the shim re-execs the fresh install. |
| Misbehaving worker floods a tool. | Per-tool MCP rate cap (default 20 calls / process). |

---

## Constraints

- **Billing.** Assumes a flat-fee subscription; there is no per-token budget gate.
- **Secrets.** The loop never reads secrets; workers fetch them from the
  project's secret manager. Plaintext secrets in commits are forbidden by brief.
- **Identity.** All GitHub and git operations run under the operator's
  credentials; authorship is the operator's, with an optional co-author trailer.

---

## Provenance

forge-loop is the extracted harness that built
[Titan](https://github.com/hadamrd/dashboard-plugin), a post-Jenkins CI/CD
engine, on its own backlog. The recursive-bootstrap pattern — a loop shipping
its own features under its own critic — is the experiment; the repository is the
log. Built on [Claude Code](https://claude.com/claude-code) and the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

MIT licensed.
