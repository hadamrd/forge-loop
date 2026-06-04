[← Limitations](./08-limitations.md) · [Index](./README.md)

---

# 8. Conclusion

## 8.1 The argument, restated

We began with an inversion: in an era where language models can reliably
generate correct code, the hard problem of autonomous software development
is no longer *generation* but *governance*. An unsupervised generation
engine does not fail loudly; it drifts — substituting achievable proxies
for real value, spending uniformly across work of unequal worth, and
degrading the codebase one locally-acceptable change at a time.

The remedy cannot be a human reviewing every output, because that
re-bottlenecks the system on the scarce resource autonomy was meant to
free. The remedy must be to move human judgment **out of the per-action
loop and into machine-checkable artifacts** — encoded once, enforced
indefinitely.

forge-loop instantiates this principle as three coupled control surfaces:

- **Product articulation** (Section 3) makes *what is worth doing* a typed,
  falsifiable input, with an explicit negative space that defeats proxy
  substitution.
- **Reinforcement feedback loops** (Section 4) gate admission with a typed
  critic and — the most original idea — ratchet every shipped defect into a
  permanent constraint, making the system's failure set shrink with
  experience.
- **Code-quality imperatives** (Section 5) turn quality from tacit taste
  into an executable admission policy, which an agent-tended codebase
  requires because its own code is its next training example.

Composed as ordered gates on a single tick (Section 7), these surfaces
deliver the property an ungoverned system cannot have: **bounded,
non-decreasing value over an unbounded number of unsupervised actions** —
bought with an upfront specification cost that amortizes while per-action
review cost never does (Section 2).

## 8.2 Why the tradeoff is favorable

The economic core of the argument is an asymmetry. Specification is a fixed
cost that an unbounded number of future actions draw against; per-PR review
is a linear cost that bottlenecks on human availability and never
amortizes. Past an early crossover, governance is strictly cheaper for the
same safety — the same reason type systems beat manual inspection, lifted
from the level of syntax to the level of *value and quality*. That is the
sense in which the architecture is "scientifically a good tradeoff": not
that it is free, but that its cost structure is the right shape for the
regime it targets.

## 8.3 What is durable here

The forge-loop *product* competes in a crowded and fast-converging space;
platform-native "assign an issue, get a PR" features may well absorb the
dispatch loop. We are explicit about that, and about the artifact's own
unevenness (Section 8).

But the **idea** is more durable than the product. The proposition that
autonomous agents should be governed by *machine-checkable value and
quality manifestos, with a feedback ratchet that converts failures into
permanent constraints* is not tied to any one orchestrator, model, or
vendor. It is a stance on how to make machine-speed software development
*safe to leave running* — and that problem only grows as the agents get
better. The control surfaces age well precisely because they do not depend
on the thing that changes fastest (the model); they depend on the thing
that changes slowest (what the operator actually values and refuses to
ship).

## 8.4 Closing

If there is a single sentence to carry away, it is this:

> As generation becomes free, value is decided at the gates. Build the gates
> well, encode your judgment in them once, and let the machine enforce that
> judgment a million times — that is the whole of the discipline.

forge-loop is one attempt to build those gates. This paper has argued that
the *shape* of that attempt — three control surfaces, composed as a gated
loop, improved by a ratchet — is the right shape, while being candid that
the *execution* is a first draft and the *evidence* is preliminary. The
gates are the contribution. Everything else is plumbing.

---

[← Limitations](./08-limitations.md) · [Index](./README.md)
