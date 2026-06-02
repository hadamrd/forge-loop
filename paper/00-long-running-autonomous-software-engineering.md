# Beyond the PR Bot

## Toward long-running autonomous software engineering systems

### Abstract

Autonomous coding agents can produce useful patches, but patch generation is
not the same as autonomous software engineering. Long-running repo evolution
fails when project cognition has nowhere durable to live: sessions reset,
context windows saturate, local worker traces crowd out strategic state, and
the system forgets which approaches were tried, rejected, or proven. This
paper argues for a stricter architecture: an event-sourced control plane, a
single-writer maestro, curated project memory, an explicit frontier cursor,
and disposable sandboxed workers. `forge-loop` is used as a working prototype
and cautionary artifact. It demonstrates useful primitives such as
issue-driven dispatch, worktree isolation, critic review, and quality gates,
but it also exposes the missing layers required for durable autonomy:
replayable state, memory promotion, sandbox capability control, and
hierarchical decision-making.

The useful patch is the wrong unit of analysis. Real software products evolve
across weeks and months. The repository is large. The codebase has history.
The important choices are not only what changed, but why it changed, what was
tried before, what failed, who rejected which direction, what the current
product frontier is, and which parts of the system are likely to matter next.

A prompt context cannot hold that. It should not try.

This paper argues that long-running autonomous software engineering should be
understood as a durable control-system problem, not as a code-generation
problem. The unit of design is not the agent that writes a patch. It is the
system that preserves project cognition across resets, dispatches isolated
workers, observes and replays what happened, promotes useful facts into
memory, forgets task noise, and resumes from an explicit frontier cursor.

`forge-loop` is an early artifact in this direction. It can pick up issues,
open worktrees, dispatch workers, run a critic, and gate merges. Those are
useful primitives. They do not yet amount to a mature long-running autonomous
engineering system. The current implementation is better read as a small
prototype that exposes the deeper architecture required: event-sourced state,
hierarchical cognition, curated memory, sandboxed execution, and reliable
recovery.

The contribution of the prototype is not that it solved autonomous software
development. It did not. Its contribution is that it makes the missing system
visible.

The argument is intentionally falsifiable. If durable memory does not improve
reset recovery, if single-writer orchestration cannot preserve coherence under
parallel execution, if sandboxing costs dominate the benefit of delegation, or
if stronger review oracles still admit semantically wrong patches at an
unacceptable rate, then this architecture is incomplete. The point of the
paper is not to declare victory. It is to make the architecture concrete
enough to test.

## 1. The failure mode is amnesia

The first generation of autonomous coding tools framed the problem as:
"given an issue, can an agent produce a patch?" Benchmarks such as SWE-bench,
projects such as SWE-agent and OpenHands, and commercial systems in the same
family have shown that this is possible often enough to be useful. The next
failure mode is subtler.

An agent can write a correct patch and still participate in a system that is
forgetting what it is doing.

The loss is not only factual. It is cognitive:

- the product frontier is no longer explicit;
- decisions lose their rationale;
- rejected approaches are rediscovered and retried;
- worker transcripts drown strategic memory;
- local task success is mistaken for product progress;
- session resets erase the chain of reasoning that made the last direction
  make sense;
- the repository becomes a pile of patches rather than a coherent product
  evolution.

This is the central problem. A long-running autonomous engineering system
must preserve high-level project cognition while making low-level task context
disposable.

The distinction matters because the two kinds of context have opposite
lifecycles. A worker's scratch context should be cheap, noisy, and temporary:
searches, failed edits, terminal output, local hypotheses, test retries. The
maestro's context should be compact, curated, and durable: product vision,
frontier, decisions, rejected paths, invariants, known risks, hot files, hot
tests, and external state of the art.

Confusing these two layers is how systems rot. Saving every transcript creates
sludge. Saving nothing creates amnesia. The architecture needs a memory
promotion policy between the two.

## 2. Context is working memory, not state

The context window is a working-memory tier. It is not the source of truth.
Long-context models reduce the pain of retrieval, but they do not eliminate
the need for state management. Empirical work such as "Lost in the Middle"
shows that information placement inside long contexts affects recall. Systems
such as MemGPT make the stronger architectural point: useful long-term memory
must live outside the active context and be paged in deliberately.

