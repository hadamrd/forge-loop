# Adversarial Review: Beyond the PR Bot

Review target:
[`paper/00-long-running-autonomous-software-engineering.md`](../00-long-running-autonomous-software-engineering.md)

## Review stance

The draft should be judged as a serious architecture paper, not as an
enthusiastic product note. The burden is to make the problem precise, avoid
inflated claims, distinguish prototype evidence from aspiration, and make the
proposed architecture testable.

## Findings

### Important: The paper could still overclaim the event-log solution

The event log is necessary for recovery, but not sufficient for cognition. A
log records what happened; it does not decide what matters. The current draft
now says this more clearly by separating event sourcing from curated memory,
but future revisions should keep resisting the temptation to treat "we have a
WAL" as equivalent to "we have durable project cognition."

Required next check: every time the paper says "memory", verify whether it
means raw history, projection, curated semantic fact, or active boot context.

### Important: The frontier cursor needs empirical validation

The frontier cursor is the strongest original idea in the draft, but it is
still a proposed construct. The paper should not imply it is proven. It needs
future evaluation criteria:

- can a fresh maestro resume useful planning from the cursor alone plus linked
  memory?
- does the cursor prevent repeated rediscovery of rejected paths?
- do operators agree that cursor moves are explainable?
- does cursor size remain bounded as the project grows?

### Important: Sandbox language must stay threat-model driven

MicroVMs, gVisor, and worktrees solve different problems. A future version
should avoid presenting any one isolation layer as universally correct.
The right claim is threat-model based: secrets, generated code execution,
untrusted dependencies, network egress, and MCP tool access each demand an
explicit capability boundary.

### Important: The forge-loop case study must remain humble

The paper is strongest when it treats forge-loop as a prototype that exposed
the architecture, not as proof that the architecture works. Current language
is mostly in that direction. Future edits should not reintroduce the old
"the gates are the contribution" framing.

Concrete evidence worth preserving:

- issue-driven dispatch exists;
- worktree-per-worker exists;
- critic and quality gates exist;
- status/audit surfaces exist;
- current quality gates are not fully green, which proves the enforcement
  story is incomplete.

### Medium: Literature references are currently a map, not yet scholarship

The references point at relevant work, but the body does not yet do careful
comparative analysis. That is acceptable for this draft, but not final-paper
quality. The next serious citation pass should add short, precise claims:

- MemGPT: context as managed memory tier;
- CoALA: memory taxonomy;
- SWE-agent: agent-computer interface;
- OpenHands: event-stream/runtime precedent;
- Temporal/Sagas: durable execution and compensation;
- Reflexion/Self-Refine/ReAct/Voyager: feedback and procedural memory.

Avoid exact benchmark numbers unless independently verified in the same pass.

### Medium: "Maestro" may sound mystical without stricter definition

The paper now defines the maestro as a single-writer deterministic state
machine, which helps. Future versions should keep using that operational
definition. If "maestro" starts meaning "smart agent that thinks deeply", the
paper becomes vague again.

### Medium: Governance gates need executable criteria

The rewritten section correctly says gates are only real if wired into
dispatch, review, and merge. The next version should give examples of failed
gates:

- axis exists but no dispatch filter reads it;
- manifesto exists but worker and critic do not load it;
- critic comments but merge gate ignores severity;
- bug rule is written but no check can observe violations.

### Minor: The title is strong but maybe too colloquial

"Beyond the PR Bot" is memorable and usefully dismisses the shallow framing.
For a more formal publication, consider:

- "Durable Cognition for Autonomous Software Engineering"
- "Event-Sourced Control Planes for Long-Running Coding Agents"
- "From Patch Generation to Repo Evolution"

## Recommended next pass

1. Add a compact diagram or textual architecture table.
2. Add a precise glossary for event log, projection, curated memory, frontier
   cursor, maestro, worker, critic, and sandbox manager.
3. Add a small forge-loop case-study box that names what exists and what is
   missing.
4. Add a citation pass with only claims that can be sourced cleanly.
5. Add an evaluation section: reset recovery, memory drift, sandbox cost,
   semantic patch correctness, operator explainability.

## Current assessment

The draft is now a serious architecture argument, but it is not yet a final
paper. Its strongest contribution is the combination of event-sourced control
plane, curated memory, frontier cursor, and disposable sandboxed workers. Its
main risk is still rhetorical inflation: claiming more proof than the
prototype provides.
