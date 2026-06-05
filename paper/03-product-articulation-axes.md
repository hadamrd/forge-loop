[← The Tradeoff](./02-the-tradeoff.md) · [Index](./README.md) · [Next: Reinforcement Feedback Loops →](./04-reinforcement-feedback-loops.md)

---

# 3. Product Articulation & Value Axes

> *Control surface #1: making "is this worth doing?" a question the system
> can answer before it acts.*

## 3.1 The problem this surface solves

Recall the value-blindness pathology from Section 1: a generation engine
has no internal notion of worth. Everything pattern-matches to "code that
could be written." The only way to give the system a sense of value is to
**supply one externally, in a form it can evaluate against a candidate
ticket.**

Free-form prose ("we want to delight our users") is not such a form. It is
unfalsifiable; an agent can justify almost any change as "delighting
users." What is needed is a representation of value that is **structured
enough to filter against** while remaining **expressive enough to capture
what the product actually is.**

## 3.2 The mechanism: axes + vision

forge-loop splits product articulation into two artifacts under `.forge/`,
and the split is deliberate:

- **`product-vision.md`** — free-form prose. Who you serve, the wedge,
  and — critically — *what is explicitly NOT valuable*. Prose is the right
  medium here because vision is narrative; it carries the *why* and the
  customer stories that a structured schema would flatten.

- **`axes.yaml`** — structured. The 4–6 *value axes* the system is allowed
  to move. Each axis names a customer, defines what "valuable" concretely
  means on that axis, enumerates `acceptable_work`, and — the load-bearing
  field — enumerates `rejected_as_cosmetic`.

The shape of a single axis (from the project's own configuration):

```yaml
axes:
  - name: golden-path-e2e
    customer: "SRE running their first pipeline on day zero"
    valuable_means: "Playwright tests driving the real rig — golden path
                     survives every release"
    acceptable_work:
      - "Customer-shaped pipeline fixtures (Node, Java, polyglot)"
      - "Adversarial paths: failed step, OOM step, secret-needing step"
    rejected_as_cosmetic:
      - "304 responses to polls customers don't notice"
      - "Pretty timestamps, sparklines, theme polish"
```

## 3.3 Why this is the scientifically interesting part

Most autonomous-coding tools have **no representation of value at all**.
They execute whatever ticket you point them at. The axis schema is a claim
that *value should be a first-class, typed input to the system*, on equal
footing with the code itself.

Three properties make this a sound design rather than a gimmick:

**1. It makes value falsifiable.** `valuable_means` is written as something
that could, in principle, be checked: "the golden path survives every
release" is testable in a way "delight users" is not. A ticket can be held
up against the axis and *judged*, not vibed.

**2. It encodes the negative space.** `rejected_as_cosmetic` is the most
important field and the one almost everyone forgets. Defining what is *not*
valuable is how you defeat proxy substitution. An agent that wants to
prettify a timestamp is now contradicting an explicit, named constraint —
not merely failing to satisfy a vague aspiration. **A value model without a
negative space is just a wish list; the system games it. A value model
*with* a negative space is a filter.**

**3. It is generative, not merely evaluative.** Because value is
structured, the system can *propose* work that serves the axes (the
`brainstormer` generates axis-aligned epics and tickets), and it can *tag*
every shipped change with the axis it served (`axis:<name>` labels). Value
flows forward into what gets built, not just backward into what gets
filtered. This closes a loop that prose vision alone cannot: the
specification of value *drives the backlog* rather than passively grading
it.

## 3.4 The anti-cosmetic guardrail as a Goodhart defense

There is a well-known failure of optimization — **Goodhart's law**: when a
measure becomes a target, it ceases to be a good measure. An autonomous
agent optimizing "ship PRs" will ship the easiest PRs — which are exactly the
cosmetic ones. This is not speculative for AI coding specifically: the
2024–2025 developer surveys (Stack Overflow; Google's DORA) report that the
top practitioner frustration with AI assistance is *"almost-right" output*
that looks done and is not — output optimized to the visible proxy (a
plausible diff) rather than the latent goal [1].
The `rejected_as_cosmetic` list is a direct structural defense: it removes
the easiest proxies from the set of admissible work, forcing the
optimizer's pressure back onto the axes that actually represent value.

This is why forge-loop's brainstormer carries an explicit *anti-cosmetic
guardrail*: the value model is not just consulted at generation time, it is
designed so that the cheapest-to-satisfy moves are precisely the ones it
forbids. The system is built to make gaming it harder than doing the real
work.

## 3.5 The cost, stated plainly

This surface is only as good as the axes the operator writes. A vague
`valuable_means`, an empty `rejected_as_cosmetic`, or axes that do not
actually capture the product's value model will all produce a system that
confidently optimizes the wrong thing. Garbage axes in, garbage backlog
out — and worse, *confidently and at scale*. The leverage of this surface
is real, but it is leverage on the operator's clarity, which means it
amplifies a poor value model as faithfully as a good one. This is the
upfront cost named in Section 2, located precisely.

That the *cheapest* place to spend this clarity is up front is the oldest
result in the field: Boehm and Basili's defect data show issues rooted in
the requirements/specification stage are the most expensive to correct later
(on the order of 100× post-delivery), with ~40–50% of project effort going to
avoidable rework [2]. The axis schema is that lesson applied to an agent
workforce — pay for value-clarity once, in a typed artifact, instead of in a
thousand misdirected PRs.

## References

1. Stack Overflow Developer Survey 2025; Google DORA, *State of DevOps* 2024
   & 2025. *Near-universal AI adoption with low/falling trust; the top
   frustration is "almost-right" AI output; full discussion + citations in
   Section 2.*
2. B. Boehm and V. R. Basili. "Software Defect Reduction Top 10 List." *IEEE
   Computer* 34(1), 2001. *Requirements/design-stage defects are the most
   expensive to fix late; ~40–50% of effort is avoidable rework.* (Full entry
   and the Menzies 2017 qualifier in Section 5.)

*(All references verified against primary sources before citation; "Goodhart's
law" is named as the standard attribution for the measure-as-target effect.)*

---

[← The Tradeoff](./02-the-tradeoff.md) · [Index](./README.md) · [Next: Reinforcement Feedback Loops →](./04-reinforcement-feedback-loops.md)
