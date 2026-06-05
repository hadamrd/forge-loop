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

**The empirical case: raw AI speed is real, narrow, and not yet value.** The
productivity evidence is sharply bimodal and the split is the whole argument.
Vendor-affiliated RCTs show large gains on narrow tasks — Peng et al. (2023)
measured a single greenfield HTTP-server task completed **55.8% faster** with
Copilot [1], and a pooled three-experiment field study of 4,867 developers
found a **26% increase** in completed tasks [2] — but those gains concentrate
in *junior* developers and shrink toward zero for senior ones [2]. Against
that, the independent METR 2025 RCT found experienced developers on their own
mature repositories were **19% slower** with AI [3]. The reconciliation is
the load-bearing finding for this paper: Google's DORA program concludes AI
is an **amplifier** — it raises individual throughput while *decreasing
software-delivery stability* unless the team is wrapped in strong control
systems (automated testing, version control, fast feedback), and that the ROI
"comes from the system around the AI, not the tool itself" [4]. That is
precisely the thesis of the governance triad: raw generation speed is the
input; the control surfaces are what convert it into bounded, durable value.
Without them you do not get the speed for free — you get throughput with
falling stability, which is value moving in both directions (Section 1).

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

## References

1. S. Peng, E. Kalliamvakou, P. Cihon, M. Demirer. "The Impact of AI on
   Developer Productivity: Evidence from GitHub Copilot." arXiv:2302.06590,
   2023. *RCT (n=95): a greenfield HTTP-server task completed 55.8% faster.*
   **Vendor-affiliated; single boilerplate task.**
2. M. Demirer et al. "The Effects of Generative AI on High-Skilled Work:
   Evidence from Three Field Experiments with Software Developers."
   *Management Science*, 2026 (SSRN 4945566). *4,867 developers; +26% completed
   tasks, concentrated in junior/recent hires, ~0 for seniors.* **Vendor-
   affiliated.**
3. J. Becker, N. Rush, E. Barnes, D. Rein (METR). "Measuring the Impact of
   Early-2025 AI on Experienced Open-Source Developer Productivity."
   arXiv:2507.09089, 2025. *Independent RCT: experienced devs 19% slower with
   AI on their own mature repos (they believed they were faster).*
4. Google / DORA. *State of DevOps Report* 2024 & 2025. *AI as an amplifier:
   raises throughput, lowers delivery stability without strong delivery
   controls; ROI comes from the surrounding system, not the tool.*

*(Sources flagged vendor-affiliated vs independent; all verified against
primary sources before citation. "AI writes X% of code"/acceptance-rate
figures were checked and treated as vendor marketing — not cited.)*

---

[← Introduction](./01-introduction.md) · [Index](./README.md) · [Next: Product Articulation →](./03-product-articulation-axes.md)
