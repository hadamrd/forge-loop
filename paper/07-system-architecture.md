[← The Slop Daemon](./06-hunting-sloppy-patterns.md) · [Index](./README.md) · [Next: Limitations →](./08-limitations.md)

---

# 7. System Architecture: The Tick

> *How the three control surfaces compose into a single, repeating control
> loop.*

Sections 3–5 defended each control surface in isolation. This section shows
how they compose at runtime. The thesis of the composition is simple: the
three surfaces are not three features bolted together — they are three
*gates on a single pipeline*, positioned so that work must pass value,
then quality, then liveness checks before it can affect the world.

## 7.1 The loop, abstractly

The system advances in discrete **ticks**. Each tick is one pass of a
control loop that pulls candidate work, runs it through the agents, gates
the results, and lands what survives. Abstractly:

```
                ┌─────────────────────────────────────────────┐
                │                  TICK                        │
                │                                              │
   value gate   │   1. select admissible work                 │
   (Section 3)  │      └─ axis filter: only work that serves   │
                │         a declared value axis                │
                │                                              │
                │   2. (periodic) maintenance / grooming       │
                │      └─ dedupe, retitle, expand thin specs   │
                │                                              │
   generation   │   3. dispatch N workers in parallel          │
                │      └─ each in an isolated git worktree      │
                │      └─ brief carries the quality manifesto   │
                │         (Section 5: guidance at gen-time)     │
                │                                              │
   quality gate │   4. critic reviews each PR → typed verdict  │
   (Sections    │      └─ sev1 blocks; sev2/3 advise           │
    4 & 5)      │      └─ manifesto compliance enforced        │
                │                                              │
   liveness     │   5. merge gate                              │
   gate         │      └─ refuse if source issue closed,       │
                │         conflicts unresolved, etc.           │
                │                                              │
                │   6. land survivors; (optional) redeploy      │
                │   7. emit audit events; sleep; repeat ↺       │
                └─────────────────────────────────────────────┘
```

## 7.2 Why this ordering is the right ordering

The sequence is not arbitrary. Each gate is positioned to fail work **as
early and as cheaply as possible**, which is a core efficiency argument.

**Value gate first (cheapest).** Filtering by value axis happens before any
agent is dispatched — before a dollar of compute is spent. Rejecting
cosmetic work at selection time is free; rejecting it after an agent has
written 700 lines is expensive. Putting the value gate first means the
system never pays generation cost for work it would refuse to ship anyway.

**Generation in isolation.** Each worker runs in its own git worktree off
the base branch. This is the concurrency-safety argument: parallel agents
cannot corrupt each other's working state, and a failed agent leaves no
trace on the others. Isolation is what makes "dispatch N in parallel"
safe rather than a race condition.

**Quality gate after generation, before merge.** The critic runs on the
produced PR. This is the only correct place for it — you cannot review code
that does not exist yet, and you must not merge code that has not been
reviewed. Manifesto enforcement and the typed verdict both live here.

**Liveness gate last.** The merge gate checks conditions that can only be
known at the last moment: has the source issue been closed mid-flight? Are
there unresolved conflicts? These are *time-of-merge* facts; checking them
any earlier would be checking stale state. The merge gate is the system's
defense against acting on a world that changed while it was working.

## 7.3 The composition is the contribution

The individual gates are each defensible (Sections 3–5). The architectural
claim of this section is that **their composition is what produces the
Section 2 property.** Value-first selection bounds *what* the system spends
effort on; the quality gate bounds *how good* what it ships is; the
liveness gate bounds *whether the world still wants it*; the audit log and
the ratchet make the whole loop *improvable*. Remove any one gate and a
pathology from Section 1 returns:

| Remove this gate | Pathology that returns |
|------------------|------------------------|
| Value (axis filter) | Value-blindness — effort spread across worthless work |
| Quality (critic + manifesto) | Quality entropy — codebase decays change by change |
| Liveness (merge gate) | Acting on stale state — landing work the world abandoned |
| Audit + ratchet | Static failure set — same bugs recur forever |

The loop is a pipeline of gates, each cheap relative to the cost of the
failure it prevents, composed so that human judgment encoded on the three
control surfaces is enforced on every one of an unbounded number of
autonomous actions. That composition — not any single gate — is the design
this paper defends.

## 7.4 A note on what the architecture deliberately does *not* do

The loop does not try to make the agents smarter, and that is intentional.
It treats the generation engine as a fixed, fallible black box and invests
entirely in the *governance* around it. This is a bet that, as base models
improve, a system organized around durable control surfaces will compound
those improvements (better agents, same gates, strictly better outcomes),
whereas a system organized around clever prompting will have to be
re-engineered each model generation. The architecture is designed to age
well by refusing to depend on the thing that changes fastest.

## 7.5 The runtime substrate is deliberately old