This gives the first design rule:

> Durable project cognition must live outside the model context, in an
> append-only event log plus curated memory projections.

The context window should be rebuilt on boot. It should contain only the
current working set: the product frontier, active decisions, relevant
constraints, selected memories, and the task at hand. Everything else should
be fetched, summarized, or ignored.

This is not a convenience. It is the condition for session reset recovery. If
the system cannot reconstruct its own strategic state from external durable
artifacts, it is not long-running. It is merely a sequence of unrelated
agentic episodes.

The useful memory taxonomy is close to CoALA's distinction between working,
episodic, semantic, and procedural memory:

- **Working memory** is the current context window.
- **Episodic memory** records task histories, failures, incident narratives,
  and what happened during prior runs.
- **Semantic memory** stores project facts, decisions, product constraints,
  rejected ideas, architecture invariants, and repo topology.
- **Procedural memory** stores reusable skills: repair recipes, test
  procedures, refactor playbooks, and verified command sequences.

The system should not blindly promote all observations. It should ask: does
this fact change future behavior? Would forgetting it cause repeated work,
bad decisions, or unsafe execution? If yes, promote it. If not, archive or
discard it.

Every durable memory item should carry provenance. At minimum: source event,
authoring role, timestamp, confidence, supersession status, and links to the
files, tests, issues, or external sources that justify it. Memory without
provenance becomes folklore. Folklore is worse than forgetting because it
feels authoritative while being hard to challenge.

This is where the LSTM analogy is useful, but only as an engineering metaphor.
A mature agent system needs input gates, forget gates, and output gates:

- an input gate that decides what new information becomes durable memory;
- a forget gate that marks old memories as superseded, stale, or archived;
- an output gate that decides what a booting agent actually receives.

The point is not to imitate neural architecture. The point is to treat memory
as a controlled state transition, not as a growing notes folder.

## 3. The event log is the spine

Autonomous repo work is a long-running transaction. It spans issues, branches,
worktrees, tool calls, tests, reviews, PRs, retries, abandoned attempts,
compensations, memory updates, and follow-up tasks. A chat transcript is the
wrong representation for this. The right representation is an append-only
event log.

Every durable transition should be recorded:

- `VisionUpdated`
- `DecisionMade`
- `IdeaRejected`
- `FrontierAdvanced`
- `TaskPlanned`
- `TaskDispatched`
- `WorkerHeartbeat`
- `WorkerObservation`
- `TaskCompleted`
- `TaskFailed`
- `TaskCompensated`
- `CritiqueIssued`
- `MemoryPromoted`
- `CompactionPerformed`
- `PullRequestOpened`
- `PullRequestMerged`

All important state should be a projection from that log: the decision ledger,
the rejected-ideas register, task state, frontier cursor, memory index, and
operator status. If the process dies, the system replays the log and rebuilds
its projections. If the history grows too large, the system snapshots and
continues with a bounded new epoch.

The log needs a durability contract, not just a file called `events.jsonl`.
Each event should have a stable type, monotonic sequence, causal parent when
applicable, task or saga identifier, idempotency key for external effects, and
schema version. Projections should record which event sequence they were built
from. A task cannot be considered terminal until both the task event and any
required compensation or cleanup event have been recorded. Without this
contract, replay is theater: the system has a history, but not one it can
trust.

This is the same family of ideas as event sourcing and durable execution. It
also mirrors the practical design of systems such as Temporal: workflows are
recoverable because durable history records what happened, while side effects
are treated as activities whose results are captured. The lesson for agentic
systems is direct: LLM calls, shell commands, GitHub mutations, and MCP tool
invocations are nondeterministic activities. The maestro should not assume it
can replay them from scratch and get the same result. It should record their
results as events.

This is the second design rule:

> The maestro should be a deterministic state machine over a durable event log;
> nondeterministic agent and tool work should be recorded as activities.

Once this exists, session reset is no longer a tragedy. It is a boot protocol.

## 4. The frontier cursor

The most important memory object is the frontier cursor.

