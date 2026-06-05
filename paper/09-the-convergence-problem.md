[← Agent Enablement](./08-agent-enablement.md) · [Index](./README.md) · [Next: Limitations →](./10-limitations.md)

---

# 9. The Convergence Problem: When the Critic Can't Teach

> *A gate that reliably rejects bad work is necessary but not sufficient for
> autonomy. The agent must also be able to **converge** — to walk from rejected
> to accepted in a bounded number of steps. We observed a loop that could not,
> even though every individual part was working.*

## 9.1 The observation

The governance thesis (Sections 3–7) says: let the agent generate, and gate the
output on machine-checkable value and quality. We built exactly that — a worker
that drafts a PR and a separate, strong, adversarial critic that blocks on
real defects. On **small, self-contained tickets it works**: the worker drafts,
the critic approves, the change lands. (A representative case: a single-file fix
for a subtle real-clock-versus-fixed-clock test time-bomb — drafted, approved
sev-0, merged.)

On **hard, multi-constraint tickets it does not converge.** The telltale is in
the severity trajectory across repair rounds. The worker *reads and acts on the
critic* — the highest-severity findings monotonically clear (`sev1: 2 → 1 → 0`),
the missing tests get added, a wrong approach gets replaced. But the *total*
defect count does not fall: as the worker fixes the flagged problems it
introduces **new** ones, and the mid-severity count **oscillates rather than
descends** (`sev2: 1 → 4 → 2`). Across three independent hard tickets the loop
ran 5–8 rounds without ever reaching zero. The diffs grew (`+854 / −0` on one),
and the new findings were in the newly-written code: dead code defined but never
wired in, an O(n) re-read of an append-only ledger per item, scope creep.

The worker is not failing to *perceive* the feedback. It is failing to
*converge*. That distinction is the subject of this chapter.

## 9.2 What the literature already establishes

A substantial 2023–2024 literature studies the adjacent setting — a single LLM
iteratively correcting its own output — and its findings bound what an
iterative critic loop can achieve. (Caveat, stated up front and returned to in
§9.5: most of this work measures **single-model self-correction on reasoning,
math, QA, and symbolic planning**, not a two-agent code-review loop. We apply it
by analogy, not as direct proof.)

**The bottleneck is the critique signal, not the refinement step.** Tyen et al.
(ACL 2024 Findings) show that "poor self-correction performance stems from LLMs'
inability to *find* logical mistakes, rather than their ability to *correct* a
known mistake" — given a ground-truth error location, correction improves across
all five reasoning tasks they study. Kamoi et al.'s critical survey (TACL 2024)
generalizes it: LLMs "can refine their responses given reliable feedback, but
generating reliable feedback on their own responses is still challenging."

