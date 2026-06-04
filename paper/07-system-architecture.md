[← The Slop Daemon](./06-hunting-sloppy-patterns.md) · [Index](./README.md) · [Next: Limitations →](./08-limitations.md)

---

# 6. System Architecture: The Tick

> *How the three control surfaces compose into a single, repeating control
> loop.*

Sections 3–5 defended each control surface in isolation. This section shows
how they compose at runtime. The thesis of the composition is simple: the
three surfaces are not three features bolted together — they are three
*gates on a single pipeline*, positioned so that work must pass value,
then quality, then liveness checks before it can affect the world.

## 6.1 The loop, abstractly

The system advances in discrete **ticks**. Each tick is one pass of a
control loop that pulls candidate work, runs it through the agents, gates
the results, and lands what survives. Abstractly:

```
                ┌─────────────────────────────────────────────┐
                │                  TICK                        │
                │                                              │
   value gate   │   1. select admissible work                 │
   (Section 3)  │      └─ axis filter: only work that serves   │
                │         a declared value axis                │
                │                                              │
                │   2. (periodic) maintenance / grooming       │
                │      └─ dedupe, retitle, expand thin specs   │
                │                                              │
   generation   │   3. dispatch N workers in parallel          │
                │      └─ each in an isolated git worktree      │
                │      └─ brief carries the quality manifesto   │
                │         (Section 5: guidance at gen-time)     │
                │                                              │
   quality gate │   4. critic reviews each PR → typed verdict  │
   (Sections    │      └─ sev1 blocks; sev2/3 advise           │
    4 & 5)      │      └─ manifesto compliance enforced        │
                │                                              │
   liveness     │   5. merge gate                              │
   gate         │      └─ refuse if source issue closed,       │
                │         conflicts unresolved, etc.           │
                │                                              │
                │   6. land survivors; (optional) redeploy      │
                │   7. emit audit events; sleep; repeat ↺       │
                └─────────────────────────────────────────────┘
```

## 6.2 Why this ordering is the right ordering

The sequence is not arbitrary. Each gate is positioned to fail work **as
early and as cheaply as possible**, which is a core efficiency argument.

**Value gate first (cheapest).** Filtering by value axis happens before any
agent is dispatched — before a dollar of compute is spent. Rejecting
cosmetic work at selection time is free; rejecting it after an agent has
written 700 lines is expensive. Putting the value gate first means the
system never pays generation cost for work it would refuse to ship anyway.

**Generation in isolation.** Each worker runs in its own git worktree off
the base branch. This is the concurrency-safety argument: parallel agents
cannot corrupt each other's working state, and a failed agent leaves no
trace on the others. Isolation is what makes "dispatch N in parallel"
safe rather than a race condition.

**Quality gate after generation, before merge.** The critic runs on the
produced PR. This is the only correct place for it — you cannot review code
that does not exist yet, and you must not merge code that has not been
reviewed. Manifesto enforcement and the typed verdict both live here.

**Liveness gate last.** The merge gate checks conditions that can only be
known at the last moment: has the source issue been closed mid-flight? Are
there unresolved conflicts? These are *time-of-merge* facts; checking them
any earlier would be checking stale state. The merge gate is the system's
defense against acting on a world that changed while it was working.

## 6.3 The composition is the contribution

The individual gates are each defensible (Sections 3–5). The architectural
claim of this section is that **their composition is what produces the
Section 2 property.** Value-first selection bounds *what* the system spends
effort on; the quality gate bounds *how good* what it ships is; the
liveness gate bounds *whether the world still wants it*; the audit log and
the ratchet make the whole loop *improvable*. Remove any one gate and a
pathology from Section 1 returns:

| Remove this gate | Pathology that returns |
|------------------|------------------------|
| Value (axis filter) | Value-blindness — effort spread across worthless work |
| Quality (critic + manifesto) | Quality entropy — codebase decays change by change |
| Liveness (merge gate) | Acting on stale state — landing work the world abandoned |
| Audit + ratchet | Static failure set — same bugs recur forever |

The loop is a pipeline of gates, each cheap relative to the cost of the
failure it prevents, composed so that human judgment encoded on the three
control surfaces is enforced on every one of an unbounded number of
autonomous actions. That composition — not any single gate — is the design
this paper defends.

## 6.4 A note on what the architecture deliberately does *not* do

The loop does not try to make the agents smarter, and that is intentional.
It treats the generation engine as a fixed, fallible black box and invests
entirely in the *governance* around it. This is a bet that, as base models
improve, a system organized around durable control surfaces will compound
those improvements (better agents, same gates, strictly better outcomes),
whereas a system organized around clever prompting will have to be
re-engineered each model generation. The architecture is designed to age
well by refusing to depend on the thing that changes fastest.

---

[← The Slop Daemon](./06-hunting-sloppy-patterns.md) · [Index](./README.md) · [Next: Limitations →](./08-limitations.md)