A frontier cursor is the compact state that tells the system where product
evolution should expand next. It is not a backlog. A backlog is a set of
possible tasks. The cursor is a theory of motion: what matters now, why it
matters, what was just learned, and what should be loaded before acting.

A useful cursor contains:

```yaml
frontier:
  product_goal: "What larger product objective this frontier serves."
  current_problem: "The active problem the maestro is expanding."
  next_expansion: "The next likely feature, proof, or cleanup."
  why_now: "Why this is the right frontier instead of other backlog items."
  active_decisions:
    - decision: "..."
      rationale: "..."
  rejected_paths:
    - idea: "..."
      reason: "..."
      revisit_if: "..."
  hot_files:
    - path: "src/..."
      why_hot: "Likely to be needed by next workers."
  hot_tests:
    - command: "..."
      why_hot: "Validates the current frontier."
  open_questions:
    - "..."
  external_state_of_art:
    last_checked: "..."
    sources:
      - "..."
```

The cursor should be small enough to fit in every maestro boot context and
specific enough to prevent aimless rediscovery. It is the antidote to the
reset problem.

The cursor also needs update rules. It should advance only when evidence
changes the product frontier: a task lands, a proof fails, an external source
changes the state of the art, a blocker appears, or a prior rejection becomes
revisitable. If every worker can nudge the cursor, it degenerates into backlog
chatter. If nobody can nudge it, the system preserves stale strategy. The
maestro owns the cursor; workers may only propose cursor updates with evidence.

On startup, the maestro should not "read the repo" in the abstract. It should
execute a boot protocol:

1. Replay or restore the event log.
2. Rebuild memory projections.
3. Load the frontier cursor.
4. Load the relevant decision and rejection ledgers.
5. Reconcile in-flight tasks.
6. Load hot files, hot tests, and hot indexes.
7. Check current Git, issue, and PR state.
8. Only then plan the next action.

This converts boot from a vague context-gathering ritual into a deterministic
reconstruction of project cognition.

## 5. Hierarchy is not optional

Flat swarms are the wrong default for long-running repo evolution. Peer agents
with independent contexts can explore quickly, but they also make hidden
decisions. Two workers can choose incompatible abstractions, edit overlapping
areas, interpret the product direction differently, or each solve a local
version of a global problem. The issue is not that agents cannot collaborate.
The issue is that durable decision-making cannot be dispersed without a
coherence mechanism.

The system needs hierarchy:

- **Maestro**: owns product frontier, durable decisions, dispatch policy, and
  memory promotion.
- **Planner or architect**: turns frontier into scoped tasks and acceptance
  criteria.
- **Workers**: execute bounded tasks in isolated environments and return
  evidence.
- **Critics and enforcers**: review correctness, tests, architecture,
  security, and policy.
- **Memory curator**: promotes signal, archives noise, records rejected
  ideas, and maintains indexes.
- **Control plane**: manages leases, heartbeats, retries, timeouts, sandbox
  lifecycle, replay, and operator visibility.

The rule is simple:

> Parallelize exploration and execution. Single-thread durable decisions.

Workers may search, edit, test, and propose. They should not directly rewrite
the product memory, advance the frontier, or make irreversible strategic
decisions. Their outputs should be structured artifacts: patch, tests run,
observations, risks, changed files, and suggested memory updates. The maestro
or curator decides what becomes durable.

This preserves the benefits of sub-agents without turning the system into a
collection of competing hidden contexts.

This is not an argument against all multi-agent systems. It is an argument
against concurrent durable decision-makers. Parallel readers are useful.
Parallel writers are dangerous unless their write sets are disjoint and their
decisions are reconciled by a single authority. In large repositories, the
implicit decisions hidden inside edits are often more consequential than the
explicit messages agents send to each other.

## 6. Workers are disposable by design

A worker should start with only the context required for its task. It should
receive:

- a scoped objective;
- relevant acceptance criteria;
- allowed files or directories when possible;
- hot memory excerpts selected by the maestro;
- tool and MCP permissions;
- explicit output contract.