**Without a reliable external oracle, intrinsic self-correction does not
reliably help — and frequently degrades.** Huang et al. ("Large Language Models
Cannot Self-Correct Reasoning Yet," ICLR 2024) find that prompted self-correction
*lowers* accuracy on reasoning benchmarks (correct answers get flipped to wrong).
Kamoi et al. concur that intrinsic self-correction "does not improve or even
degrades" performance on arithmetic reasoning, closed-book QA, code generation,
plan generation, and graph coloring — and conclude bluntly that *no prior work
demonstrates successful self-correction with feedback from prompted LLMs in
general tasks*; it works only where reliable external feedback exists or with
large-scale fine-tuning.

**A model's own verdict is internally inconsistent.** Li et al. (ICLR 2024)
measure a generator–validator gap: even GPT-4 is GV-consistent only **76%** of
the time (it produces a correct answer in generation mode, then rejects that same
answer in validation mode). Valmeekam et al. (2023) find LLM self-verifiers on
Blocksworld planning emit a high false-positive rate (accepting invalid plans),
and that self-critiquing *diminishes* generation performance relative to an
external sound verifier.

**The "verification is easier than generation" asymmetry is bounded.** Kamoi et
al. (§7) find the Saunders et al. (2022) hypothesis — that recognizing errors is
easier than avoiding them — "is only true for certain tasks whose verification is
*exceptionally easy* (e.g., responses are decomposable)," not for general hard
tasks. **This is the single closest match to our data:** convergence holds where
verification is easy (small tickets) and breaks where it is hard (multi-constraint
tickets). The boundary the survey draws analytically is the boundary we hit
empirically.

**Even a strong, dedicated critic is a noisy teacher.** McAleese et al. (OpenAI,
2024) show a separate RLHF-trained critic (CriticGPT) catches more bugs than paid
human reviewers and is preferred 63% of the time — *yet* it "hallucinate[s] bugs
that could mislead humans," trades precision for recall, and produces its best
results as a **human+critic team**, not as an autonomous signal driving clean
generation.

## 9.3 Our extension: a reliable separate critic is still not enough

Here is where our observation goes *beyond* the cited literature rather than
merely instantiating it. The literature's prescribed remedy for unreliable
self-critique is to **separate the critic** (a distinct, ideally stronger or
purpose-trained, evaluator). We already run that architecture: the critic is a
separate role, a strong model, adversarially prompted — and it is *good*. Over
this sprint it independently caught a wrong-working-directory bug, a quality-rule
violation, and a sev-1 security-bypass in generated code. It behaves like the
CriticGPT result: a high-precision filter.

And yet the loop **still does not converge on hard tickets.** So the failure
cannot be reduced to "the critic signal is unreliable." Two further mechanisms
are doing the work:

1. **A filter is not a teacher.** The critic answers *"is this acceptable?"* with
   high reliability. It does not answer *"what is the smallest complete change
   that would be acceptable?"* The worker therefore learns the acceptance
   predicate only by *violating* it, one round at a time. On a high-dimensional
   predicate (correctness ∧ tests ∧ no-dead-code ∧ performance ∧ scope ∧
   house-style), trial-and-error discovery costs more rounds than any budget
   allows. This is the generation–verification gap (Saad-Falcon et al. 2025
   measure a 37-point oracle-vs-selection headroom) re-appearing as a
   *teaching* gap: the information needed to converge exists in the critic but is
   never transmitted as a plan.

2. **The target is non-stationary, and generation is the binding constraint.**
   The critic re-reviews the *whole artifact* each round, so as the worker's diff
   grows the evaluation surface grows with it — the worker chases a target that
   moves because of its own edits. And on a hard change the worker cannot
   satisfy all constraints *simultaneously*: it resolves the salient (flagged)
   defects while introducing latent ones in the new code. The result is exactly
   the non-monotone churn we logged — the agentic analog of program-repair
   "regression-during-fix," where a patch that closes one fault opens another.

Restated as the chapter's thesis: **the worker perceives; it cannot converge.
The critic filters; it cannot teach. And the acceptance predicate is holistic,
hidden, and non-stationary.** Reliability of the gate — the thing the governance
argument optimizes — is necessary but does not buy convergence.

## 9.4 The adversariality–convergence frontier

There is a structural tension underneath, which sharpens why "just tell the
worker exactly what the critic wants" is not a free fix. It is a Goodhart problem
(Goodhart 1975; specification gaming, Krakovna et al. 2020; reward-model
over-optimization, Gao et al. 2023):

- **Publish the critic's exact rubric to the worker** → fast convergence, *but
  gameable.* The worker optimizes the stated checks and the deficiency migrates to
  the dimensions the rubric did not enumerate — the agentic form of "when a
  measure becomes a target, it ceases to be a good measure."
- **Hide the rubric / judge holistically** → un-gameable, *but non-convergent.*
  The worker cannot anticipate, so it discovers the standard only by repeated
  violation, and the round budget expires first.

A robust evaluator and a learnable one pull in opposite directions. The art is
to move as much of the acceptance predicate as possible into a form that is
**observable, stationary, and ungameable simultaneously** — and to reserve
adversarial holism only for the irreducible-taste residual.

## 9.5 Toward a solution: make the critic teach, and guarantee progress

Our proposed remedy follows directly from the diagnosis. It is deliberately
**language-first** — the bulk is a change to how the critic *communicates*,
because that is where the missing information lives — with exactly one structural
guarantee that prose cannot provide. These are hypotheses to be tested, not
settled results.

1. **The teaching critic (round-aware, escalating specificity).** Early rounds:
   terse findings — let the worker try. Stalled rounds: escalate from *what is
   wrong* → *why it matters* → *how to fix* → *the minimal patch*, and state the
   explicit **minimal path to green** ("these two findings block merge; the rest
   are follow-ups"). Each step shrinks what the worker must *invent*, attacking
   the generation gap directly. This converts the critic from filter toward
   teacher without weakening it.

2. **Severity triage, not standard erosion.** A round counter must *not* lower
   the bar as rounds increase — a gate that tires teaches the system that
   persistence beats quality, and slop merges by attrition (Goodhart again). The
   legitimate move is triage: blocking severities (sev-1/sev-2 — real defects)
   *always* block; once convergence stalls, *cosmetic* findings are demoted to
   follow-up tickets so rounds are not burned on nits. Standards held; perfectionism
   removed.

3. **Failure-mode diagnosis.** Use the round signal to recognize *why* it is
   stuck and redirect. The classic tell is scope inflation (`+854 / −0`): the
   critic should say, by round two, *"you are adding, not editing — the dead-code
   and performance findings come from over-building; cut scope to the minimal
   wiring,"* teaching aimed at the actual cause rather than re-flagging symptoms.

4. **The one structural guarantee: monotonicity.** Language improves the dialogue
   but cannot stop the oscillation we measured. A durable, addressable
   finding-ledger — where an addressed finding cannot silently reappear and the
   open set must *strictly shrink* round over round — converts a friendly
   conversation into a *converging* one. This is the minimal piece of mechanism
   that "code with language" cannot replace.

5. **A convergence budget with escalation.** After K stalled rounds, stop
   grinding: decompose the ticket into smaller (verification-easy) units —
   precisely the regime where §9.2 says the loop *does* converge — or escalate to
   a stronger model or a human. Non-termination is itself a defect to be gated.

6. **The learning critic (persistent teaching memory).** Levers 1–5 improve
   teaching *within* a task; none lets the critic improve *across* tasks. Give
   the critic a two-tier persistent memory — a write-ahead log of every finding
   it has raised (which recurred, which the worker addressed, which PRs merged
   versus were abandoned) and a compacted, curated *front page* of distilled
   lessons (the recurring failure modes on this codebase, the teaching framings
   that actually produced convergence). The critic reads the front page at
   review time to mentor with institutional knowledge ("workers here repeatedly
   add dead code when touching the scheduler — wire it in or cut it"), and a
   periodic consolidation pass re-curates the front page from the log. This is
   the same two-tier architecture the rest of the system already uses (the
   `.forge/` event log plus projections; this paper's own authoring memory),
   applied to the *evaluator*. The caveat is dual to the benefit: a curated
   memory is a *poisoning* surface — a wrong lesson, taught persistently, is a
   systematic defect — so lessons must be evidence-gated (corroborated across
   instances), decay if unconfirmed, and never override a present-tense reading
   of the actual diff. Sequenced last, because a learning layer over an
   unvalidated teacher learns the wrong things.

The unifying principle: **shrink the gap between what the worker can cheaply
iterate against (observable, stationary, deterministic) and what the critic
ultimately judges (expensive, holistic, adversarial)** — by teaching the path,
guaranteeing monotone progress, and decomposing hard work into the verification-
easy regime where convergence is known to hold.

## 9.6 Limitations of this analysis

This chapter is honest about its evidentiary footing. The cited primary results
measure **single-model self-correction on reasoning/math/QA/planning**, not a
two-agent code-review loop; applying them to our setting is argument by analogy.
Our own evidence is **observational and small-n** — a handful of tickets on one
codebase (forge-loop grinding itself), not a controlled study. We have *not* yet
run the controlled experiment that would settle it: does the bounded
verification-easy/​hard asymmetry reproduce for multi-constraint code loops with a
*separate* critic, and is the non-monotone churn best explained by critic
false-positives, by the non-stationary whole-diff target, or by credit-assignment
failure under a delayed, partially-observed acceptance predicate? Those are the
open questions §9.5's design is meant to probe. We also do not claim
self-correction *never* works: RL-based self-correction training (e.g. SCoRe,
2024) produces real gains and is exactly the "large-scale fine-tuning" exception
the survey literature concedes — a path orthogonal to our prompt-and-protocol
remedy.

The contribution is not a solved problem. It is a **named** one: convergence, as
distinct from gate quality, is a first-class requirement for autonomous software
agents — and the pairing of this chapter with [Agent Enablement](./08-agent-enablement.md)
gives the two halves of viability. Enablement asks whether the agent *can act*.
Convergence asks whether, having acted and been judged, it can *get to done*.

## References

- Goodhart, C. (1975). *Problems of Monetary Management: The U.K. Experience.*
  (Goodhart's Law.)
- Madaan, A., et al. (2023). *Self-Refine: Iterative Refinement with Self-Feedback.*
  NeurIPS 2023. arXiv:2303.17651.
- Shinn, N., et al. (2023). *Reflexion: Language Agents with Verbal Reinforcement
  Learning.* NeurIPS 2023. arXiv:2303.11366.
- Gou, Z., et al. (2024). *CRITIC: Large Language Models Can Self-Correct with
  Tool-Interactive Critiquing.* ICLR 2024. arXiv:2305.11738.
- Huang, J., et al. (2024). *Large Language Models Cannot Self-Correct Reasoning
  Yet.* ICLR 2024. arXiv:2310.01798.
- Tyen, G., Mansoor, H., Carbune, V., Chen, P., & Mak, T. (2024). *LLMs Cannot
  Find Reasoning Errors, but Can Correct Them Given the Error Location.* ACL 2024
  Findings. arXiv:2311.08516.
- Kamoi, R., Zhang, Y., Zhang, N., Han, J., & Zhang, R. (2024). *When Can LLMs
  Actually Correct Their Own Mistakes? A Critical Survey.* TACL 2024.
  arXiv:2406.01297 / doi:10.1162/tacl_a_00713.
- Li, X. L., Shrivastava, V., Li, S., Hashimoto, T., & Liang, P. (2024).
  *Benchmarking and Improving Generator–Validator Consistency of LMs.* ICLR 2024.
  arXiv:2310.01846.
- Valmeekam, K., Marquez, M., & Kambhampati, S. (2023). *Can LLMs Really Improve
  by Self-critiquing Their Own Plans?* arXiv:2310.08118. (See also Stechly et al.
  2024, arXiv:2402.08115.)
- McAleese, N., et al. (OpenAI) (2024). *LLM Critics Help Catch LLM Bugs.*
  arXiv:2407.00215.
- Saad-Falcon, J., et al. (2025). *Shrinking the Generation–Verification Gap with
  Weak Verifiers (Weaver).* arXiv:2506.18203. (Lab-blog-backed; medium confidence.)
- Gao, L., Schulman, J., & Hilton, J. (2023). *Scaling Laws for Reward Model
  Overoptimization.* ICML 2023.
- Krakovna, V., et al. (2020). *Specification Gaming: The Flip Side of AI
  Ingenuity.* DeepMind.

---

[← Agent Enablement](./08-agent-enablement.md) · [Index](./README.md) · [Next: Limitations →](./10-limitations.md)
