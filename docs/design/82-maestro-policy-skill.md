# The maestro as a skill tree — orchestration policy, explicit (#465 S1)

This is the maestro's orchestration written down as a **skill tree**: each tick
is a walk over the nodes below. Today the *tick executes this in Python*
(`runner/tick.py` + `runner/repairs.py`); this document is the explicit,
declarative form — the literal "one skill that explains to the maestro what to
do." It is the spec the interpreter seam (S2) will read, and the artifact the
self-improvement loop (S3) will evolve.

## The hard boundary (read first)

Every node is tagged:

- **[HARD]** — mechanism or a safety gate. Deterministic Python. **NEVER**
  interpreted from a skill, NEVER overridable by an evolved policy. The loop is
  money- and merge-safe *because* these are code. A test must prove a tampered
  policy cannot bypass any [HARD] node.
- **[SOFT]** — judgment/policy. Currently hardcoded, but legitimately
  expressible as an interpreted + evolvable skill (S2/S3). Degrading a [SOFT]
  node falls back to its documented default; it can never escalate privilege or
  skip a [HARD] gate.

## The tick, as a tree

```
tick
├─ 1. sense
│   ├─ [HARD] load Settings + Config (settings.py); resolve events log
│   ├─ [HARD] fetch open issues, open PRs, repo state (githubkit)
│   └─ [SOFT] axis filter: an issue is eligible iff it carries a matching
│             axis:<name> (derived from .forge/axes.yaml names) AND loop:ready
│
├─ 2. triage / shape work  [SOFT]
│   ├─ [SOFT] PO expansion: a thin/underspecified ready ticket is rewritten to
│   │         the 5 falsifiable sections before dispatch (skip if already shaped)
│   └─ [SOFT] selection: which ready issue(s) to dispatch this tick, honoring
│             parallel cap; promotion of triage→ready in dep order when ready<N
│
├─ 3. repair-before-new  [SOFT priority, HARD effects]
│   ├─ [SOFT] prefer repairing an existing blocked/open PR over dispatching new
│   │         (repairs.py: blocking_pr_repairs, ready_issue_open_pr_repairs)
│   ├─ [SOFT] adopt an orphaned clean PR (orphaned_clean_pr_adoptions) —
│   │         but [HARD] never adopt a blocking PR, a closed issue, or a human PR
│   └─ [HARD] in-flight / cooldown skip guards (don't double-dispatch; a body
│             change or blocking comment invalidates the skip)
│
├─ 4. execute  [HARD mechanism]
│   ├─ [HARD] dispatch worker in an isolated worktree with the policy-enforced
│   │         env (secrets withheld), brief built by worker_brief (now incl. the
│   │         injected skill-tree section, #458)
│   └─ [HARD] capture outcome (status, cost, turns) → events
│
├─ 5. review + merge gate  [HARD — safety-critical]
│   ├─ [HARD] run critic; map verdict. ONLY an affirmative verdict clears merge;
│   │         changes_requested / blocked / error withholds it
│   ├─ [HARD] risk:high → DO NOT auto-merge; park "ready for human review"
│   ├─ [HARD] merge gate: required checks + approvals + not-vetoed + issue-open
│   │         (refused issues never advance frontier or memory)
│   └─ [HARD] on cleared: squash-merge + enable automerge for repaired PRs
│
└─ 6. learn  [HARD mechanism, SOFT what-to-learn]
    ├─ [HARD] record episodic memory for merged issues (learning.py); supersede
    │         the failure episode if the ticket previously failed
    ├─ [SOFT] harvest a procedural skill card from each critic-clean merge
    │         (#458) — gated by misc.skill_tree_harvest; best-effort, never
    │         breaks the tick
    └─ [SOFT] (future) harvest/curate ORCHESTRATION-policy skills from tick
              outcomes — the recursion that makes THIS document self-improving
```

## What S2/S3 may touch — and may not

- S2 introduces an interpreter for exactly ONE [SOFT] node first (selection,
  step 2) — the tick consults the policy skill, with the hardcoded rule as a
  fallback, proven at parity on a fixture. No [HARD] node is ever read from a
  skill.
- S3 lets [SOFT] policy nodes evolve via the #458 substrate (orchestration-skill
  cards, harvested from tick outcomes, curated, injected into the maestro's own
  decision context).
- **Invariant (all slices):** a test asserts that a tampered/garbage policy
  skill (a) degrades [SOFT] nodes to their defaults and (b) cannot make a [HARD]
  gate pass — no merge of a critic-blocked PR, no auto-merge of risk:high, no
  spend past the cap.

## Why this ordering is the policy
"Repair-before-new" (3 before 2's dispatch) keeps a blocked PR from rotting while
new work piles up. "Refused issues never advance memory" keeps the skill/episodic
trees honest. "Risk:high parks" keeps a human in the loop for consensus-adjacent
change. These are the load-bearing policy choices an evolved maestro must be
*measured against*, not free to discard.
