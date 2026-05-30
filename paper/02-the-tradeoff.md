[← Introduction](./01-introduction.md) · [Index](./README.md) · [Next: Product Articulation →](./03-product-articulation-axes.md)

---

# 2. The Central Tradeoff

Every architecture is an answer to the question *"what cost are you willing
to pay, in exchange for what property?"* This section states forge-loop's
answer explicitly, because a tradeoff defended honestly is more convincing
than a benefit claimed without a price.

## 2.1 What you pay

The governance triad is not free. It imposes a real, unavoidable cost on
the operator, paid **upfront and continuously**:

- **You must articulate the product.** Writing `axes.yaml` and a product
  vision forces you to state, in falsifiable terms, who you serve and what
  counts as value. This is hard — harder than writing the code, for many
  people — because it demands clarity that ad-hoc development lets you
  avoid.
- **You must write the rules.** Every quality imperative in the manifesto
  is a sentence someone had to think through and commit to. The critic can
  only enforce what has been written down.
- **You must tend the feedback loop.** Each bug that ships is a debt: it
  must be distilled into a rule, or the same class of failure recurs.

In short: the system shifts effort from *reviewing outputs* to
*specifying constraints*. You do less of the thing humans are slow at
(reading every diff) and more of the thing humans are uniquely good at
(deciding what matters).

## 2.2 What you buy

In exchange, you buy the single property an ungoverned autonomous system
cannot have:

> **Bounded, non-decreasing value over an unbounded number of unsupervised
> actions.**

Unpack that:

- **Unbounded actions.** The loop can run indefinitely, dispatching many
  agents in parallel, without a human gating each one.
- **Non-decreasing value.** Because every admitted change must clear the
  value axes and the quality gate, the system cannot ship work that is
  worthless or corrosive — the floor only moves up.
- **Bounded blast radius.** Because failures are converted into permanent
  gates, the set of possible bad outcomes *shrinks monotonically over
  time* rather than recurring.

## 2.3 Why this is a *good* tradeoff, not just *a* tradeoff

The trade is favorable because of an asymmetry in how the two costs scale.

**Specification cost is paid once and amortizes; review cost is paid per
action and does not.** A value axis you write today governs every ticket
the system ever generates against it. A quality rule you write after one
bug blocks that bug class in every future PR, across every agent, forever.
The marginal cost of governing the *N+1*-th action approaches zero as the
ruleset matures. By contrast, per-PR human review is a flat tax: the
ten-thousandth review costs as much as the first.

This is the same economic shape that makes *compilers* worth more than
*manual code inspection*, or *type systems* worth their annotation
overhead: you pay a fixed cost to encode a constraint, and the machine
enforces it an unbounded number of times at no incremental human cost. The
governance triad applies that pattern one level up — not to syntax or
types, but to **value and quality**.

```
        cost
         │
review   │            ╱  per-action human review (linear, never amortizes)
(human)  │          ╱
         │        ╱
         │      ╱
         │    ╱        ┌──────────────────  governance (fixed + decaying margin)
         │  ╱      ┌───┘
         │╱   ┌────┘
         └────┴───────────────────────────────► number of autonomous actions
```

The two regimes cross early. Past the crossover, governance is strictly
cheaper for the same safety — and unlike review, it does not bottleneck
throughput on human availability.

## 2.4 When the tradeoff is *bad*

Intellectual honesty requires stating where this design loses. The
governance triad is a poor fit when:

- **The work is inherently subjective.** "Make it feel more premium" cannot
  be reduced to falsifiable axes or rules. The system degrades to needing a
  human at the wheel — which forge-loop's own documentation concedes.
- **The product is too young to articulate.** If you genuinely do not yet
  know what you are building, forcing an `axes.yaml` produces fiction, and
  the system will faithfully optimize the fiction.
- **Volume is low.** If you only need three changes, the fixed cost of
  specification never amortizes. Just write them yourself.

The tradeoff is *good* precisely in the regime forge-loop targets: a
product with a knowable value model, a meaningful backlog, and an operator
willing to invest in specification once to harvest leverage many times. The
following three sections defend each leg of the triad in that context.

---

[← Introduction](./01-introduction.md) · [Index](./README.md) · [Next: Product Articulation →](./03-product-articulation-axes.md)
