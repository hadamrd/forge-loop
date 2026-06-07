[← The Convergence Problem](./09-the-convergence-problem.md) · [Index](./README.md) · [Next: Limitations →](./11-limitations.md)

---

# 10. The Outer Loop: The Half the System Never Closed

> *This paper has spent nine chapters describing the pieces of a loop without
> ever naming the loop. Product articulation decides what is worth doing; the
> ratchet turns failures into permanent gates; the tick executes. But nothing
> measures whether the system is improving, and nothing feeds outcomes back to
> re-aim what it builds. The control loop was half-built and left open. This
> chapter names it, and argues that closing it — not adding more machinery — is
> the frontier.*

## 10.1 The observation

By the time the inner loop was solid, a different absence became visible. The
loop ran hands-off for hours: it took small tickets, converged them in two
critic rounds, and **auto-merged them with zero operator action** — clean
autonomous lands, no pile-up, no crashes, trunk green throughout. On the
inner-loop axis, exactly what Sections 4–7 promise.

And yet the work it *chose* was timid. Its proposals were edge-case guards on
functions that already existed — correct, convergeable, and almost without
leverage. To test whether this was the generator's fault or the **material's**,
we ran a deliberate probe: a fresh, zero-context agent was given only the
project's own cognition artifacts (`.forge/` vision, axes, frontier cursor,
manifestos, backlog) and asked to reconstruct the project's true state and find
its highest-leverage next move. Its unprompted findings were diagnostic:

- It reconstructed the project's *intent and architecture* with high
  confidence — the articulation material is genuinely good.
- It could **not verify the system's headline claim** ("gets measurably better
  from its own outcomes"): no metric, no trend, nothing. Cracking open the
  durable stores by hand, it found the learning memory held *one* stub row and
  the metric projections had **never run** (zero cursors).
- It identified that the *operational* priority signal **inverts** the
  strategic one: the only work tagged ready-to-build was small guard tickets,
  while the one high-leverage item (an improvement-measurement epic) carried no
  ready label. *A planner trusting the salient signal ships busywork and never
  measures itself.*

The same conclusion arrived twice — once from watching the loop choose timid
work, once from a blind agent reading only the material. The system can run; it
cannot tell whether running is getting it anywhere, and it is steered away from
finding out.

## 10.2 The loop the paper never named

Step back and the diagnosis is structural. The oldest vocabulary for
improvement, the **Plan-Do-Check-Act** cycle (Shewhart 1939; Deming 1986),
makes the gap legible — and reveals that this paper has been describing PDCA
fragments all along, without assembling them:

- **Plan** — decide what is worth doing. **Built, as Section 3.** Axes + vision
  articulate value. But §3.3's claim that this "closes a loop … value *drives
  the backlog*" is precisely half-true: the arc is **feed-forward only**
  (articulation → brainstormer → backlog). There is no return path, and §3.5
  concedes the axes are static — *only as good as what the operator writes*,
  never revised from what actually happened.
- **Do** — execute. **Built, as Section 7** (the tick).
- **Act, on quality** — turn outcomes into durable constraints. **Partly built,
  as Section 4**: the bug→rule→gate ratchet converts shipped *defects* into
  permanent manifesto gates. But it acts only on the *quality* axis, is
  operator-triggered, and consumes bug-fix PRs — there is no analogous arc that
  acts on *value and direction*.
- **Check** — measure whether the system is improving. **Absent.** No
  first-pass-acceptance, rework-rate, or cost-per-merged-PR; the projection
  framework exists but no metric projection was ever registered.
- **Act, on direction** — re-rank the backlog and advance the frontier from
  outcomes. **Absent.** Nothing rewrites what the system pursues.

So the system is not missing an outer loop wholesale. It built **Plan-intent**
and **Act-on-quality**, runs a tight **Do**, and never closed the two arcs that
make the loop a *loop*: measurement, and the feedback from measurement to
direction. A cycle with two of four quadrants, executed forever, cannot bend
its own trajectory — which is exactly what we observed.

There is a self-inflicted reason the timidity is acute, and it ties to the
previous chapter. Section 9's remedy for the convergence ceiling was to
**decompose work into small, verification-easy units** (§9.5). That works — but
it rewards proposing work *small enough to land cleanly*, and "small enough to
land" pulls against "high-leverage." The adversariality–convergence frontier of
§9.4 reappears at the **planning** altitude as a Goodhart effect (Goodhart
1975): optimize for convergence, get convergent triviality. The fix for the
inner loop is part of why the outer loop must exist.

## 10.3 Non-activation, not absence

