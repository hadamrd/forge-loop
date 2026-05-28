# forge-loop: the operator's guide

This is the longer companion to the [README](../README.md). The README is the
reference; this is the narrative. Read this once, end-to-end, before your
first overnight run.

---

## 1. The mental model

forge-loop is a **loop runner**, not a code generator. The loop's job is to:

1. Pick a labeled issue from your repo.
2. Hand the issue body to Claude Code (Opus 4.7) as a brief.
3. Watch Claude work — git worktree, tests, commit, push.
4. Review the resulting PR with a typed-rubric critic.
5. Merge what passes, leave the rest for you.

The interesting question is **not** "can the AI write code" (it can). The
interesting question is **"how do you make a swarm of autonomous agents
behave well over hours of unattended operation"** — and that's what the
loop's discipline is for: fingerprint cooldowns, drift detection,
attempts ledger, closed-issue merge gate, watchdog, auto-restart.

You set the policy via the brief and the issue's acceptance criteria.
The loop is the dispatcher.

---

## 2. Your first day

### Setup (5 minutes)

```bash
# install forge-loop
uv tool install --from git+https://github.com/hadamrd/forge-loop forge-loop

# in any repo where you want the loop to operate
cd my-repo
forge-loop init                                       # forge-loop.yaml + manual stub
gh label create loop:ready    --color FFD700
gh label create loop:blocked  --color d93f0b
gh label create loop:halt     --color cf222e
```

Edit `forge-loop.yaml` and set `repo.github` to `owner/repo`. Set
`deploy.task` to your project's redeploy command (or leave empty if you
don't auto-deploy). Commit it.

### Your first ticket (the easy mode)

Write an issue body that looks like this:

```markdown
## Problem
Users reporting that the `/admin/users` page crashes on first paint when the
backend returns a user with `lastLogin: null` — the column renderer assumes
the date is always present.

## Acceptance criteria
- Add a `null`-guard around the `lastLogin` rendering in `src/routes/admin/users.tsx`
- The column shows "—" (em-dash) when `lastLogin` is null
- Existing tests still pass
- New test: navigate to `/admin/users` with a synthetic null-lastLogin user,
  assert the page renders without a console error

## Test matrix
- unit: `users.tsx` snapshot when lastLogin=null → em-dash visible
- e2e: Playwright spec navigating to /admin/users with a seeded null user

## Out of scope
- Refactoring how dates are rendered elsewhere (separate ticket)

## File pointers
- src/routes/admin/users.tsx
- src/test/admin-users.test.tsx (new)
- e2e/specs/admin-users-null-lastlogin.spec.ts (new)
```

Then label and start:

```bash
gh issue edit 42 --add-label loop:ready
forge-loop run                                        # foreground, Ctrl-C to stop
```

You'll see, in ~10–15 minutes:

- A new PR open with the fix + the two tests
- A critic comment summarising the review
- The PR auto-merged (if critic approved)
- The next tick going idle

That's it. **The loop just shipped a typed fix with tests on a real codebase, unattended.**

### Your first hard ticket

Easy-mode tickets ship cleanly because their acceptance criteria are
unambiguous. Hard tickets — refactors, integrations, new subsystems —
ship cleanly when **you spend two more minutes writing the spec**. The
PO pass rewrites thin bodies, but it can't invent intent. Compare:

**Bad** (loop will ship one-line trivia):
> Make the auth flow cleaner.

