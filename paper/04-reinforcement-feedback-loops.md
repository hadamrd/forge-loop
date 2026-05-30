[← Product Articulation](./03-product-articulation-axes.md) · [Index](./README.md) · [Next: Code-Quality Imperatives →](./05-code-quality-imperatives.md)

---

# 4. Reinforcement Feedback Loops

> *Control surface #2: gating what gets admitted, and turning every failure
> into a constraint the system can never violate again.*

## 4.1 Two loops, two timescales

Product articulation (Section 3) decides what work *enters* the system.
This section concerns what happens to work *inside* it. forge-loop runs two
feedback loops at different timescales:

- **The fast loop (per-PR): the critic gate.** On every candidate change,
  a typed critic produces a structured verdict; severity-1 findings block
  the merge. This operates in seconds-to-minutes and decides *this* change.
- **The slow loop (per-bug): the ratchet.** When a defect escapes the fast
  loop and ships, it is distilled into a permanent rule that the fast loop
  will enforce forever after. This operates over days and changes *all
  future* changes.

The combination is the interesting claim: a system that not only filters
its outputs but **improves its own filter from its own failures.** That is
the "reinforcement" in the title — not gradient-based RL, but a structural
reinforcement loop where the policy (the ruleset) is updated by the
environment's signal (production bugs).

## 4.2 The fast loop: the typed critic as an admission policy

The critic is the system's reviewer-of-record. Its design has two
properties worth defending.

**It emits a typed verdict, not prose.** A `CriticReport` carries
structured findings, each with a *severity* (sev1/sev2/sev3) and a
*category* (correctness / security / style / tests / docs). This typing is
what makes the verdict *actionable by the loop*: sev1 mechanically disables
auto-merge and labels the PR blocking; sev2/sev3 become inline review
comments. A prose review ("looks mostly fine but I have concerns") cannot
drive an automated gate; a typed one can.

**Severity encodes a deliberate asymmetry.** The system is built to
*believe sev1 and discount sev3*. Blocking findings are treated as
authoritative; advisory findings are treated as likely-noise. This is a
direct acknowledgment that the critic is itself an imperfect agent: the
gate is tuned so that the *expensive* error (blocking good work) is rarer
than the *cheap* error (letting through a stylistic nit). An admission
policy that did not distinguish severities would either block too much
(throttling throughput) or block too little (admitting defects). Typing the
severity is how the tradeoff is made tunable instead of binary.

## 4.3 The slow loop: the bug → rule → permanent gate ratchet

This is, in our assessment, the most original idea in the system.

The ratchet works like this:

1. A defect escapes the critic and ships in PR #N.
2. It gets fixed.
3. The *shape* of the failure is distilled into a new rule and added to the
   quality manifesto (the project provides `manifesto suggest --from-pr N`
   to draft this delta).
4. From the next run onward, the critic enforces the new rule. **Any future
   PR exhibiting that failure shape is blocked at merge.**

A real instance from the project's own history: a stringly-typed
event-boundary bug shipped, was fixed, and the quality manifesto gained a
rule forbidding cross-module string discriminators. The critic now blocks
any future PR that compares `event["kind"] == "literal"` across a module
boundary. The class of bug was retired, not just the instance.

## 4.4 Why the ratchet is scientifically the right shape

Three reasons this mechanism is more than a convenience.

**It makes the failure set monotonically shrink.** In an ungoverned
system, the set of possible bad outcomes is constant — every bug class that
ever happened can happen again. Under the ratchet, each realized failure
*permanently removes itself* from the future failure set. This is the
formal source of the "non-decreasing value" property claimed in Section 2:
the system's worst case improves with experience.

**It converts a per-instance cost into a one-time cost.** Without the
ratchet, the same bug class recurs across agents and modules, and each
recurrence costs a fresh debugging session. With it, the *first* occurrence
is paid in full and every subsequent occurrence is paid at the price of an
automated block. This is the amortization argument of Section 2 instantiated
at the level of defects.

**It is institutional memory for a memoryless workforce.** The agents do
not learn between runs; each dispatch is fresh. The ratchet is where the
*system* remembers what the *agents* cannot. Knowledge that would normally
live in a senior engineer's head ("we don't do X here, we got burned")
becomes an executable artifact that outlives any individual run and applies
uniformly to every agent. This is the closest thing an agent swarm has to
seniority.

## 4.5 The honest caveat: the loop is only as sharp as its distillations

The ratchet's power depends entirely on the quality of the
*distillation* — step 3. A rule written too narrowly ("don't compare
`event['kind']` in `events.py`") fails to generalize and the bug returns in
the next module. A rule written too broadly throttles legitimate work with
false positives. And the loop is not autonomous: a human must still notice
the bug, decide it is worth a rule, and write the rule well. The system
*supports* the ratchet (it drafts the delta); it does not *guarantee* it.
The mechanism is sound; its yield is bounded by operator discipline — once
again locating the cost exactly where Section 2 said it would be.

---

[← Product Articulation](./03-product-articulation-axes.md) · [Index](./README.md) · [Next: Code-Quality Imperatives →](./05-code-quality-imperatives.md)
