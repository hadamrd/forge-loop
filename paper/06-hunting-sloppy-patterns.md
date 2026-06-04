[← Code-Quality Imperatives](./05-code-quality-imperatives.md) · [Index](./README.md) · [Next: System Architecture →](./07-system-architecture.md)

---

# 6. The Slop Daemon: Hunting AI Code Pathologies

> *Control surface #3, sharpened: quality is not only a standard to write
> toward, it is a set of named adversaries to hunt down — continuously, on
> every loop, forever.*

## 6.1 A failure class that passes every other gate

Chapters 3–5 defend against an agent that builds the *wrong* thing, drifts
from the vision, or violates a written rule. There is a subtler failure that
slips through all three: code that is *aligned, in-scope, and rule-compliant*,
whose tests pass and whose diff reviews cleanly in isolation — and whose
**internals are quietly bad**. It works on the surface and rots underneath.

This is the characteristic output of a capable code model left unsupervised.
The model is trained to produce text that *looks like* a correct solution, and
it is extraordinarily good at the local appearance of competence. What it is
*not* reliably good at — absent pressure — is the engineering virtues that have
no immediate test: reaching for the right library instead of re-deriving it,
finding the helper that already exists, choosing the abstraction that will
still be legible in six months, picking the algorithm that does not quadratic-
blow-up at scale, and going back to fix the document its change just made
false. None of these have a failing test. All of them compound.

We name this failure class the **slop daemon**: the standing tendency of
autonomous code generation toward output that is statistically
indistinguishable from good engineering at a glance and materially worse on
inspection. The industry's lived experience of it is now common — *"it worked,
but when I read the internals they were absurd and non-performant."* A
governance system that does not hunt this daemon explicitly will accumulate it,
because every other gate waves it through.

## 6.2 Why slop is self-amplifying — and therefore must be hunted, not tolerated

Section 5.4 established the load-bearing fact: **in a codebase tended by
agents, today's code is tomorrow's training example.** The slop daemon turns
that fact from a quality concern into a runaway. A re-invented helper is copied
by the next agent that greps for "how do we do X here." A convoluted function
becomes the template for the next convoluted function. A stale doc is read as
ground truth and propagated into new code built on a false premise. Slop is not
inert debt that sits where it lands; it is *seed stock*. Left unhunted, the
codebase's own surface teaches the workforce to produce more of it, and the
rate of slop generation rises with every loop.

This is why the response cannot be periodic cleanup. A human team can run an
occasional refactoring sprint because humans stop generating slop between
sprints. An agent swarm does not. The only stable answer is a **standing
guardian**: a cop that rides every loop, inspects every diff for these
pathologies before they enter the training surface, and refuses the ones that
would seed more of themselves.

## 6.3 Naming the daemon's faces: a slop taxonomy

You cannot gate what you cannot name. A standing guardian needs the daemon
*enumerated* — each pathology written down as an explicit, recognizable target
rather than a vague appeal to "good taste." forge-loop's working taxonomy:

- **Reinvention.** Hand-rolling what the standard library or an existing
  dependency already does well — a retry/backoff loop, an LRU, datetime
  parsing, a glob, an HTTP client. The fix is always "use the known solution to
  the known problem."
- **Non-reuse of internals.** Duplicating a helper, parser, or abstraction that
  *already exists in this repo* instead of importing it — usually because the
  agent never looked. Copy-paste of existing logic is the most common and most
  corrosive face, because it multiplies maintenance surface invisibly.
- **Convoluted, under-abstracted logic.** Branchy mega-functions and tangled
  control flow that "work" but cannot be held in one reading — the god-function
  pathology, the wrong data structure (a list scan where a set lookup is
  obvious), abstraction at the wrong seam.
- **Over-abstraction.** Its mirror image: indirection, configuration knobs, and
  "frameworks" with a single caller. Slop is not only too little structure; it
  is also structure with no payer.