**Good** (loop will ship a multi-file refactor with tests):
> ## Problem
> `AuthProvider.tsx` mixes OIDC redirect handling, session refresh, and
> per-route guards. The OIDC refresh logic specifically (lines 120–180)
> uses an interval timer that races with the route guard's redirect — we
> see ~5% flake in `test_auth_session_refresh.spec.ts`.
>
> ## Acceptance criteria
> - Extract OIDC refresh into `src/auth/refresh.ts` as a pure function
>   `refreshIfStale(session, now)` returning `{ session, redirected: bool }`.
> - `AuthProvider` calls this in a `useEffect` whose dep array includes
>   only the session id (not the timer tick) — eliminates the race.
> - Existing per-route guard behaviour unchanged.
> - The flake reproduction added in `auth-session-refresh.spec.ts` must
>   pass 10× in a row.
>
> ## Test matrix
> - unit: `refresh.ts` pure-function happy path + stale token + already-stale
> - integration: AuthProvider with mocked clock advances correctly
> - e2e: the existing flake spec, run 10×
>
> ## Out of scope
> - Refactoring per-route guards (separate ticket)
> - Migrating away from the OIDC redirect flow (much bigger ticket)
>
> ## File pointers
> - src/auth/AuthProvider.tsx
> - src/auth/refresh.ts (new)
> - src/test/auth-refresh.test.ts (new)
> - e2e/specs/auth-session-refresh.spec.ts

This is the shape of ticket that ships clean. ~3 minutes to write. The
loop produces a real refactor with falsifiable tests against it.

### Watching it work

```bash
# in another terminal
forge-loop events -n 20                    # last 20 events, colored
forge-loop doctor                           # health check
forge-loop status                           # state file dump

# deepest: per-worker activity stream
tail -f docs/ops/loop-runner-logs/worker-42-*.log

# or via MCP if you've added forge-loop to Claude Code:
# ask Claude: "what's worker 42 doing?"
# Claude calls worker_logs(issue=42, kind_filter="tool_use", tail=10)
```

---

## 3. The discipline matters more than the cleverness

forge-loop's edge isn't that it can write code (Claude does that). It's
that the loop refuses to do the wrong thing:

- **Fingerprint cooldown** — never re-attempts an issue within an hour
  of a failure, never duplicates an in-flight PR
- **Closed-issue merge gate** — if you close the source issue while a
  worker is running, the loop refuses to land the PR
- **Drift detector** — 3 consecutive identical failures halt the loop
  and file a `loop:halt` issue so you wake up to a clear signal
- **Typed critic** — sev1 findings block auto-merge; the rubric is
  defined in the critic prompt
- **Attempts ledger** — every dispatch is recorded as a GH issue comment
  so future workers see prior tries' notes
- **Watchdog** — kills workers idle > 30 min; wall ceiling at 2 hours
- **Orphan worktree reaper** — stale `/tmp/wt-loop-*` paths cleaned at boot
- **Auto-restart on self-upgrade** — if a merged PR bumps forge-loop's
  own version, the running process exits cleanly and the all-nighter
  shim re-execs against the fresh install

You can run the loop overnight and wake up to either (a) merged PRs, or
(b) a clear halt with a labeled `loop:halt` issue explaining why.
**There is no silent failure mode by design.**

---

## 4. Manifestos: drive what gets built (not just how)

Out of the box, the loop will ship whatever ticket you label `loop:ready`. That's fine for a hobbyist run — but it's also how backlogs drift toward cosmetic features (sparklines, ETag headers, theme polish) that ship effortlessly and add zero customer value.

Manifestos are how you tell the loop **what should exist**, not just how to build it.

### The four files

Drop these in your repo at `.forge/`:

| File | Owns |
|---|---|
| `product-vision.md` | Prose: who you serve, the golden path, the wedge, what's NOT valuable |
| `axes.yaml` | Structured: 4-6 value axes with customer, acceptable_work, rejected_as_cosmetic |
| `quality-manifesto.md` | Hard rules: how code MUST be written. **Critic enforces — sev1 blocks auto-merge.** |
| `testing-manifesto.md` | Hard rules: how tests MUST be written. Worker reads after impl, before push. |

Every shipped ticket cites its axis. Every PR is gated by the manifestos.

### Bootstrapping a project

```bash
# 1. Author the four files (steal from the seed examples in this repo)
$EDITOR .forge/product-vision.md
$EDITOR .forge/axes.yaml
$EDITOR .forge/quality-manifesto.md
$EDITOR .forge/testing-manifesto.md

# 2. Dry-run the brainstormer — it proposes axis-aligned epics + tickets
GH_TOKEN=$(gh auth token) forge-loop brainstorm

# 3. Apply: file them on GitHub with axis labels + customer-story citations
forge-loop brainstorm --apply

# 4. Dispatch — the loop only picks tickets that carry an axis label
forge-loop run
```

