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

This is not a folk worry; it is measured. A 2026 static-analysis study of
**304,362 AI-authored commits across 6,275 repositories** found that *more than
15% of commits from every assistant introduce at least one issue* (from 17.3%
for GitHub Copilot to 28.7% for Gemini), and — the load-bearing number —
**24.2% of those AI-introduced issues still survive at HEAD**, roughly 37 per
100 AI commits [5]. The defining property of slop is precisely this *survival*:
it is not caught and reverted, it sits and compounds. The model is trained to
produce text that *looks like* a correct solution, and it is extraordinarily
good at the local appearance of competence. What it is *not* reliably good at —
absent pressure — are the engineering virtues with no immediate failing test:
reaching for the right library instead of re-deriving it, finding the helper
that already exists, choosing an abstraction still legible in six months,
picking the algorithm that does not quadratic-blow-up at scale, and going back
to fix the document its change just made false.

We name this failure class the **slop daemon**: the standing tendency of
autonomous code generation toward output that is statistically
indistinguishable from good engineering at a glance and materially worse on
inspection. A governance system that does not hunt this daemon explicitly will
accumulate it, because every other gate waves it through.

## 6.2 Why slop is self-amplifying — and therefore must be hunted, not tolerated

Section 5.4 established the load-bearing fact: **in a codebase tended by
agents, today's code is tomorrow's training example.** The slop daemon turns
that fact from a quality concern into a runaway, and the macro-trend data is
consistent with exactly this dynamic. GitClear's longitudinal analysis of
**211 million changed lines (2020–2024)** reports that **2024 was the first
year on record in which copy/pasted lines exceeded "moved" (refactored)
lines**; that refactored code fell from ~25% of changed lines in 2021 to under
10% in 2024; and that the share of commits containing a 5+-line duplicated
block rose roughly *eight- to ten-fold* over two years. Their summary is
blunt — AI-assisted code "resembles an itinerant contributor, prone to violate
the DRY-ness of the repos" [1]. *(Caveat, treated honestly in §6.6: GitClear is
a commercial analytics vendor and the trend is correlational, not a controlled
causal result.)*

The mechanism is compounding. A re-invented helper is copied by the next agent
that greps for "how do we do X here." A convoluted function becomes the
template for the next convoluted function. A stale doc is read as ground truth
and propagated into new code built on a false premise. Slop is not inert debt
that sits where it lands; it is *seed stock*. This is why the response cannot be
periodic cleanup: a human team can run an occasional refactoring sprint because
humans stop generating slop between sprints — an agent swarm does not. The only
stable answer is a **standing guardian**: a cop that rides every loop, inspects
every diff before it enters the training surface, and refuses the changes that
would seed more of themselves.

## 6.3 Naming the daemon's faces: a slop taxonomy

You cannot gate what you cannot name. A standing guardian needs the daemon
*enumerated* — each pathology written as an explicit, recognizable target
rather than a vague appeal to "good taste." forge-loop's working taxonomy, each
face annotated with the evidence that it is real:

- **Reinvention & non-reuse.** Hand-rolling what the standard library or an
  existing dependency already does (a retry loop, an LRU, datetime parsing, a
  glob), or duplicating a helper that *already exists in the repo* instead of
  importing it. This is the empirically dominant face: copy/paste overtaking
  refactoring for the first time, refactored code more than halving as a share,
  and duplicate blocks rising ~8–10× [1]. The most corrosive because it
  multiplies maintenance surface invisibly.
- **Convoluted / under-abstracted logic, and its mirror, over-abstraction.**
  Branchy mega-functions, the wrong data structure (a list scan where a set
  lookup is obvious), abstraction at the wrong seam — or, inverted, indirection
  and "frameworks" with a single caller. A peer-reviewed analysis of 387
  agent-authored build PRs found *Lack of Error Handling* among the top
  introduced smells (63 occurrences) [4].
- **Non-performant code.** Quadratic where linear is easy; **N+1** subprocess /
  API / DB calls inside a loop that wanted one batched call; redundant I/O
  re-reading the same file on a hot path; unbounded growth with no cap or index.
  Each is invisible to a passing unit test and obvious only at scale.