- **Non-performant code.** Quadratic where linear is easy; N+1 subprocess / API
  / database calls inside a loop that wanted one batched call; redundant I/O
  re-reading the same file or query on a hot path; unbounded growth with no cap
  or index. Each is invisible to a passing unit test and obvious at scale.
- **Stale, abandoned documentation.** A change that makes a doc false and never
  returns to update it. Docs that are never re-reviewed decay into confident
  lies — uniquely dangerous in an agent codebase, where the next worker treats
  the doc as fact.

The list is not closed, and that is the point: the taxonomy is itself an
artifact the system grows. Each newly observed slop pattern is named and added,
exactly as the ratchet of Section 4 grows the rule set — the daemon is mapped
incrementally, one recognized face at a time.

## 6.4 The mechanism: the loop as a standing anti-slop guardian

Naming the patterns is inert without enforcement. forge-loop wires the taxonomy
into the same two-ended pressure as Chapter 5 — guidance at generation,
enforcement at admission — but aimed specifically at internal quality:

- **An admission lens.** The critic carries dedicated `architecture` and
  `performance` review categories whose entire job is to hunt the taxonomy of
  6.3 on every PR — reinvention, non-reuse, N+1, redundant I/O,
  over-abstraction — and to block or flag them with a concrete fix. "It passes
  its tests" is explicitly *not* a defense the critic accepts. Crucially this
  inspects *internals*, the dimension every other gate ignores.
- **A generation-time standard.** The same pathologies are written as manifesto
  rules — "use the known library," "reuse the existing helper," bounds on
  function size and complexity — prepended to the worker's brief so the agent
  is steered away from slop before it writes it, not only caught after.
- **The ratchet, pointed at internals.** When a slop pattern slips through, it
  is not merely fixed in place; it is *named* and converted into a standing
  rule and a critic lens, so that specific face of the daemon can never recur
  silently. The taxonomy in 6.3 is the accumulated output of this ratchet.

The defining property is **continuity**. This is not a one-shot audit but a
guardian that re-runs its hunt on every diff, every loop, indefinitely —
because the daemon regenerates on every loop. The cop never goes off duty,
because the thing it polices never stops being produced.

## 6.5 Why explicit enumeration is the whole game

The temptation is to delegate this to a single instruction: *"write good,
performant, well-architected code."* That instruction fails for the same
reason "write valuable software" fails (Chapter 3) — it is a proxy the model
satisfies in appearance. Vague quality asks elicit vaguely-quality-shaped
output. The leverage is in the *enumeration*: a named pattern ("N+1 `gh` call
inside a loop"; "re-implements an existing helper") is checkable, teachable,
and ratchetable in a way "be performant" never is. Making each face of the
daemon an explicit, individually-named target is what converts an aspiration
into an admission policy — and what lets the system get monotonically better at
the hunt instead of relitigating taste on every PR.

## 6.6 The cost, and the standing confession

The honest costs are real. An over-zealous slop hunt produces false
positives — flagging a deliberate simplicity as "under-abstracted," or a clear
inline as "should reuse" — and false positives erode trust in the gate exactly
as in Chapter 5; the same discipline of *tight, earned* patterns applies. And
the hunt has a blind spot: the critic inspects what enters, but pre-existing
slop already in the tree is only revisited when a change touches it, so old
internals can harbor unhunted daemons indefinitely.

The standing confession of Section 5.5 applies here with full force, and is
itself evidence for the thesis: *this very codebase still contains specimens of
every face in 6.3* — an over-long orchestration function, a redundant per-loop
read, a duplicated config path. That the artifact has not yet exterminated its
own slop is not a refutation; it is the demonstration that without a relentless,
named, continuous hunt, even a slop-conscious system drifts. The contribution
of this chapter is not a clean codebase. It is the claim that the daemon must
be *named, hunted on every loop, and grown into the gate* — and the machinery
to do so.

---

[← Code-Quality Imperatives](./05-code-quality-imperatives.md) · [Index](./README.md) · [Next: System Architecture →](./07-system-architecture.md)
