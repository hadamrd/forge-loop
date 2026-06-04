# Governing Autonomous Software Factories

### A position paper on the architecture of forge-loop

> New broader draft: [Beyond the PR Bot](./00-long-running-autonomous-software-engineering.md)
> reframes forge-loop as an early prototype for long-running, event-sourced,
> hierarchical autonomous software engineering systems. The older chapter set
> below is retained as the governance-focused first draft.
> Adversarial notes live in
> [paper/reviews/00-long-running-autonomous-software-engineering-adversarial-review.md](./reviews/00-long-running-autonomous-software-engineering-adversarial-review.md).

*An argument that three coupled control surfaces — explicit product articulation, reinforcement feedback loops, and tight code-quality imperatives — together form a favorable engineering tradeoff for autonomous, multi-agent software development systems.*

---

## Abstract

Autonomous coding systems — swarms of language-model agents that pick up
work, write code, and ship it without a human in the loop — fail not
because the agents cannot write code, but because nobody told them what is
*worth* writing and nothing stops them from drifting. An unsupervised agent
optimizes for the nearest proxy of "done": tests pass, the diff is large,
the PR is open. Left alone, it produces motion without value and erodes the
codebase it touches.

This paper argues that the central engineering problem of an autonomous
software factory is not *generation* but *governance*, and that governance
must be made **machine-checkable** to operate at machine speed. We present
the architecture of `forge-loop` as a worked example of one such governance
design, built around three coupled control surfaces:

1. **Product articulation as a typed artifact** — value *axes* and a
   product vision that make "is this worth doing?" a question the system
   can answer before it spends a dollar of compute (see
   [Product Articulation & Value Axes](./03-product-articulation-axes.md)).
2. **Reinforcement feedback loops** — a typed critic that gates merges, and
   a *bug → rule → permanent gate* ratchet that converts every failure into
   a constraint the system can never violate again (see
   [Reinforcement Feedback Loops](./04-reinforcement-feedback-loops.md)).
3. **Code-quality imperatives as a control surface** — manifestos and a
   severity rubric that turn "good code" from a matter of taste into an
   executable admission policy (see
   [Code-Quality Imperatives](./05-code-quality-imperatives.md)). Its active,
   adversarial form is a standing hunt for AI code pathologies — reinvention,
   non-reuse, convoluted logic, non-performant code, stale docs — named and
   gated on every loop (see [The Slop Daemon](./06-hunting-sloppy-patterns.md)).

We make the case that this triad is a *good tradeoff* — that the cost it
imposes (operator effort to write specifications and rules upfront) buys
the one property an autonomous system cannot otherwise have: **bounded,
non-decreasing value over an unbounded number of unsupervised actions.** We
also state, honestly, where the current implementation falls short of the
thesis it embodies (see [Limitations & Threats to Validity](./08-limitations.md)).

---

## Table of Contents

| # | Section | What it argues |
|---|---------|----------------|
| 1 | [Introduction: The Governance Problem](./01-introduction.md) | Why generation is solved and governance is not. |
| 2 | [The Central Tradeoff](./02-the-tradeoff.md) | Upfront specification cost in exchange for bounded autonomous value. |
| 3 | [Product Articulation & Value Axes](./03-product-articulation-axes.md) | Making "is this worth doing?" machine-checkable. |
| 4 | [Reinforcement Feedback Loops](./04-reinforcement-feedback-loops.md) | The critic gate and the bug→rule→gate ratchet. |
| 5 | [Code-Quality Imperatives](./05-code-quality-imperatives.md) | Quality as an executable admission policy, not taste. |
| 6 | [The Slop Daemon](./06-hunting-sloppy-patterns.md) | Naming AI code pathologies and hunting them on every loop. |
| 7 | [System Architecture: The Tick](./07-system-architecture.md) | How the three surfaces compose into one control loop. |
| 8 | [Limitations & Threats to Validity](./08-limitations.md) | Where implementation diverges from thesis. |
| 9 | [Conclusion](./09-conclusion.md) | The governance triad as the durable contribution. |

---

## How to read this paper

Each section stands alone but the argument is cumulative. Section 1
establishes the problem; Section 2 states the thesis as an explicit
tradeoff; Sections 3–5 defend each of the three control surfaces in turn;
Section 6 shows how they compose at runtime; Section 7 is the honest ledger
of where the artifact does not yet live up to the argument; Section 8
states what we believe is durable.

Throughout, claims are grounded in the actual mechanisms of the
`forge-loop` codebase (`.forge/axes.yaml`, `.forge/quality-manifesto.md`,
the `critic` module, the `brainstormer`, the merge gate) rather than in an
idealized system. This is a *position paper*, not a controlled study: it
argues a design philosophy and is explicit about its evidentiary limits.

---

*Status: draft. Authored as an architectural rationale for the forge-loop
project.*