- **Hallucinated dependencies ("slopsquatting").** The model imports a package
  that *does not exist*. This is not hypothetical: across 2.23M generations,
  **19.7% of samples referenced at least one non-existent package** (≈21.7% for
  open-source models vs ≈5.2% commercial), and **43% of phantom names recurred
  identically** across re-runs — a stable target an attacker can pre-register to
  inject malware [2]. The PSF's Seth Larson named it *slopsquatting* in 2025
  [7]; a proof-of-concept hallucinated `huggingface-cli` drew 30k+ downloads.
  The threat is *compressed, not retired* on frontier models: one 2026 study
  measures rates down to ~5% yet finds **127 phantom package names that five
  different models all invent identically** [3].
- **Unpinned / wildcard dependency versions.** The single **most common** build
  smell in that 387-PR study was *Wildcard Usage* (97 occurrences) — `*`/`+`
  version specifiers that make builds non-deterministic — followed by
  *Deprecated* and *Outdated Dependencies* (56 and 27), i.e. agents propagating
  stale, unmaintained libraries [4].
- **Stale, abandoned documentation.** A change that makes a doc false and never
  returns to update it. Uniquely dangerous in an agent codebase, where the next
  worker treats the doc as fact. (We caught a live specimen *writing this very
  chapter*: a section-renumber left three cross-references pointing at the wrong
  sections — the exact pathology, in the document warning about it.)

The list is not closed, and that is the point: the taxonomy is an artifact the
system *grows*. Each newly observed pattern is named and added, exactly as the
ratchet of Section 4 grows the rule set — the daemon mapped incrementally, one
recognized face at a time.

## 6.4 The mechanism: the loop as a standing anti-slop guardian

Naming the patterns is inert without enforcement. forge-loop wires the taxonomy
into the same two-ended pressure as Chapter 5 — guidance at generation,
enforcement at admission — aimed specifically at internal quality:

- **An admission lens.** The critic carries dedicated `architecture` and
  `performance` review categories whose entire job is to hunt the taxonomy of
  6.3 on every PR — reinvention, non-reuse, N+1, redundant I/O,
  over-abstraction — and to block or flag them with a concrete fix. "It passes
  its tests" is explicitly *not* a defense the critic accepts.
- **A dependency-integrity gate.** Because hallucinated and wildcard
  dependencies are both empirically common *and* mechanically checkable, they
  are first-class: a newly added dependency must resolve to a real registry
  entry and carry a pinned version. This converts the two security-adjacent
  faces from "hope the reviewer notices" into a deterministic check.
- **A generation-time standard.** The same pathologies are written as manifesto
  rules — "use the known library," "reuse the existing helper," bounds on
  function size and complexity — prepended to the worker's brief so the agent is
  steered away from slop *before* it writes it, not only caught after.
- **The ratchet, pointed at internals.** When a pattern slips through it is
  *named* and converted into a standing rule and a critic lens, so that face can
  never recur silently. The taxonomy in 6.3 is the accumulated output of this.

The defining property is **continuity**, and one empirical result makes
continuity non-negotiable rather than merely nice. A controlled experiment
found that **iteratively re-prompting a model to improve code raised critical
vulnerabilities by 37.6% after just five iterations** — security debt
accumulating non-linearly *as the agent "improves" its own work* [6]. An
autonomous loop that re-dispatches workers (as forge-loop does, Section 7) is
therefore not a system that converges to safety by trying again; it is a system
that must **re-run the full gate on every iteration**, because the iteration
itself is a slop source. The cop never goes off duty, because the thing it
polices is regenerated by the very act of iterating.

A further property worth stating: several faces reduce to **git-computable
metrics**, not only model judgment. *Churn* — lines reverted or substantially
revised within two weeks of authoring — rose from 3.1% (2020) to 5.7% (2024)
in the GitClear corpus [1] and is a direct, mechanical proxy for "looks-right
code discarded soon after." It is the same quantity the system's own roadmap
names as a health metric (rework rate), which means the anti-slop hunt and the
self-measurement loop of Section 4 are the same instrument pointed at the same
target.

## 6.5 Why explicit enumeration is the whole game