If the governance composition (§7.3) is the novel contribution, the runtime
machinery underneath it is the opposite: it is, on purpose, forty-year-old
distributed-systems engineering. This is not incidental — it is the paper's own
anti-slop thesis (Chapter 6) applied to itself. The fastest way to rot an
autonomous system would be to let it *reinvent* coordination primitives that
the literature settled decades ago; so the substrate reuses the canonical
solutions to the canonical problems, and earns the right to spend its novelty
budget on governance instead.

- **Each dispatched unit of work is a saga.** A worker's run is a long-lived
  transaction whose steps each carry a *compensating* action (reap the
  worktree, close the row) that semantically undoes partial work on failure —
  exactly the construct Garcia-Molina and Salem introduced for long-lived
  transactions in 1987 [1]. "Compensation" in the recovery path is not a
  coinage; it is the saga's defining mechanism.
- **Liveness is a lease.** A worker holds a time-limited lease, renewed by
  heartbeat; if the process dies, the lease lapses and the saga is reaped at the
  next boot. Gray and Cheriton established the lease in 1989 precisely for this
  property — under non-Byzantine (crash/network) failure, an expired lease costs
  *performance, not correctness* [2]. We respect the boundary they drew: a lease
  alone is safe for *reclaiming* a dead worker's slot, but using it as a
  distributed lock around external mutation would require fencing tokens, as
  Kleppmann argues [3]; the loop's compensations, not the lease, are what keep a
  late-waking zombie worker from corrupting state.
- **The lease claim is optimistic concurrency.** Two schedulers racing for the
  same task are arbitrated by a single conditional write (`UPDATE … WHERE lease
  is free`), the validation-based concurrency control Kung and Robinson
  formalized in 1981 [4] — no lock manager, the database adjudicates.
- **The audit log is an event source.** State is reconstructed by replaying an
  append-only log of past-tense domain events, the event-sourcing/CQRS lineage
  documented by Young and Fowler [5], itself rooted in Meyer's command-query
  separation.
- **Delivery is at-least-once; reapers are idempotent.** The tick re-runs
  recovery and grooming every pass and must tolerate re-delivery without
  double-acting — the at-least-once + idempotent-consumer discipline that is
  standard messaging-semantics canon [6], older than the brokers that document
  it.

None of these are this paper's inventions, and that is the point worth stating
plainly: a credible autonomous-engineering substrate is mostly *boring, proven*
distributed-systems work, with the genuinely new ideas concentrated in the
governance gates above. Citing the lineage is also a small act of intellectual
honesty the topic demands — a paper about hallucinated references (§6.3) should
carry a bibliography every entry of which has been verified against its primary
source.

## References

1. H. Garcia-Molina and K. Salem. "Sagas." *Proc. ACM SIGMOD Int. Conf. on
   Management of Data*, 1987, pp. 249–259. DOI 10.1145/38713.38742. *Long-lived
   transactions as sequences of sub-transactions, each with a compensating
   transaction.*
2. C. G. Gray and D. R. Cheriton. "Leases: An Efficient Fault-Tolerant
   Mechanism for Distributed File Cache Consistency." *Proc. 12th ACM SOSP*,
   1989, pp. 202–210. DOI 10.1145/74850.74870. *The lease as a time-limited
   grant; expiry affects performance, not correctness, under non-Byzantine
   failure.*
3. M. Kleppmann. *Designing Data-Intensive Applications.* O'Reilly, 2017
   (ISBN 9781491903063); and "How to do distributed locking," 2016-02-08.
   *Lease expiry alone is insufficient for distributed locks over external
   mutation; fencing tokens are required.*
4. H. T. Kung and J. T. Robinson. "On Optimistic Methods for Concurrency
   Control." *ACM Trans. Database Syst.* 6(2), 1981, pp. 213–226.
   DOI 10.1145/319566.319567. *Validation-based concurrency control as an
   alternative to locking.*
5. G. Young. *CQRS Documents* (self-published whitepaper), 2010; and
   M. Fowler, "Event Sourcing" / "CQRS" (martinfowler.com). *Reconstructing
   state from an append-only log of past-tense domain events; command/query
   model separation, rooted in Meyer's CQS.*
6. Transactional Outbox: C. Richardson, microservices.io (popularizer/
   cataloguer). Delivery semantics (at-most/at-least/exactly-once) and
   idempotency: Apache Kafka KIP-98 / Confluent documentation — authoritative
   documentation of a taxonomy that predates Kafka.

*(Every reference above was verified against its primary source — ACM DL / DBLP
DOIs, author-hosted PDFs, or the publisher of record — before being cited.)*

---

[← The Slop Daemon](./06-hunting-sloppy-patterns.md) · [Index](./README.md) · [Next: Limitations →](./08-limitations.md)