It should operate in a separate worktree or clone. For serious use, that
filesystem boundary should be backed by stronger sandboxing: gVisor,
Firecracker, Kata, or an equivalent isolation layer depending on the threat
model. Plain Docker is useful for convenience, but it shares the host kernel
and should not be treated as a sufficient boundary for untrusted generated
code, secrets, or arbitrary tool execution.

The threat model should be explicit. A worker may execute generated code,
download dependencies, invoke project tools, read logs, or call MCP servers.
Any of those actions can expose secrets, mutate shared state, consume budget,
or attack the host. The default policy should therefore be deny-by-default:
scoped filesystem, scoped network, scoped credentials, scoped MCP tools, and
an audit trail for every privileged action. Capability grants should be part
of the task specification, not ambient properties of the machine the worker
happens to run on.

The worker output should be condensed:

```yaml
worker_result:
  task_id: "..."
  status: "completed|failed|blocked"
  patch_ref: "..."
  tests_run:
    - command: "..."
      result: "pass|fail"
  observations:
    - "..."
  risks:
    - "..."
  proposed_memory:
    - kind: "decision|repo_fact|rejected_path|procedure"
      content: "..."
  next_actions:
    - "..."
```

The raw worker context can be discarded. If it matters, it should have been
emitted as an observation and promoted. This is how the system prevents
low-level task noise from polluting high-level cognition.

## 7. Tasks are sagas

Every dispatched task is a saga: a sequence of operations with possible
compensations. Create worktree. Create branch. Edit files. Run tests. Push.
Open PR. Request review. Merge. Clean up. Any of these steps can fail, stall,
or become obsolete.

A reliable control plane must therefore track:

- task lease;
- worker heartbeat;
- sandbox identity;
- branch and worktree;
- latest observed progress;
- retry budget;
- compensation action;
- terminal state.

Examples of compensation:

- remove an unused worktree;
- abandon a branch;
- close a draft PR;
- remove a stale label;
- requeue a task after a crashed worker;
- quarantine a workspace for human inspection.

This is mundane infrastructure, but it is not optional. Without it, the system
will leak worktrees, leave stale PRs, re-run obsolete tasks, lose partial
state, or burn tokens in loops. The intelligence of the model does not fix a
bad control plane.

Stuck detection should be built in from the beginning:

- no-progress timers;
- maximum turns;
- cost budgets;
- repeated-command detection;
- heartbeat expiry;
- failed-test repetition detection;
- task-level cancellation.

The system should assume agents will stall. Recovery is an architectural
feature, not an operator afterthought.

## 8. Governance gates are necessary, not sufficient

The existing forge-loop paper framed the central contribution as governance:
value axes, critic gates, quality manifestos, and the bug-to-rule ratchet.
That is a useful subsystem. It is not the whole system.

Governance gates answer these questions:

- Is this work worth doing?
- Does this patch satisfy quality rules?
- Did this failure teach us a reusable constraint?
- Should this PR merge?

They do not answer:

- What should the system remember after reset?
- Which rejected ideas must not be retried?
- How should the frontier advance?
- How do workers get isolated capabilities?
- How does the system replay after a crash?
- How does stale task state get compensated?
- How does the maestro avoid loading transcript sludge?

The mature framing is therefore:

> Governance gates are enforcers inside a larger event-sourced, hierarchical
> agent operating system.

They remain important. The value axis prevents value-blind dispatch. The
critic prevents some bad patches. The quality manifesto makes tacit standards
explicit. The ratchet can convert escaped failures into future constraints.
But none of these mechanisms preserves strategic continuity by itself.

The gates should be kept. The theory should be widened.

The adversarial test for any gate is whether it changes behavior under
pressure. A value axis that does not block low-value tickets is decoration. A
critic that cannot stop a merge is commentary. A quality manifesto that is not
checked by the same task runner that lands code is aspiration. A ratchet that
records rules but does not catch repeats is a diary. The paper should treat
gates as executable control surfaces only when they are wired into dispatch,
review, and merge decisions.

## 9. What forge-loop currently proves

`forge-loop` is valuable precisely because it is incomplete. It shows which
primitive loops are useful and which missing layers become painful.

It demonstrates:

- issue-driven dispatch;
- worktree-per-worker execution;
- a critic gate;
- labels and PR state as external coordination;
- operator status surfaces;
- quality and audit ideas;
- the ability to dogfood the system on its own backlog.

It does not yet demonstrate:

- durable event log as source of truth;
- deterministic maestro replay;
- robust session reset boot protocol;
- frontier cursor;
- curated semantic, episodic, and procedural memory;
- memory promotion and forgetting policy;
- serious worker sandboxing and capability control;
- reliable saga compensation for every task;
- state-of-art research as part of planning;
- strong semantic test oracles;
- clean separation between strategic and worker context.

The current artifact also shows why functional progress is not enough. A
repository can have many passing tests while still failing its own quality
contract: formatting gates can drift, type-checkers can disagree, orchestration
files can grow too large, and speculative subsystems can accumulate faster
than they are exercised. Those are not cosmetic flaws in an autonomous system.
They are evidence that the control plane and memory plane are not yet strong
enough to enforce the architecture they describe.

This is not a failure of the prototype. It is the reason the prototype is
useful. The first loop exposed the second-order problems.

The right way to write about forge-loop is not:

> Here is the architecture of an autonomous software factory.

It is:

> Here is a toy autonomous software factory that taught us what a real one
> must contain.

That is a stronger and more honest claim.

## 10. A pragmatic reference architecture

A mature system should be built in this order.

First, build the event log and projections. Define the durable event schema.
Make decision ledger, rejected-ideas register, task state, and frontier cursor
rebuildable from replay. If this layer is weak, every later feature inherits
amnesia.

Second, implement the maestro as a deterministic state machine over that log.
All LLM calls, GitHub calls, shell commands, and MCP calls become recorded
activities. The maestro owns durable writes. It is allowed to be slow and
careful because it is preserving coherence.

Third, build the boot protocol. Hard-kill the process repeatedly and require
it to reconstruct the same frontier, decisions, in-flight tasks, and memory
state. If boot cannot recover cleanly, the system is not long-running.

Fourth, build one excellent worker before scaling to many. Give it an
agent-computer interface optimized for repository work: scoped search, paged
file reads, structured edits, lint-on-edit feedback, test selection, and
compact reporting. Bad tools waste more intelligence than bad prompts.

Fifth, isolate the worker. Start with worktrees, then add real sandboxing,
capability allowlists, secret isolation, and network policy. A worker with
unbounded shell, network, and secrets is not autonomous infrastructure. It is
ambient risk.

Sixth, add critics and enforcers. They should run tests, review architecture,
check security, detect secrets, inspect changed files, and challenge weak
evidence. Existing test suites are not enough; many plausible patches pass
available tests while being semantically wrong.

Seventh, build the memory curator. It reads worker observations and event
history, promotes durable facts, records decisions and rejected paths, updates
the frontier cursor, and archives noise. Its job is not to summarize
everything. Its job is to preserve what changes future behavior.

Only after these pieces exist should broad parallelism be trusted.

This order is intentionally conservative. It is tempting to add more workers
first because parallelism looks like progress. But parallelism multiplies the
number of partial states, hidden assumptions, cleanup obligations, and memory
promotion decisions. If the event log, boot protocol, and curator are weak,
parallelism amplifies incoherence.

## 11. What remains open

Several problems remain unsolved in public systems and should be treated as
research risks, not implementation details.

**Memory promotion.** Which facts deserve durability? Importance scoring is
not enough. The system needs behavioral criteria: would forgetting this fact
cause repeated work, unsafe behavior, or a wrong frontier decision?

**Forgetting and supersession.** Memories become stale. Rejected ideas may
become valid when constraints change. The memory system needs versioning,
supersession, and revisit conditions.

**Compaction safety.** Summaries can drop load-bearing details. A serious
system needs decision checksums or invariants that verify compaction preserved
active decisions, constraints, and unresolved risks.

**Contradiction handling.** New evidence will contradict old decisions. The
system needs an explicit way to reopen, amend, or retire decisions without
silently forking reality.

**Semantic correctness.** Passing tests is not proof of correctness. Stronger
oracles, mutation testing, property checks, adversarial tests, and critic
review all help, but none fully solves the problem.