The brainstormer **refuses** to file a ticket that doesn't move an axis or that matches a `rejected_as_cosmetic` pattern. Example output:

```
brainstorm: filed:
  + #1114: EPIC: Real RBAC — role model, per-action gates, SSO mapping, audit
  + #1116: test(e2e): adversarial golden-path fixtures — failed step, secret-needing, OOM
  + #1117: feat(scm): Bitbucket Cloud — PR-comment status + line-level review parity
  + #1118: feat(scm): webhook reconcile loop — catch missed events on transient SCM outage
  + #1119: feat(pdl): real-shaped fixture — Node app with lint+test+build+deploy
  + #1121: feat(rbac): permission check helper + enforce on /api/v1/builds re-run (smallest enforceable slice)
```

Notice the brainstormer split RBAC into an epic plus a "smallest enforceable slice" — it knows multi-day work is unshippable in one tick.

### The feedback loop

Every bug → manifesto update → permanent gate.

```bash
# A bug shipped and got fixed in PR #N.
# Propose what manifesto rule would have prevented it:
forge-loop manifesto suggest --from-pr <N>

# Review the proposal, commit if good. The critic enforces it from
# the next worker run.
```

Real example: PR #147 hot-fixed a stringly-typed event-boundary bug (a four-PR train of identical-shape bugs preceded it). The quality manifesto gained `No stringly-typed cross-module discriminators — sev1`. Any future PR that compares `event["kind"] == "literal"` across module boundaries now gets auto-blocked by the critic.

### What the worker sees

Before writing code, every worker dispatch loads:
- The product vision (so the worker writes value-aligned commits + PR descriptions)
- The quality manifesto (so the impl follows project conventions)
- The testing manifesto (consulted POST-implementation, BEFORE push)

The critic loads the same set + the proposed diff. Sev1 manifesto violations are blocking review comments, not nits.

---

## 5. The brief is your contract

Out of the box, the worker brief tells Claude to:

- Read the issue spec carefully (ACs / Test matrix / Out of scope / File pointers)
- Write happy + adversarial tests
- Discover related tests via Lumen semantic search (capped at 4 invocations)
- Pass pre-commit gates
- Commit with a real "why" body, push, open PR, enable auto-merge

For your project, **fork this brief**. Drop a `.forge-loop/briefs/worker.md.tmpl`
in your repo telling the worker:

- Which `cat docs/X.md` reads are non-negotiable (CONSTITUTION, CLAUDE.md, design docs)
- The exact build command (e.g. `./gradlew -p <module> test --tests <Class> --no-daemon -Xmx1500m` on WSL — the WSL-OOM guard is a real concern)
- Forbidden patterns (Jenkins imports if you're post-Jenkins; `window.confirm` in styled UIs; plaintext secrets)
- The architecture invariants you'd flag in code review (discriminated-union typed config, pull-based workers, OIDC + PKCE, etc.)

Same for `.forge-loop/briefs/po.md.tmpl`. A Titan-grade PO brief looks like
[the one used by the Titan engine](https://github.com/hadamrd/dashboard-plugin/blob/trunk/.forge-loop/briefs/po.md.tmpl).

---

## 6. Cost and economics

Observed on Opus 4.7 with subscription billing (Max plan, no per-token charge):

| Ticket size | Cost | Time | Outcome |
|---|---|---|---|
| Bug fix, single file, 2 tests | $3 | 7 min | Always merges |
| Refactor, 4 files, 5 tests | $5–8 | 15 min | Usually merges |
| New module + tests | $8–12 | 20 min | Usually merges |
| Cross-cutting refactor (>1000 LOC) | $9–15 | 25 min | Risk of self-supersede if parallel races |

A typical evening shipping 8–15 PRs costs around **$30–60** in API spend
(if you weren't on subscription). On the Claude Max subscription that's
all flat.

The waste mode is **parallel-overlap**: when two workers both target
the same file, one ships and the other burns $5–9 producing a duplicate
PR it then self-closes. To avoid this, the PO pass dedupes, but operator
discipline (close obvious duplicates as soon as you see them) matters.

---

## 7. When things go wrong

### Loop self-halted with a `loop:halt` issue

```bash
cat docs/ops/loop-runner.HALT             # the reason is line 1
forge-loop events -n 30                   # last 30 events for context
# fix the root cause (deploy script, gh auth, …)
rm docs/ops/loop-runner.HALT
forge-loop run                            # back up
```

### Worker stuck — log hasn't moved in 10+ min

Don't panic. Opus extended thinking can sit quietly for 5+ min between
log writes. The watchdog will SIGTERM at 15 min idle, SIGKILL at 30 min.
Wall ceiling is 2 hours.

To kill earlier: `tmux attach -t loop`, Ctrl-C, then restart.

### Critic merged garbage

Rare but possible — usually a critic prompt issue. Inspect:

```bash
# what did the critic say?
sqlite3 :memory: <<EOF
.mode column
SELECT ts, kind, json_extract(payload, '$.verdict') as verdict, json_extract(payload, '$.reasons') as reasons
FROM read_ndjson('docs/ops/loop-runner-events.jsonl')
WHERE kind = 'critic_done'
ORDER BY ts DESC
LIMIT 10;
EOF
```

If the critic's prompt isn't catching a recurring bug class, tighten
`.forge-loop/briefs/critic.md.tmpl` with an explicit "this category
gets sev1" rule.

### Multiple PRs on the same surface

```bash
gh pr list --state open                                   # see all
gh pr close <N> --delete-branch                          # close the duplicate
gh issue comment <I> --body "Superseded by PR #M"        # mark the source issue
```

The loop's self-supersede pattern (worker discovers trunk already has
the fix, closes its own PR as `failed: superseded`) is the friendly
path. Manual close is the firm path when the worker doesn't notice.

---

## 8. Patterns observed across many runs

These come from dogfooding the loop on its own codebase + on the Titan
engine. They are real, not theoretical.

**Workers genuinely write good code on falsifiable-AC tickets.** The
PRs reviewed in the [dogfood retros](../docs/RETROS.md) consistently
include: dedicated new modules (not bloated existing ones), Protocol-
based dependency injection for tests, conservative-on-failure defaults,
schema-versioned dataclasses, falsifiable assertions, comments citing
the source issue.

**Workers don't write good code on subjective tickets.** "Clean up X"
or "improve performance" tickets produce plausible-looking but
direction-uncertain PRs. The PO pass can't rewrite "clean up X" into
a real spec without operator intent.

**Self-correction is real.** When the loop's own worker detects that
trunk already has the change it was working on, it closes its own PR
as "superseded" with a structured note. ~$9 of compute waste but no
duplicate merge — the system makes the right choice.

**Out-of-band operator actions need explicit handling.** If you close
an issue mid-flight, the worker doesn't know. The closed-issue merge
gate (#65) catches this at merge time. Other out-of-band actions
(re-labeling, re-titling) flow through without issue because the
worker's branch name is derived from the issue title at dispatch time.

---

## 9. Going further

- [README.md](../README.md) — the reference
- `forge-loop --help` — every subcommand
- `forge-loop config` — your resolved configuration
- `forge-loop brief --kind worker --issue 42` — the exact brief the
  loop would send for an issue, useful for tightening your project-
  specific overrides
- [hadamrd/dashboard-plugin](https://github.com/hadamrd/dashboard-plugin)
  — the Titan engine codebase that forge-loop was extracted from; the
  PR history is the best demo of what the loop can do on a real
  production-grade Java + React codebase

---

## 10. Final piece of advice

The loop is patient. It will tick idle for hours waiting for work.
There is no rush to "use up" your subscription quota or fill the queue.

The bottleneck is **your operator discipline**: writing tickets with
falsifiable acceptance criteria, reviewing the critic's verdicts when
something looks off, closing duplicates promptly. The loop does the
typing; you do the steering.

Treat it like an unattended CI that ships code instead of running tests
— and you'll spend the rest of your time on the work only a human can
do.