The temptation is to delegate this to one instruction: *"write good,
performant, well-architected code."* That fails for the same reason "write
valuable software" fails (Chapter 3) — it is a proxy the model satisfies in
*appearance*. Vague quality asks elicit vaguely-quality-shaped output. The
leverage is in the *enumeration*: a named pattern ("N+1 `gh` call inside a
loop"; "wildcard version specifier"; "re-implements an existing helper") is
checkable, teachable, and ratchetable in a way "be performant" never is. Naming
each face individually is what converts an aspiration into an admission policy —
and what lets the system get monotonically better at the hunt instead of
relitigating taste on every PR.

## 6.6 The cost, the evidence, and the standing confession

Three honesty notes, because a paper that hides them is itself a kind of slop.

**On the evidence.** The strongest macro numbers — the duplication, refactor-
decline, and churn trends [1] — come from a *commercial analytics vendor*
publishing *self-measured, non-peer-reviewed, correlational* data; the vendor's
own CEO has acknowledged the data cannot establish causation, and a confounder
(the 2022–2024 change in who was committing) is plausible. The descriptive
trends are robust and widely re-reported, but the load-bearing claims of this
chapter rest on the *peer-reviewed* studies: package hallucination [2],
agent-introduced build smells [4], and defect survival at scale [5]. The
frontier-slopsquatting figure [3] is a single non-peer-reviewed preprint and is
cited as "one study reports," not as settled.

**On the gate's limits.** An over-zealous hunt produces false positives —
flagging deliberate simplicity as "under-abstracted" — which erode trust
exactly as in Chapter 5; the discipline of *tight, earned* patterns applies.
And the hunt inspects what *enters*: pre-existing slop is only revisited when a
change touches it, so old internals can harbor unhunted daemons indefinitely.

**The standing confession.** *This very codebase still contains specimens of
every face in 6.3* — an over-long orchestration function, a redundant per-loop
read, a duplicated config path. That the artifact has not yet exterminated its
own slop is not a refutation; it is the demonstration that without a relentless,
named, continuous hunt, even a slop-conscious system drifts. The contribution
of this chapter is not a clean codebase. It is the claim — now backed by the
measured survival of AI-introduced defects [5], the empirical reality of
hallucinated and wildcard dependencies [2][4], and the counter-intuitive
*degradation* under naive iteration [6] — that the daemon must be **named,
hunted on every loop, and grown into the gate.**

## References

1. GitClear. *AI Copilot Code Quality: 2025 Research* (analysis of ~211M
   changed lines, 2020–2024). Copy/paste exceeding moved code; refactoring 25%
   → <10%; ~8–10× duplicate-block growth; churn 3.1% → 5.7%.
   <https://www.gitclear.com/ai_assistant_code_quality_2025_research>
2. Spracklen et al. *"We Have a Package for You": A Comprehensive Analysis of
   Package Hallucinations by Code-Generating LLMs.* USENIX Security 2025
   (arXiv:2406.10279). 2.23M samples; 19.7% hallucinated-package rate; 43%
   recurrence.
3. Churilov. *The Range Shrinks, the Threat Remains: Package Hallucination on
   Frontier Models.* 2026 preprint (arXiv:2605.17062). ~5% rates; 127
   model-agnostic phantom package names. *(Non-peer-reviewed; cite as one
   study.)*
4. Ghammam & Almukhtar. *AI Builds, We Analyze: Build-Code Smells in
   Agent-Authored Pull Requests.* MSR 2026 (arXiv:2601.16839). 387 agent PRs;
   364 introduced smells; Wildcard Usage (97) the most common.
5. Liu, Widyasari, Zhao, Irsan, Chen & Lo. *Debt Behind the AI Boom:
   Static-Analysis of AI-Authored Commits at Scale.* 2026 preprint
   (arXiv:2603.28592). 304,362 AI commits / 6,275 repos; >15% introduce an
   issue; 24.2% survive at HEAD; security issues decay slowest (41.1% survive).
6. Shukla, Joshi & Syed. *Iterative Prompting and Security Degradation in
   LLM-Generated Code.* 2025 preprint (arXiv:2506.11022). 400 samples; +37.6%
   critical vulnerabilities after five improvement iterations.
7. Larson, S. (Python Software Foundation). Coinage and analysis of
   *"slopsquatting"* — registering hallucinated package names as an attack, 2025.

---

[← Code-Quality Imperatives](./05-code-quality-imperatives.md) · [Index](./README.md) · [Next: System Architecture →](./07-system-architecture.md)
