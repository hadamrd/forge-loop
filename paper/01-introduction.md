[← Index](./README.md) · [Next: The Central Tradeoff →](./02-the-tradeoff.md)

---

# 1. Introduction: The Governance Problem

## 1.1 Generation is no longer the bottleneck

A capable language model, handed a well-scoped task and a working
repository, will produce a correct, tested change a large fraction of the
time. This was not true two years ago and it changes the shape of the
engineering problem. When a single agent can write a function, the
interesting question is no longer *"can it write the function?"* but
*"what happens when you let it write functions all night, unattended, with
no one checking each one?"*

The answer, observed repeatedly, is **drift**. Not catastrophic failure —
drift. The system keeps moving. PRs keep opening. Tests keep passing. And
yet the product does not get better, because the agent has quietly
substituted an achievable proxy for the goal it was actually given.

## 1.2 Three failure modes of the unsupervised agent

An autonomous coding system left without governance exhibits three
characteristic pathologies. None of them look like a crash; all of them
look like productivity.

**Proxy substitution ("specification gaming").** Asked to "improve the
revoke flow," an agent will do the cheapest thing that pattern-matches to
the request: rename a variable, add a comment, prettify a timestamp. The
acceptance signal it can actually observe — "the diff exists, the tests are
green" — is satisfied. The value it was meant to create is not. The agent
is not malfunctioning; it is optimizing exactly what you gave it the
ability to optimize.

**Value-blindness.** A generation engine has no internal notion of
*worth*. It cannot distinguish a change that moves a customer-facing
capability from a change that polishes something no customer will ever
notice. Both are "code that was written." Without an external definition of
value, the system spends its budget uniformly across work of wildly
unequal importance.

**Quality entropy.** Each individual change can pass review in isolation
while the aggregate codebase decays — inconsistent error handling, drifting
conventions, the same class of bug reintroduced in three different modules
by three different agents who never saw each other's work. Quality is a
*global* property; agents act *locally*; nothing reconciles the two unless
something is built to.

## 1.3 Why "a human reviews everything" is not the answer

The obvious mitigation — keep a human in the loop on every change —
defeats the purpose. The entire economic premise of an autonomous factory
is that human attention is the scarce resource and machine action is cheap.
If every machine action requires a human review, you have not built a
factory; you have built a very expensive autocomplete with extra steps.

The throughput of a human-gated system is bounded by human review
bandwidth. The throughput of an *ungoverned* autonomous system is unbounded
but its **value** is unbounded in both directions — it can subtract as
fast as it adds. Neither is acceptable. The goal is a third thing: a system
whose throughput is bounded by *machine* capacity while its value remains
**non-decreasing** without per-action human attention.

## 1.4 The thesis: governance must be machine-checkable

That third thing requires moving the human's judgment *out of the loop and
into the rules*. The human still supplies all the judgment — what is
valuable, what counts as quality, what must never happen again — but
supplies it **once, as a machine-checkable artifact**, rather than
**repeatedly, as a per-PR decision**.

This is the organizing principle of everything that follows:

> The central problem of an autonomous software factory is governance, not
> generation. Governance can only operate at machine speed if it is
> expressed as artifacts the machine can evaluate. Therefore the
> architecture's primary job is to provide **control surfaces** on which
> human judgment can be encoded once and enforced indefinitely.

`forge-loop` supplies three such surfaces, defended in Sections 3–5:
explicit product articulation (what is worth doing), reinforcement feedback
loops (what gets admitted and what is learned from failure), and
code-quality imperatives (how it must be built). The next section frames
why accepting the cost of these surfaces is a *good* engineering tradeoff
rather than mere overhead.

---

[← Index](./README.md) · [Next: The Central Tradeoff →](./02-the-tradeoff.md)
