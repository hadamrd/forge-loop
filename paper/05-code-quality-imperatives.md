[← Reinforcement Feedback Loops](./04-reinforcement-feedback-loops.md) · [Index](./README.md) · [Next: The Slop Daemon →](./06-hunting-sloppy-patterns.md)

---

# 5. Code-Quality Imperatives

> *Control surface #3: turning "good code" from a matter of taste into an
> executable admission policy.*

## 5.1 Why quality must be a first-class control surface

The third pathology of Section 1 was *quality entropy*: each change passes
review in isolation while the aggregate codebase decays. This is not a
metaphor but a formalized property of software: Lehman's laws of software
evolution (1980) hold that an E-type system embedded in the real world must
be *continually* adapted or it becomes progressively less satisfactory, and
that its complexity *increases* unless work is actively done to reduce it
[1]. Entropy is the default; arresting it is the cost. The root cause of why
agents accelerate it is that "quality" normally lives as **tacit
knowledge** — the taste a senior engineer applies in review, never fully
written down. Tacit knowledge does
not scale to a machine workforce. An agent cannot consult a taste it was
never given, and a swarm of agents cannot converge on a consistency none of
them can see.

The only remedy is to **make the tacit explicit**: write quality down as
rules, and enforce those rules as a *gate*, not as a suggestion. This is
the same move as Sections 3 and 4 — encode human judgment once, enforce it
indefinitely — applied to the *how* of code rather than the *what* or the
*whether*.

## 5.2 The mechanism: manifestos + severity rubric

forge-loop separates quality into two operator-owned manifestos under
`.forge/`:

- **`quality-manifesto.md`** — how code *must* be written. Enforced by the
  critic; violations at sev1 block the merge.
- **`testing-manifesto.md`** — how tests *must* be written. Consulted by
  the worker after implementation, so the standard shapes the code as it is
  produced, not only after.

Two placement decisions matter:

**Quality is injected at *both* ends of the pipeline.** The relevant
manifesto content is prepended to the *worker's* brief (so the agent writes
to the standard) **and** enforced by the *critic* (so violations are caught
if the agent ignores it). Guidance at generation time plus enforcement at
admission time is strictly stronger than either alone: the first reduces
violations, the second guarantees they cannot ship.

**Manifestos are versioned and the version is recorded.** Each worker
outcome records which manifesto versions governed it. This is what makes
the ratchet of Section 4 auditable — you can answer "which standard was
this PR held to?" and "did this rule exist when that bug shipped?" Quality
becomes a tracked, evolving artifact rather than an ambient assumption.

## 5.3 Why "tight imperatives" is the right stance — and why tight, not maximal

It would be easy to read this section as "more rules are always better."
That is not the claim. The claim is that quality rules must be **tight** in
a specific sense: *precise, falsifiable, and motivated by a realized
failure* — not maximal in number.

The discipline that keeps the manifesto tight is the ratchet itself
(Section 4): rules earn their place by corresponding to a bug that actually
happened. This is a crucial constraint. A manifesto grown by speculation
("we should probably also forbid...") accumulates false positives that
throttle the workforce and erode trust in the gate. A manifesto grown by
the ratchet stays *grounded* — every rule has a corpse behind it. The
imperatives are tight because they are *earned*, and earned rules are the
ones least likely to be wrong.

This gives a principled answer to the perennial question "how many quality
rules should we have?": **exactly as many as you have had distinct,
worth-preventing failures** — no more (speculative rules throttle), no
fewer (ungated bug classes recur).

## 5.4 The deeper argument: quality as the precondition for autonomy

There is a reason quality cannot be deferred in an autonomous system the
way it sometimes can in a human one. A human team can carry quality debt
because humans *route around* bad code — they know which modules are
landmines and tread carefully. Agents have no such situational awareness;
they read the code as ground truth and faithfully imitate whatever
conventions they find. **In a codebase tended by agents, today's quality
defect is tomorrow's training example.** A god-function that ships becomes
the template the next agent copies. Inconsistent error handling, once
present, propagates.

This means quality entropy is not merely undesirable in an autonomous
system — it is *self-amplifying*. The code the agents read shapes the code
the agents write. The quality gate is therefore not a finishing step; it is
the mechanism that keeps the system's own training surface clean enough to
remain governable. Tight code-quality imperatives are, in the most literal
sense, a precondition for the autonomy being safe to continue.

This also reframes the classic argument for catching problems early. Boehm
and Basili's defect-reduction synthesis (2001) reports that fixing a defect
after delivery can cost on the order of 100× its cost at requirements/design
time, that projects spend ~40–50% of effort on *avoidable* rework, and that
peer review catches a median ~60% of defects [2] — the textbook case for a
front-loaded gate. We cite it with a deliberate honesty the rest of this
chapter demands: the largest replication to date (Menzies et al., 2017; 171
projects) found *no consistent* evidence for a universal exponential
"delayed-issue effect," concluding the cost-escalation curve is
context-dependent rather than a law [3]. The early-gate argument therefore
rests not on a fixed multiplier but on the *self-amplification* above — in an
agent-tended codebase the cost of a shipped defect is not merely a later fix
but a corrupted training surface, which is precisely the regime where late
correction is most expensive.

## 5.5 The cost, and the irony

The honest cost: writing and maintaining manifestos is real work, and an
over-eager manifesto can throttle throughput with false positives — the
gate blocks good work, the operator loses trust, the gate gets disabled,
and the whole surface collapses. The discipline of "tight, earned rules"
mitigates this but does not remove the maintenance burden.

There is also an irony worth stating, because it bears on credibility:
*this very codebase* exhibits some of the quality defects its manifestos
preach against — a 567-line orchestration function, dead scaffolding,
duplicated config systems. That the artifact does not fully live up to its
own imperatives is not a refutation of the imperatives; if anything it is
evidence *for* them, demonstrating that without relentless enforcement even
a quality-conscious author drifts. Section 9 treats this honestly rather
than hiding it.

## References

1. M. M. Lehman. "Programs, Life Cycles, and Laws of Software Evolution."
   *Proceedings of the IEEE* 68(9), 1980. *E-type systems must be continually
   adapted; complexity rises unless work is done to reduce it.*
2. B. Boehm and V. R. Basili. "Software Defect Reduction Top 10 List." *IEEE
   Computer* 34(1), 2001. *Post-delivery fixes ~100× costlier; ~40–50% effort
   is avoidable rework; peer review catches a median ~60% of defects.*
3. T. Menzies, W. Nichols, F. Shull, L. Layman. "Are Delayed Issues Harder to
   Resolve? Revisiting Cost-to-Fix of Defects throughout the Lifecycle."
   *Empirical Software Engineering* 22, 2017. *Across 171 projects, no
   consistent evidence for a universal exponential delayed-issue effect — the
   honest qualifier to Boehm.*

*(All references verified against primary sources before citation.)*

---

[← Reinforcement Feedback Loops](./04-reinforcement-feedback-loops.md) · [Index](./README.md) · [Next: The Slop Daemon →](./06-hunting-sloppy-patterns.md)