The sharper and more uncomfortable finding is that the missing arcs are mostly
**already scaffolded, and switched off**. An audit of the codebase against the
two absent organs found:

- a complete event-log **projection framework** (cursors, replay, guards) — with
  **no concrete metric projection registered** against it;
- a **writable frontier cursor** *and a reserved event kind named
  `frontier.advanced`* — emitted by nothing;
- the **manifesto ratchet** (Section 4) — a working failure→rule→inject-into-brief
  mechanism — wired to a manual operator command, never to the loop, and fed by
  bug PRs rather than recurring critic findings;
- an **outcome-drift detector** that can already halt the loop, and an
  **audit-probe framework** that scans the repo and files tickets — both present,
  both barely populated.

The substrate for Check and Act-on-direction is largely *built and dormant*.
This is itself a finding about how an autonomous builder accretes a system:
**building a piece of substrate is convergeable, single-mechanism work — the
regime the loop is good at — while *wiring the pieces into a closed loop* is an
act of recognizing what is absent and connecting it, the generation-side leap
Section 9 shows is hard.** The system accumulated the parts of its own outer
loop and could not take the step that turns parts into a cycle. The failure
mode is not "forgot to build it"; it is "built it and never closed it."

## 10.4 What human engineering already settled (so we do not reinvent it)

The temptation is to invent a bespoke "strategist agent." We resist it twice
over now — once because human organizations solved this long ago, and once
because the audit shows we would be rebuilding parts we already have. The honest
move is to **adopt the proven minimal form and activate the existing scaffolding**:

- **PDCA / kaizen** (Shewhart 1939; Deming 1986; Ohno 1988) — the cycle that
  *defines* improvement. A system claiming to improve must close it.
- **Objectives with falsifiable key results** (Drucker 1954; Grove 1983; Doerr
  2018) — "self-improving" with no measured result is a slogan, not an objective.
- **Value-based prioritization** (cost of delay / WSJF, Reinertsen 2009; RICE,
  Intercom 2016) — a backlog *ranked by value*, the structural answer to trivia
  floating to the top.
- **The retrospective** (Kerth 2001; Derby & Larsen 2006) — a cadence-bound
  look-back that reads outcomes and *changes what gets done next*. This, not an
  oracle, is how teams "find the frontier."

None of this is exotic, and that is the point: the outer loop is a known
structure to be *installed and wired*, not discovered.

## 10.5 The approach: close the loop, mostly by wiring

The work is to complete the two open arcs, reusing what exists. Framed as
*activation* rather than construction:

1. **CHECK — register the Scorecard.** Write one concrete projection on the
   existing framework that derives, as a trend: first-pass critic acceptance,
   mean repair-rounds-to-converge and the sev-2 *regeneration* rate (§9.1),
   cost/tokens per merged PR, lead time, abandonment. This turns "are we
   improving?" into a curve the system can read. It is a class on a built
   framework, not new infrastructure.

2. **PLAN — rank by value under an objective.** The one genuinely *new* build:
   attach an impact/effort estimate to every brainstormer proposal so the
   backlog is **ordered by value**, under one stated Objective + Key Result (the
   natural home is the existing `frontier.yaml`). A near-zero-impact guard sinks;
   leverage rises. This completes the return arc that §3.3 only gestured at —
   value *actually* driving the backlog, now informed by outcomes.

3. **ACT — wire the two feedbacks that already have substrate.** (a) A retro
   policy that reads the Scorecard against the Key Result and **advances the
   frontier cursor**, emitting the `frontier.advanced` event that is already
   defined and never fired. (b) **Extend the manifesto ratchet** to trigger from
   recurring *critic-finding* patterns (not only bug PRs) and run loop-driven —
   making the Section-4 quality ratchet a true cross-task teacher, the
   system-level sibling of §9.5's learning critic.

The discipline human practice insists on — learned the hard way — is that **a
ceremony without teeth becomes theater**. So every organ must *change a decision
or not run*: the Scorecard feeds ranking, the retro rewrites the cursor, the Key
Result is falsifiable, and there is one ranked backlog as the single source of
truth. We keep PDCA's spine and refuse its rituals.

## 10.6 An honest accounting: where this helps and where it might fail

This is **one attempt, not the solution** — a set of hypotheses to be falsified
by the very Scorecard it proposes, in the spirit of §9.5.

**What it has going for it.**

- It is *borrowed and activated, not invented* — PDCA, OKRs, value-ranking, and
  retrospectives are decades-proven, and most of the mechanism already exists in
  the repo, dormant. The plan shrank from "build three organs" to "register one
  projection, add value-ranking, wire two feedbacks."
