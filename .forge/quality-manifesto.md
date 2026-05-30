# forge-loop quality manifesto (seed)

This is the dogfood quality manifesto for forge-loop itself. Every future
forge-loop change is gated by these rules. Each rule is followed by a
rationale that names the iteration-probe bug or persistent-worker work
that motivated it, so future contributors understand *why* the rule
exists, not just what it says.

## Rules

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

## How to apply this manifesto

* When reviewing a forge-loop PR, scan the diff for each rule. A
  violation is a blocking review comment, not a nit.
* When the critic agent reviews a forge-loop PR, it loads this file and
  flags violations as P0 findings.
* When adding a new rule, add a rationale paragraph that names the
  concrete issue or incident. No rationale ⇒ no rule.