**External research.** A maestro that only reads its own repo will develop
tunnel vision. Strategic planning should include state-of-art scans and
competitor/tool research, with citations promoted into memory when they affect
architecture.

These are the hard problems. They are also the interesting ones.

## 12. What would falsify this architecture

This architecture should not be defended as taste. It should be exposed to
failure tests.

It is weakened if:

- replay cannot reconstruct the same frontier, decisions, and in-flight tasks
  after a hard kill;
- compaction drops decisions that later matter;
- workers repeatedly rediscover rejected ideas despite the rejected-ideas
  register;
- the maestro becomes a bottleneck that prevents useful throughput even after
  read-only exploration is parallelized;
- sandboxing overhead makes routine tasks uneconomical;
- critic and test oracles still admit a high rate of semantically wrong
  patches;
- memory promotion creates more stale authority than useful continuity;
- operators cannot understand why the frontier moved.

The right response to these failures is not rhetorical defense. It is
measurement: reset-recovery tests, replay checksums, worker-sandbox cost
benchmarks, strengthened test oracles, memory-drift audits, and operator
explainability reviews.

## 13. Conclusion

The future of autonomous software engineering is not a better PR bot. It is a
long-running, event-sourced, memory-bearing control system for repo evolution.

The model writes code. The worker executes tasks. The critic enforces local
quality. But the system succeeds or fails at a higher level: whether it can
remember why it is acting, preserve the product frontier, recover from reset,
reject stale ideas, isolate disposable workers, and turn every run into
evidence that improves the next run.

Forge-loop is a useful first artifact because it already contains the seed of
this loop: issue dispatch, worktree isolation, critic review, and operator
status. Its current limitations are not embarrassing side notes. They are the
map of the next architecture.

The mature claim is therefore modest and stronger:

> Autonomous software engineering becomes credible when generation is
> subordinated to durable state, curated memory, reliable orchestration, and
> controlled execution.

Everything else is just a patch generator.

## References

- Packer et al., "MemGPT: Towards LLMs as Operating Systems",
  <https://arxiv.org/abs/2310.08560>
- Sumers et al., "Cognitive Architectures for Language Agents",
  <https://arxiv.org/abs/2309.02427>
- Park et al., "Generative Agents: Interactive Simulacra of Human Behavior",
  <https://arxiv.org/abs/2304.03442>
- Yang et al., "SWE-agent: Agent-Computer Interfaces Enable Automated
  Software Engineering", <https://arxiv.org/abs/2405.15793>
- Wang et al., "OpenHands: An Open Platform for AI Software Developers as
  Generalist Agents", <https://arxiv.org/abs/2407.16741>
- Hong et al., "MetaGPT: Meta-Programming for a Multi-Agent Collaborative
  Framework", <https://arxiv.org/abs/2308.00352>
- Wu et al., "AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent
  Conversation", <https://arxiv.org/abs/2308.08155>
- Shinn et al., "Reflexion: Language Agents with Verbal Reinforcement
  Learning", <https://arxiv.org/abs/2303.11366>
- Madaan et al., "Self-Refine: Iterative Refinement with Self-Feedback",
  <https://arxiv.org/abs/2303.17651>
- Yao et al., "ReAct: Synergizing Reasoning and Acting in Language Models",
  <https://arxiv.org/abs/2210.03629>
- Wang et al., "Voyager: An Open-Ended Embodied Agent with Large Language
  Models", <https://arxiv.org/abs/2305.16291>
- Liu et al., "Lost in the Middle: How Language Models Use Long Contexts",
  <https://arxiv.org/abs/2307.03172>
- Garcia-Molina and Salem, "Sagas",
  <https://dl.acm.org/doi/10.1145/38714.38742>
- Fowler, "Event Sourcing",
  <https://www.martinfowler.com/eaaDev/EventSourcing.html>
- Temporal documentation, "Events and Event History",
  <https://docs.temporal.io/workflow-execution/event>
- Anthropic, "Building effective agents",
  <https://www.anthropic.com/research/building-effective-agents>
- Anthropic, "How we built our multi-agent research system",
  <https://www.anthropic.com/engineering/multi-agent-research-system>
