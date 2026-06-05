# forge-loop quality manifesto (seed)

This is the dogfood quality manifesto for forge-loop itself. Every future
forge-loop change is gated by these rules. Each rule is followed by a
rationale that names the iteration-probe bug or persistent-worker work
that motivated it, so future contributors understand *why* the rule
exists, not just what it says.

## Rules

### Q6. No `--no-verify` without a justified reason in the PR body. Pre-commit gates are not optional.

Pre-commit exists to enforce the same quality gates locally that CI will
enforce later. Bypassing it hides defects from the worker loop and turns
review into the first real gate. If a bypass is genuinely required, the
PR body MUST include a `## Pre-commit bypass justification` section that
explains why the hook could not run.

**Rationale:** see #158. Forge-loop had a pre-commit config but the hook
was never installed in worker worktrees, so commits silently bypassed
ruff, mypy, pyright, and formatting until CI or production found them.

### Q1. No shared mutable module-level state. Use a Container or per-instance State.

Module-level globals (caches, counters, "current run" dicts, singleton
queues) make tests order-dependent and make the worker un-restartable in
process. All mutable state MUST hang off a `Container` or a per-instance
`State`/`Runner` object that can be re-created cheaply.

**Rationale:** see #100 (Runner class). The iteration-probe leaked state
between sprints because the runner held onto module-level dicts; the
persistent-worker refactor wedged that into per-instance state and the
class of bug went away.

### Q2. Every external I/O boundary lives behind a typed Protocol with a Fake for tests.

If forge-loop talks to the network, the filesystem, a subprocess, an LLM
API, or `gh`, the call MUST go through a `typing.Protocol` adapter with a
companion `Fake*` implementation in `forge_loop/_testing/`. Tests use the
Fake. Production wires the Real. No ad-hoc `httpx.get` / `subprocess.run`
calls scattered through business logic.

**Rationale:** see #104 (adapters). Untyped boundaries are exactly where
mocks drift away from reality and where flaky tests breed.

### Q3. Single config source of truth via `Settings`. No `os.environ.get` outside `settings.py`.

Configuration is read in exactly one place: `forge_loop/config.py`'s
`Settings` (or its successor `settings.py`). Reaching for
`os.environ.get(...)` anywhere else is a lint-level violation. Tests
override config by constructing a `Settings` instance, not by mutating
`os.environ`.

**Rationale:** see #98. The iteration probe found two different code
paths reading the same env var with different defaults, producing
divergent behaviour between the CLI and the worker.

### Q4. Typed events for every state change. No untyped `**fields` for kinds that have a registered model.

If an event `kind` has a registered pydantic / dataclass model, the
event MUST be constructed via that model. Passing arbitrary `**fields`
into a generic `emit()` for a known kind is forbidden — it defeats
schema validation and lets typos through silently.

**Rationale:** see #99. A misspelled field (`attempt_no` vs `attempt`)
silently fell through `**fields` and produced empty dashboard rows for
two days before anyone noticed.

### Q5. No `subprocess.run` for SDK-able external services.