- It makes the central thesis **falsifiable** for the first time.
- It attacks the ambition collapse **at its cause** (value-ranking demotes
  trivia) rather than by exhorting the generator to think bigger.
- It honors the project's own anti-reinvention rule, which a bespoke parallel
  design would have violated.

**Where it may fail — stated plainly.**

- **Closing the loop is itself the hard absent-leap.** Recognizing the dormant
  parts and wiring them is exactly the generation-side weakness of Section 9.
  The Act organ — a retro that re-aims the frontier — is an LLM doing the
  frontier-finding task that is the weak spot; reframing it as "wiring" lowers
  but does not remove that risk. We hope its narrower, evidence-anchored
  question is easier than open-ended generation; we do not have proof.
- **Metrics invite gaming, recursively.** A Scorecard rewarding throughput is
  satisfied by trivial-merge padding; optimizing first-pass acceptance can teach
  the system to propose only the trivially acceptable — the timidity we are
  curing, re-incentivized by the cure. The Key Result is load-bearing and easy
  to get wrong (Goodhart, again).
- **A rewritten frontier and an auto-fed ratchet are poisoning surfaces.**
  Letting the system edit its own objective and its own gates is powerful and
  dangerous in equal measure; a wrong lesson written persistently is a
  systematic defect. Both must be evidence-gated and reversible.
- **Activation can re-expose latent bugs.** Substrate built and never run
  (the unregistered projections, the never-fired event) is substrate never
  tested in anger; turning it on may surface defects that dormancy hid.
- **Process can ossify into theater** despite the guardrail — the failure mode
  human teams know best is the one we are most likely to reproduce.

The contribution is symmetrical with Chapter 9. That chapter named
*convergence* as a first-class requirement distinct from gate quality. This one
names the *outer loop* — measurement and self-direction — as a first-class
requirement distinct from reliable execution, and observes that the system had
built its fragments (Plan-intent in §3, Act-on-quality in §4) without ever
closing them into a cycle. A system can be perfectly governed and perfectly
convergent and still go nowhere, because nothing decides where "somewhere" is
and nothing checks whether it is getting closer.

## 10.7 Limitations of this analysis

The footing is weaker than Chapter 9's, and we say so. The closing work is
**largely unbuilt at the time of writing** — a forward-looking proposal grounded
in an observation and a code audit, not a report on a shipped subsystem; none of
§10.5's claims are yet validated by the Scorecard they depend on. The
human-process analogy (PDCA, OKRs, retrospectives) is drawn from *human*
organizations; whether those structures transfer to a swarm of stateless agents
— which do not tire, forget, or resist process the way teams do — is an open
empirical question. And the motivating evidence is the same small-n,
single-codebase kind as before: forge-loop grinding (and reading) itself, the
cleanest available testbed and the least generalizable one. The zero-context
probe in §10.1 is a single trial, not a controlled study. The next step is the
controlled one: register the Scorecard, state one Key Result, and let the curve
decide whether any of this chapter is right.

## References

- Shewhart, W. A. (1939). *Statistical Method from the Viewpoint of Quality
  Control.* Graduate School, U.S. Dept. of Agriculture. (Origin of the
  Shewhart/PDCA cycle.)
- Deming, W. E. (1986). *Out of the Crisis.* MIT CAES. (PDCA/PDSA; continuous
  improvement.)
- Ohno, T. (1988). *Toyota Production System: Beyond Large-Scale Production.*
  Productivity Press. (Kaizen lineage.)
- Drucker, P. F. (1954). *The Practice of Management.* Harper & Row.
  (Management by Objectives — the OKR ancestor.)
- Grove, A. S. (1983). *High Output Management.* Random House. (Objectives and
  key results at Intel.)
- Doerr, J. (2018). *Measure What Matters.* Portfolio. (OKRs.)
- Reinertsen, D. G. (2009). *The Principles of Product Development Flow.*
  Celeritas. (Cost of delay; WSJF.)
- Kerth, N. L. (2001). *Project Retrospectives: A Handbook for Team Reviews.*
  Dorset House.
- Derby, E., & Larsen, D. (2006). *Agile Retrospectives: Making Good Teams
  Great.* Pragmatic Bookshelf.
- Goodhart, C. (1975). *Problems of Monetary Management: The U.K. Experience.*
  (Goodhart's Law.)

---

[← The Convergence Problem](./09-the-convergence-problem.md) · [Index](./README.md) · [Next: Limitations →](./11-limitations.md)