If a service has a first-class Python SDK (Anthropic, GitHub), use the
SDK. Shelling out to `claude` / `gh` from inside forge-loop is
forbidden in new code. Existing shell-outs must migrate (#103 critic
SDK, #105 GhClient).

**Rationale:** subprocess wrappers hide returncode handling bugs
(#128), eat structured errors, and make tests painful (you end up
mocking argv strings instead of method calls).

## Rule: No stringly-typed cross-module event boundaries

**Rule.** If module A emits an event consumed by module B, the event KIND
(or state name, or outcome label, or any cross-module discriminator) must
be an enum (e.g. ``class FooKind(str, Enum)``) imported from a shared
module. String literal comparisons on dynamic dict values are sev1.

**Rationale.** A 4-PR train of bugs in this codebase had the SAME shape:
a discriminator was a string literal on one side and a different string
literal on the other side, the type checker had nothing to say, a typo
silently broke production.

- ``#147`` — ``_critic_sdk`` checked ``event["type"] == "result"`` but
  ``_worker_sdk`` emits ``event["kind"] == "final_result"``. Two-
  field-name mismatch. Symptom: brainstormer/critic/PO returned empty
  output for an unknown duration.
- ``#128`` — iteration state ``"PUSHED_NO_PR"`` returned via a
  fallthrough default instead of an explicit edge.
- ``#120`` — PR state ``"CLOSED"`` not handled because the dispatcher
  matched ``"MERGED"`` only.
- ``#97`` — worker outcome ``"failed"`` vs ``"no_pr"`` distinguished by
  substring matching.

Any of these would have been a 1-line type error with proper enums.

**How to apply.** When adding a new event KIND, state name, outcome
label, brief KIND, severity, or any cross-module discriminator: declare
it as a ``str`` Enum in a shared module; import the enum on both sides;
compare with ``is`` not ``==``. The critic flags string-literal
comparisons of dynamic dict values as sev1 — fix by extracting the
discriminator into an enum + updating both call sites in the same PR.

## Rule: state-based rules need a state-based gate

**Rule.** A manifesto rule that can only be checked by looking at codebase **state** (file size, module count, deprecated-pattern count, test coverage, dead-code count) MUST register a probe in the codebase auditor (#156). Per-PR critic catches **deltas**; the auditor catches **accumulation**.

**Rationale.** `src/forge_loop/cli.py` is 1705 LOC. The Python soft-cap is 500. The critic never flagged it because cli.py grew 50-100 LOC per PR over many merges. Each individual increment was a reasonable diff; the cumulative state-violation slipped past every per-PR review. Classic boiling-frog.

**How to apply.** When you add a rule like "no module > N LOC" or "no Any-typed param" or "no `subprocess.run(['gh', ...])` in production code": the same PR that adds the rule MUST add an audit probe under ``src/forge_loop/audit_probes/``. Rule and gate ship together; otherwise the rule is decoration.

## Rule: reuse, function size, and performance (anti-slop)

These three rules exist because the rubric above is entirely about
*correctness/type-safety*. None of it catches the failure mode the operator
named: code that is statistically shaped like good engineering and works on the
surface, but whose internals are duplicated, bloated, or naively non-performant
— the "AI slop" tells. A 2026 audit of this repo found all three accreting
silently. Each rule names its concrete anchor (no rationale ⇒ no rule) and
ships with a state-gate per the meta-rule above.

### Q7. Reuse before you write. No second implementation of a capability that already exists.

Before adding a function or module, search for an existing one that does the
job. A new **public** function/method whose (verb, noun) duplicates an existing
public capability is **sev2**; a **third or later** parallel implementation of
the same capability is **sev1**. "I needed `X`, the model didn't surface the
existing `X`, so it wrote a new `X`" is the single most common slop pattern, and
it is invisible to the type checker — both copies type-check fine.

**Rationale.** This repo has **three** GitHub-issue-creation surfaces:
`gh.py::create_issue`, `gh_issues.py::create_issue`, and `gh_client.py` with
**three** `create_issue` methods across its classes. A worker that needed "open
an issue" reinvented it instead of importing the existing `GhClient`. None of
the duplications was individually flagged because each looked like a reasonable
new helper in its own diff — the same boiling-frog shape as cli.py's LOC.

**How to apply.** When the diff adds a function/method, the critic checks
whether a public function with the same normalized name (or same external
target — same `gh` subcommand, same SQL table, same endpoint) already exists
elsewhere in `src/`. If so: the new code must instead import and call the
existing one, or the PR body must justify why a distinct implementation is
warranted. **State-gate:** an audit probe under `audit_probes/` buckets public
`def`s by normalized name and fails when ≥3 cross-module implementations of the
same capability exist (seed it with the `create_issue` cluster as the first
known offender to burn down).

### Q8. No god-functions. A function over 80 LOC must be decomposed.

A single function/method longer than **80 logical lines** (or whose cyclomatic
complexity exceeds ~15) is **sev2**. A PR that grows an already-over-cap
function is **sev2** even if the net diff is small — that is how they get to 582
lines. Decompose into named helpers that can each be unit-tested.

**Rationale.** `runner/tick.py::_tick()` is **582 lines** (432–1014). The #156
auditor caps *module* size (cli.py at 1705 LOC) but never *function* size, so the
single most important function in the system — the orchestration tick — grew
unreviewable and has no unit tests of its branches, only end-to-end coverage.
Module caps without function caps just relocate the boiling frog one scope down.

**How to apply.** The per-PR critic flags any function in the diff that ends
over 80 LOC. **State-gate:** an audit probe records the max-function-LOC per
file and fails on regressions, seeded with `_tick()` as the first burn-down
target (extract its phases — sync, groom, expand, select, dispatch, critic,
merge, redeploy — into named, individually-tested steps).

### Q9. Performance is a review dimension, not an afterthought.

The critic must spend findings budget on *how the code runs*, not only whether
it is correct: (a) **no network / DB / `gh` / subprocess call inside a loop over
unbounded input** — batch it or hoist it out (N+1); (b) **no re-reading the same
file/config or re-deriving the same value inside a loop** — compute once; (c) a
change to a hot path (the tick loop, dispatch, the event projections) that could
regress complexity needs a one-line **baseline → target** note in the PR body,
matching the product axes' "no perf work without a number." Quadratic-by-default
loops over the work-list or the event log are **sev2**.

**Rationale.** Honesty per the meta-rule: this audit did **not** find a confirmed
perf *incident* — but it did find that loop-bodies issuing external calls cluster
in `gh.py`, `critic.py`, and `_worker_sdk.py`, exactly where an N+1 would hide,
and the rubric has *zero* perf coverage, so nothing measures it. Per the
boiling-frog meta-rule, perf needs an auditor probe before it accretes the way
LOC did — the rule and that probe ship together; until the probe exists this
rule is enforced per-PR on the diff only, and that limit is stated here on
purpose rather than pretended away.

### Q10. Load-bearing data must not round-trip through a best-effort side-effect.

When component A produces data that component B needs, **hand it over
directly** (in-memory value, explicit argument). Do **not** write it to an
external system (GitHub, a file, a queue) and have B re-read it when a direct
hand-off exists — a failure in the side-effect then *silently starves* B with
no error at the consumer. Corollaries: (a) a side-effect on a critical path
must **fail loud or fall back**, never fail-soft-and-continue — and a function
whose docstring promises a fallback must implement it; (b) a cross-component
data flow needs a **seam test** asserting the data actually arrives at the
consumer, because per-PR-diff review is structurally blind to a broken seam.
Routing required data through a fallible, silently-degrading side-effect is
**sev2** (it yields non-converging degradation, not a crash — the hardest kind
to detect).

**Rationale.** The 2026-06-05 incident: the critic posted findings as *inline*
review comments (`file:line`); GitHub 422-rejects inline comments on lines not
in the PR diff; `post_review_comment` returned `False` with **no fallback**
(its own docstring promised one). The repair worker rebuilds its brief from the
*fetched* posted review — so with the findings dropped it repaired **blind**,
and one PR churned 44 minutes across repair rounds without ever converging,
burning model budget. Four slop patterns stacked: in-memory data round-tripped
through GitHub, a fail-soft post on the critical path, an unimplemented
documented fallback, and no seam test. The loop's own per-PR critic could not
catch it because the break was *between* components. This rule + the
findings-always-land fix ship together.

### Q11. A worker's required toolchain/environment must be DECLARED and PREFLIGHTED — no verification step may depend on ambient PATH/inherited env.

A capability the agent needs — the project venv, a linter, a type-checker, a
test runner — must be **provisioned and verified at dispatch**, failing LOUD if
absent, never silently degrading. Concretely: the per-project worker-environment
contract (``worker.env.path_prepend`` / ``vars`` / ``require`` + ``worker.verify``)
is the single source of truth; the loop builds the worker's PATH from it and
preflights ``require`` with ``shutil.which`` BEFORE driving the session,
aborting with a typed ``worker_toolchain_unavailable`` event if a tool is
missing. A verification step (lint/type/test gate) that resolves its tool via
the worker's *inherited* ``PATH``/``VIRTUAL_ENV`` rather than the *declared*
contract is **sev2**; shipping a worker path that runs a gate command without a
declared+preflighted contract behind it is **sev2**. The brief must inject the
canonical ``verify`` commands so the worker runs them verbatim and never guesses
``mypy`` vs ``python -m mypy``.

**Rationale.** The 2026-06-05 silent-toolchain incident: workers run in a
``/tmp`` git worktree and inherited the orchestrator's ambient env
(``_worker_sdk._clean_sdk_env`` = ``dict(os.environ)``). The project ``.venv``
was NOT on that PATH (workers saw ``VIRTUAL_ENV=/usr`` and the system python),
so ``pyright`` / ``mypy`` / ``pytest`` silently failed with "command not found";
the worker retried command variants for ~20 minutes with NO error surfaced. The
toolchain was an IMPLICIT, unverified, silently-degrading dependency — the
"Agent Enablement" gap: an agent must be GIVEN (and verified to have) the
environment it needs. This rule + the ``worker_env`` provisioning/preflight and
the declared per-project contract ship together.

### Q12. The definition-of-done (`worker.verify`) must be a programmatic, repo-wide MERGE GATE — not just prose in the brief.

A command that defines "done" (``ruff check src/ tests/``, ``pyright
src/forge_loop``, ``python -m pytest -q``) MUST be enforced by something that
**deterministically blocks the merge** when it fails — not merely injected into
the worker brief as an instruction the LLM may self-report satisfying. The gate
runs the configured ``worker.verify`` commands against the **whole repo /
worktree** (NOT just the diff), AFTER the critic and BEFORE auto-merge is
enabled, mirroring ``merge_gate.apply_issue_closed_gate``: on a non-clean
result it disables auto-merge, posts a PR comment, emits a typed
``merge_refused_verify_unclean`` event (which command failed + a truncated
output tail), and flips the worker outcome ``merged`` → ``open`` so the attempts
ledger reflects the truth. Tools are resolved via the **declared** ``worker.env``
contract (Q11), never ambient ``PATH``; a missing tool fails LOUD (refuse), never
silently passes. Each verify command is tokenised with ``shlex.split`` and exec'd
as ``argv`` (``shell=False``) — never piped through ``/bin/sh`` — so an
operator-declared config string carries no shell-injection sharp edge; an
unparseable command is a config error → fail-closed, not a silent pass. Shipping
a verify list with no gate behind it, or a gate that
checks only the diff rather than the whole repo, is **sev2**. The gate may ship
behind an enable flag defaulting to off ONLY while the baseline is red (a
non-clean repo would block every PR); the flip-to-enforce is then a tracked
follow-up.

**Rationale.** Issue #241. forge-loop has no GitHub Actions CI (``Taskfile.yml``
documents that decision — it is its own CI), so ``gh pr merge --auto`` had no
required check to wait on, and the only real pre-merge gate
(``runner/merge_gate.py``) checked just one thing: whether the source issue was
closed mid-flight. The repo accreted **30 ruff + 61 pyright** violations
PR-by-PR — each diff looked locally clean to the per-PR critic while the LLM
worker self-reported "definition of done met" and repo-wide ``ruff`` / ``pyright``
were red. Per the boiling-frog meta-rule ("a metric with no gate drifts"), an
unenforced verify list is decoration. This rule + the
``apply_verify_clean_gate`` ratchet ship together.

**State-gate.** Per the "state-based rules need a state-based gate" meta-rule:
the cumulative clean-tree invariant is enforced by the gate itself running the
repo-wide commands every merge cycle. Once the ruff/pyright baseline is green
and ``worker.verify_gate_enabled`` is flipped on, a regression cannot accrete —
the next merge that would introduce a violation is refused deterministically.

## How to apply this manifesto

* When reviewing a forge-loop PR, scan the diff for each rule. A
  violation is a blocking review comment, not a nit.
* When the critic agent reviews a forge-loop PR, it loads this file and
  flags violations as P0 findings.
* When adding a new rule, add a rationale paragraph that names the
  concrete issue or incident. No rationale ⇒ no rule.
